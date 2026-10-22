"""Validation and capability checks for the partial, public live feed."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time


class LiveStateError(ValueError):
    pass


class CaptureReadError(LiveStateError):
    """The reader discarded a sample; a later valid sample may recover."""


def latest_snapshot(path: Path, *, wait_seconds: float = 0.25) -> dict:
    """Return the newest validated snapshot, tolerating the writer's first partial line.

    Capture starts asynchronously. A reader can observe the file between creation
    and the first newline, so briefly retry the empty/partial tail instead of
    turning that startup race into a visible policy block.
    """
    if wait_seconds < 0:
        raise ValueError("wait_seconds must be nonnegative")
    deadline = time.monotonic() + wait_seconds
    while True:
        if path.exists():
            # Read a bounded tail; discard a partial final line from the active writer.
            with path.open("rb") as source:
                source.seek(0, 2)
                size = source.tell()
                start = max(0, size - 2 * 1024 * 1024)
                source.seek(start)
                data = source.read()
            rows = data.split(b"\n")[:-1]
            if start:
                rows = rows[1:]
            for line in reversed(rows):
                row = json.loads(line)
                if row.get("event") in {"read_error", "script_error", "idle", "detached", "stopped"}:
                    detail = row.get("message")
                    if not detail and isinstance(row.get("detail"), dict):
                        detail = row["detail"].get("description")
                    suffix = f": {detail}" if detail else ""
                    error = CaptureReadError if row["event"] == "read_error" else LiveStateError
                    raise error("live capture is not ready: " + row["event"] + suffix)
                if row.get("event") == "snapshot":
                    return validate_snapshot(row)
        if time.monotonic() >= deadline:
            raise LiveStateError("no complete snapshot in capture tail")
        time.sleep(0.02)


def _integer(value, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise LiveStateError("invalid " + name)
    return value


def validate_snapshot(row: dict) -> dict:
    if row.get("schema") != "cr-live-snapshot.v1" or row.get("event") != "snapshot":
        raise LiveStateError("unsupported live snapshot")
    _integer(row.get("own_side"), 0, 1, "own side")
    for key in ("battle_epoch", "sequence", "received_monotonic_ns"):
        _integer(row.get(key), 1, 2**63 - 1, key)
    _integer(row.get("tick"), 0, 1000000, "tick")
    if type(row.get("ended")) is not bool:
        raise LiveStateError("invalid ended flag")
    if row.get("winner") not in (None, -1, 0, 1):
        raise LiveStateError("invalid winner")
    own = row["own"]
    _integer(own.get("elixir_raw"), 0, 100000, "own elixir")
    deck = own["deck"]
    if len(deck) != 8 or [c["deck_slot"] for c in deck] != list(range(8)):
        raise LiveStateError("invalid own deck slots")
    ids = [_integer(c["card_id"], 1, 2**31 - 1, "card id") for c in deck]
    if len(set(ids)) != 8:
        raise LiveStateError("duplicate own deck cards")
    for card in deck:
        if "form_code" in card:
            _integer(card["form_code"], 0, 15, "card form code")
        if "cost" in card:
            _integer(card["cost"], 0, 15, "card cost")

    def validate_card(card):
        if card is not None:
            index = _integer(card.get("deck_slot"), 0, 7, "deck slot")
            if card.get("card_id") != ids[index]:
                raise LiveStateError("hand/deck identity mismatch")
            for key in ("form_code", "cost"):
                if key in card or key in deck[index]:
                    if type(card.get(key)) is not int or card.get(key) != deck[index].get(key):
                        raise LiveStateError("hand/deck selection mismatch: " + key)

    if len(own["hand"]) != 4 or [c["slot"] for c in own["hand"]] != list(range(4)):
        raise LiveStateError("invalid hand slots")
    hand_ids = []
    for slot in own["hand"]:
        validate_card(slot["card"])
        if slot["card"] is not None:
            hand_ids.append(slot["card"]["card_id"])
    if len(set(hand_ids)) != len(hand_ids):
        raise LiveStateError("duplicate hand cards")
    validate_card(own["next_card"])
    entities = row["entities"]
    if len(entities) > 2048 or len({e["id"] for e in entities}) != len(entities):
        raise LiveStateError("invalid entity identities")
    for entity in entities:
        _integer(entity["id"], 1, 2**32 - 1, "entity id")
        _integer(entity["owner"], 0, 1, "entity owner")
        if entity["kind"] not in (12, 13, 15):
            raise LiveStateError("unsupported entity kind")
        # Native health is signed int32, not capped at one million. Hero
        # helpers have scaled HP above that; static max HP may be zero and
        # does not constrain health added by native healing mechanics.
        for key, low, high in (("hp", 1, 2**31 - 1), ("max_hp", 0, 2**31 - 1),
                               ("x", -1000, 19000), ("y", -1000, 33000)):
            _integer(entity[key], low, high, key)
        if (entity["owner"] != row["own_side"]
                and entity["visibility"] != "visible"):
            raise LiveStateError("opponent entity is not confirmed visible")
    forbidden = {"opponent", "opponent_hand", "opponent_deck", "opponent_elixir", "pending_commands"}
    if forbidden.intersection(row):
        raise LiveStateError("private opponent fields in live feed")
    return row


def validate_touch(row: dict, *, epoch: int, slot: int, card_id: int,
                   now_ns: int | None = None, max_age_ms: int = 500) -> dict:
    validate_snapshot(row)
    _integer(slot, 0, 3, "hand slot")
    age_ms = ((time.monotonic_ns() if now_ns is None else now_ns) - row["received_monotonic_ns"]) / 1e6
    if not math.isfinite(age_ms) or not 0 <= age_ms <= max_age_ms:
        raise LiveStateError("snapshot expired or belongs to another host/boot")
    if row["ended"] or row["battle_epoch"] != epoch:
        raise LiveStateError("battle changed or ended")
    if row["tick"] < 150:
        raise LiveStateError("battle intro is still active")
    card = row["own"]["hand"][slot]["card"]
    if card is None or card["card_id"] != card_id:
        raise LiveStateError("selected card is no longer in that slot")
    if "cost" in card:
        cost = _integer(card["cost"], 0, 15, "card cost")
        if row["own"]["elixir_raw"] < cost * 10000:
            raise LiveStateError("selected card is not affordable")
    return card


def same_battle_continuation(previous: dict, current: dict) -> bool:
    """Prove a capture epoch reset did not start a new match.

    The native world pointer can be replaced while a match is still running.
    Epochs are therefore capture-stream identities, rather than game identities.
    This deliberately uses only public, stable fields and is conservative when
    the two samples do not share enough state to establish continuity.
    """
    validate_snapshot(previous)
    validate_snapshot(current)
    if previous["ended"] or current["ended"]:
        return False
    if current["battle_epoch"] == previous["battle_epoch"]:
        return True
    if current["own_side"] != previous["own_side"]:
        return False
    if current["tick"] < previous["tick"]:
        return False

    def deck_signature(row):
        return tuple((card["card_id"], card.get("form_code"), card.get("cost"))
                     for card in row["own"]["deck"])

    if deck_signature(current) != deck_signature(previous):
        return False

    def crown_signature(row):
        return {
            entity["id"]: (entity["owner"], entity.get("native_data_id"),
                           entity.get("native_data_name"), entity["x"], entity["y"],
                           entity["max_hp"], entity["hp"])
            for entity in row["entities"]
            if entity["kind"] in (12, 13)
            and entity.get("native_data_name") in
            {"KingTower", "PrincessTower", "ChefTowerKing", "ChefTower"}
        }

    before, after = crown_signature(previous), crown_signature(current)
    shared = set(before) & set(after)
    if not shared:
        return False
    for entity_id in shared:
        old, new = before[entity_id], after[entity_id]
        # Identity and static geometry must survive the rebind. Crown health
        # cannot increase during a match; this also rejects a reset to full HP.
        if old[:6] != new[:6] or new[6] > old[6]:
            return False
    return True


def model_blockers(row: dict) -> list[str]:
    validate_snapshot(row)
    coverage = row.get("coverage", {})
    required = ("combat_phases", "spells", "equipped_forms", "tower_identity",
                "action_masks", "public_opponent_tracker", "atomic_snapshot")
    return [key for key in required if coverage.get(key) is not True]
