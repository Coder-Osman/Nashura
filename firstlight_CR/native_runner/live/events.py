"""Validated public card-consumption receipts, never inferred from spawns."""
from __future__ import annotations

from .snapshot import LiveStateError

PUBLIC_EVENT_SCHEMA = "cr-live-public-events.v1"
MAX_PUBLIC_EVENTS = 2048


def public_events(row: dict, *, required: bool = True) -> list[dict]:
    stream = row.get("public_events")
    if stream is None and not required:
        return []
    if not isinstance(stream, dict) or stream.get("schema") != PUBLIC_EVENT_SCHEMA:
        raise LiveStateError("capture lacks verified public card-play events; start a new capture")
    events = stream.get("events")
    if (stream.get("complete") is not True or stream.get("first_sequence") != 1
            or not isinstance(events, list) or len(events) > MAX_PUBLIC_EVENTS
            or stream.get("next_sequence") != len(events) + 1):
        raise LiveStateError("public card-play event stream is incomplete")
    started = stream.get("started_tick")
    if type(started) is not int or not 0 <= started <= row["tick"]:
        raise LiveStateError("invalid public event start tick")
    previous_tick = started
    allowed = {"sequence", "tick", "owner", "card_id", "cost", "form_code", "source", "card_identity"}
    for index, event in enumerate(events, 1):
        if set(event) != allowed or event.get("source") != "native-consume-card.v1":
            raise LiveStateError("unsupported public card-play receipt")
        for name, low, high in (("sequence", index, index), ("tick", previous_tick, row["tick"]),
                                ("owner", 0, 1), ("card_id", 1, 2**32-1),
                                ("cost", 0, 15), ("form_code", 0, 5)):
            if type(event.get(name)) is not int or not low <= event[name] <= high:
                raise LiveStateError("invalid public event " + name)
        if event["card_identity"] not in ("own-deck", "consumed-descriptor"):
            raise LiveStateError("unknown public card identity provenance")
        if event["card_identity"] == "own-deck" and event["owner"] != row["own_side"]:
            raise LiveStateError("opponent private deck identity in public feed")
        previous_tick = event["tick"]
    return events


def event_watermark(row: dict) -> int:
    return len(public_events(row))


class PublicEventCursor:
    def __init__(self, *, required: bool = True):
        self.required = required
        self.identity = None
        self.seen = []

    def update(self, row: dict) -> tuple[dict, ...]:
        identity = (row["battle_epoch"], row["own_side"])
        events = public_events(row, required=self.required)
        if self.identity is None:
            self.identity = identity
        elif self.identity != identity:
            raise LiveStateError("public event cursor belongs to another battle")
        if len(events) < len(self.seen) or events[:len(self.seen)] != self.seen:
            raise LiveStateError("public card-play history changed or regressed")
        fresh = tuple(dict(e) for e in events[len(self.seen):])
        self.seen.extend(fresh)
        return fresh
