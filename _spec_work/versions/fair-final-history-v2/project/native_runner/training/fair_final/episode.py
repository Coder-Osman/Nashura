from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid

import torch

from native_runner.contracts import TerminalV1
from ..fair_search.contracts import pack, require_sha, unpack
from .policy import FullAction, LegalPrefixes
from .rollout import DecisionInformation


SCHEMA = "FairFinalAcceptanceEpisode/v1"


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def terminal_result(terminal, raw, *, truncated, failed):
    if failed or truncated or not terminal.ended or raw.get("finalized") is not True:
        raise ValueError("complete native finalization required; truncation is not a draw")
    result = raw.get("worldResult")
    if type(result) is not int or result not in (-1, 0, 1) or raw.get("worldResultRaw") != result:
        raise ValueError("unrecognized native final result")
    winner = result if result in (0, 1) else None
    expected = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    if raw.get("winner") != winner or terminal.winner != winner or terminal.result_by_owner != expected:
        raise ValueError("native result and owner labels disagree")
    crowns = raw.get("crownsRaw")
    if winner is None and (not isinstance(crowns, (tuple, list)) or len(crowns) != 2 or crowns[0] != crowns[1]):
        raise ValueError("native draw needs equal exposed crowns")
    return dict(winner=winner, terminal_tick=terminal.terminal_tick,
        labels=["DRAW" if winner is None else "WIN" if owner == winner else "LOSS" for owner in (0, 1)],
        native_evidence={key: raw[key] for key in ("finalized", "worldResult", "worldResultRaw", "winner", "crownsRaw")})


class AcceptanceRecorder:
    def __init__(self, root, *, match_id, attempt_id, identity):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", match_id) or type(attempt_id) is not int or not 1 <= attempt_id <= 3:
            raise ValueError("invalid match or attempt identity")
        if set(identity) != {"checkpoint_sha256", "engine_sha256", "source_manifest_sha256", "controller", "adapter"}:
            raise ValueError("acceptance identity fields differ")
        for key in ("checkpoint_sha256", "engine_sha256", "source_manifest_sha256"):
            require_sha(identity[key])
        self.root = Path(root)
        self.match_id, self.attempt_id = match_id, attempt_id
        self.identity = dict(identity)
        self.directory = self.root / "attempts" / match_id / str(attempt_id)
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "frames").mkdir()
        self.rows = []
        self.resolved = 0
        self.sealed = False
        atomic_json(self.directory / "attempt.json", dict(schema=SCHEMA, match_id=match_id,
            attempt_id=attempt_id, identity=self.identity, status="collecting", training_admissible=False))

    def record_committed(self, information, actions, clock):
        if self.sealed or set(information) != set(actions) or clock.ready:
            raise ValueError("record only a complete committed boundary")
        prepared = []
        for owner in sorted(information):
            info, action = information[owner], actions[owner]
            plan = clock.pending[owner]
            if (info.owner != owner or info.tick != clock.tick or plan.created_tick != info.tick
                    or plan.action != action or not info.legality.legal(action)
                    or plan.identity != len(self.rows) + len(prepared)):
                raise ValueError("decision does not match its legal committed reservation")
            relative = f"frames/{plan.identity:06d}.pt.gz"
            payload = dict(frame=pack(info.frame), card_ids=list(info.legality.card_ids),
                plays=pack(info.legality.plays), wait=bool(info.legality.modes[0]))
            buffer = io.BytesIO()
            torch.save(payload, buffer)
            compressed = gzip.compress(buffer.getvalue(), compresslevel=1, mtime=0)
            with (self.directory / relative).open("xb") as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            prepared.append(dict(row_id=plan.identity, owner=owner, tick=info.tick, due_tick=plan.due_tick,
                fair_sha256=info.sha256, frame_ref=relative, frame_sha256=hashlib.sha256(compressed).hexdigest(),
                action=asdict(action), action_id=action.key, search_mode="NONE", policy_target=None))
        self.rows.extend(prepared)

    def reconcile(self, clock):
        if self.sealed or len(clock.evidence) < self.resolved:
            raise ValueError("recorder lifecycle moved backwards or is sealed")
        for event in clock.evidence[self.resolved:]:
            identity = event["reservation"]
            if type(identity) is not int or not 0 <= identity < len(self.rows):
                raise ValueError("unrecorded reservation outcome")
            row = self.rows[identity]
            reason = event["reason"]
            if row["owner"] != event["owner"] or "outcome" in row:
                raise ValueError("duplicate or mismatched reservation outcome")
            if reason not in ("executed", "wait_expired", "preempted", "terminal_expired"):
                raise ValueError("rejected or unconfirmed action quarantines the episode")
            if (reason == "executed" and row["action_id"] == "wait") or (reason == "wait_expired" and row["action_id"] != "wait"):
                raise ValueError("outcome differs from action type")
            if reason in ("executed", "wait_expired") and event["tick"] != row["due_tick"]:
                raise ValueError("execution attestation missed its deadline")
            if reason == "preempted" and not row["tick"] < event["tick"] < row["due_tick"]:
                raise ValueError("preemption must precede the deadline")
            row["outcome"] = dict(event)
            self.resolved += 1

    def publish(self, *, terminal, raw, runtime):
        self.reconcile(runtime.clock)
        result = terminal_result(terminal, raw, truncated=runtime.truncated, failed=runtime.failed)
        if (not runtime.ended or runtime.clock.pending or runtime.clock.ready or runtime.frozen
                or runtime.clock.tick != result["terminal_tick"] or not self.rows
                or self.resolved != len(self.rows) or runtime.clock.next_identity != len(self.rows)
                or {row["owner"] for row in self.rows} != {0, 1}):
            raise ValueError("episode has incomplete boundaries or unresolved decisions")
        for row in self.rows:
            row["wdl"] = result["labels"][row["owner"]]
        episode = dict(schema=SCHEMA, match_id=self.match_id, attempt_id=self.attempt_id, identity=self.identity,
            purpose="native-complete-episode-acceptance", training_admissible=False,
            result=result, rows=self.rows)
        atomic_json(self.directory / "episode.json", episode)
        validate_attempt(self.directory)
        receipt = dict(schema=SCHEMA, match_id=self.match_id, attempt_id=self.attempt_id,
            path=str(self.directory.relative_to(self.root)), identity=self.identity, rows=len(self.rows),
            episode_sha256=hashlib.sha256((self.directory / "episode.json").read_bytes()).hexdigest())
        completed = self.root / "completed"
        completed.mkdir(exist_ok=True)
        staged = self.directory / "receipt.json"
        atomic_json(staged, receipt)
        try:
            os.link(staged, completed / f"{self.match_id}.json")
        except FileExistsError:
            previous = json.loads((completed / f"{self.match_id}.json").read_text())
            if previous["identity"] != self.identity:
                raise ValueError("duplicate match has different experiment identity")
            previous_content = (self.root / previous["path"] / "episode.json").read_bytes()
            if hashlib.sha256(previous_content).hexdigest() != previous["episode_sha256"]:
                raise ValueError("previous completion receipt checksum mismatch")
            validate_attempt(self.root / previous["path"])
            self.sealed = True
            return dict(status="duplicate", receipt=previous)
        self.sealed = True
        return dict(status="published", receipt=receipt)

    def quarantine(self, reason):
        if self.sealed:
            raise ValueError("published episode is immutable")
        atomic_json(self.directory / "quarantine.json", dict(reason=str(reason), rows=self.rows,
            training_admissible=False, published=False))
        self.sealed = True


def validate_attempt(directory):
    directory = Path(directory)
    if (directory / "quarantine.json").exists():
        raise ValueError("quarantined episode")
    episode = json.loads((directory / "episode.json").read_text())
    if episode["schema"] != SCHEMA or episode["training_admissible"] is not False:
        raise ValueError("acceptance data must remain outside training")
    result = episode["result"]
    winner = result["winner"]
    scores = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    terminal = TerminalV1(ended=True, winner=winner, result_by_owner=scores, terminal_tick=result["terminal_tick"])
    expected = terminal_result(terminal, result["native_evidence"], truncated=False, failed=False)
    if result != expected or not episode["rows"] or {row["owner"] for row in episode["rows"]} != {0, 1}:
        raise ValueError("serialized terminal evidence or player coverage differs")
    previous_ticks = {0: -1, 1: -1}
    for index, row in enumerate(episode["rows"]):
        if row["row_id"] != index or row["frame_ref"] != f"frames/{index:06d}.pt.gz":
            raise ValueError("frame identity or ordering changed")
        content = (directory / row["frame_ref"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != row["frame_sha256"]:
            raise ValueError("frame checksum mismatch")
        payload = torch.load(io.BytesIO(gzip.decompress(content)), weights_only=True, map_location="cpu")
        legality = LegalPrefixes(payload["card_ids"], payload["plays"], wait=payload["wait"])
        info = DecisionInformation(row["owner"], row["tick"], unpack(payload["frame"]), legality)
        action = FullAction(**row["action"])
        duration = 60 if action.card_id is None else 2 * action.delay_slot
        outcome = row["outcome"]
        reason = outcome["reason"]
        if (not previous_ticks[row["owner"]] < row["tick"] < result["terminal_tick"]
                or row["due_tick"] != row["tick"] + duration
                or outcome["reservation"] != index or outcome["owner"] != row["owner"]
                or not row["tick"] < outcome["tick"] <= min(row["due_tick"], result["terminal_tick"])
                or (reason in ("executed", "wait_expired") and outcome["tick"] != row["due_tick"])
                or (reason == "preempted" and outcome["tick"] >= row["due_tick"])
                or (reason == "executed" and action.card_id is None)
                or (reason == "wait_expired" and action.card_id is not None)):
            raise ValueError("serialized reservation lifecycle differs")
        previous_ticks[row["owner"]] = row["tick"]
        if (info.sha256 != row["fair_sha256"] or not legality.legal(action) or action.key != row["action_id"]
                or row["search_mode"] != "NONE" or row["policy_target"] is not None
                or row["wdl"] != episode["result"]["labels"][row["owner"]]
                or row["outcome"]["reason"] not in ("executed", "wait_expired", "preempted", "terminal_expired")):
            raise ValueError("serialized decision differs from its validated FAIR contract")
    return episode
