from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

import torch

from ..fair_search.contracts import require_sha, semantic_digest
from .contracts import exact_fields, integer
from .policy import FullAction
from .storage import durable_write, json_bytes


SCHEMA = "FairFinalPrivateRecovery/v1"
IDENTITY_FIELDS = {"engine_sha256", "checkpoint_sha256", "source_manifest_sha256",
    "controller", "adapter"}


def rng_states(rollout):
    return {str(owner): generator.get_state().numpy().tobytes().hex()
        for owner, generator in rollout.generators.items()}


def restore_rng(rollout, states):
    exact_fields(states, {"0", "1"}, "execution RNG")
    prepared = {}
    for owner in (0, 1):
        encoded = states[str(owner)]
        if not isinstance(encoded, str) or len(encoded) != 2 * rollout.generators[owner].get_state().numel():
            raise ValueError("execution RNG state size differs")
        state = torch.tensor(list(bytes.fromhex(encoded)), dtype=torch.uint8)
        torch.Generator().set_state(state)
        prepared[owner] = state
    for owner, state in prepared.items():
        rollout.generators[owner].set_state(state)


def validate_identity(identity):
    exact_fields(identity, IDENTITY_FIELDS, "recovery identity")
    for field in ("engine_sha256", "checkpoint_sha256", "source_manifest_sha256"):
        require_sha(identity[field])
    if any(not isinstance(identity[field], str) or not identity[field] for field in ("controller", "adapter")):
        raise ValueError("recovery protocol identity is empty")


class RecoveryJournal:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = (self.root / "writer.lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.lock.close()
            raise
        self.process_id = os.getpid()
        self.rollout = None
        self.sequence = 0
        self.previous_sha = None
        self.evidence_count = 0
        self.evidence_sha = hashlib.sha256(b"fair-final-reservation-evidence.v1").hexdigest()
        self.closed = False

    @classmethod
    def create(cls, root, rollout, *, identity, private_inputs):
        validate_identity(identity)
        cls._validate_inputs(private_inputs)
        Path(root).mkdir(parents=True, exist_ok=False)
        journal = cls(root)
        journal.rollout = rollout
        try:
            journal._verify_opening(private_inputs)
            manifest = dict(schema=SCHEMA, training_admissible=False, identity=identity,
                episode_id=rollout.runtime.environment.episode_id, private_inputs=private_inputs,
                opening=journal._proof(), rng=rng_states(rollout))
            journal.manifest_sha = hashlib.sha256(json_bytes(manifest)).hexdigest()
            durable_write(journal.root / "manifest.json", json_bytes(manifest))
            journal.previous_sha = journal.manifest_sha
            return journal
        except Exception:
            rollout.runtime.failed = True
            journal.close()
            raise

    @staticmethod
    def _validate_inputs(inputs):
        exact_fields(inputs, {"match_config", "reset_options", "execution_seed"}, "private reset inputs")
        if not isinstance(inputs["match_config"], dict) or not isinstance(inputs["reset_options"], dict):
            raise ValueError("private reset configuration is malformed")
        integer(inputs["execution_seed"], 0, "execution seed")
        json_bytes(inputs)

    def _verify_opening(self, inputs):
        environment = self.rollout.runtime.environment
        if (environment.current_tick != environment.warmup_ticks
                or self.rollout.runtime.clock.next_identity != 0
                or self.rollout.runtime.clock.evidence
                or semantic_digest(asdict(environment.match_config)) != semantic_digest(inputs["match_config"])):
            raise ValueError("recovery requires a fresh engine with the exact match configuration")
        for owner, generator in self.rollout.generators.items():
            identity = f"fair-final-execution.v1:{inputs['execution_seed']}:{owner}".encode()
            seed = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
            if not torch.equal(generator.get_state(), torch.Generator().manual_seed(seed).get_state()):
                raise ValueError("recovery opening execution RNG differs")

    def _live(self):
        if self.closed or self.process_id != os.getpid():
            raise ValueError("recovery writer is closed or belongs to another process")
        if self.rollout.runtime.truncated:
            self._quarantine("truncated native match")
        if self.rollout.runtime.failed:
            raise ValueError("failed or truncated runtime requires verified fresh-engine recovery")

    def _quarantine(self, reason):
        self.rollout.runtime.failed = True
        path = self.root / "quarantine.json"
        if not path.exists():
            durable_write(path, json_bytes(dict(schema=SCHEMA, reason=reason,
                tick=self.rollout.runtime.clock.tick, previous_sha=self.previous_sha)))
        raise ValueError(reason)

    def _proof(self):
        self._live()
        rollout = self.rollout
        information = rollout.freeze_due_information()
        clock = rollout.runtime.clock
        if len(clock.evidence) < self.evidence_count:
            raise ValueError("reservation evidence moved backwards")
        for entry in clock.evidence[self.evidence_count:]:
            if entry["reason"] == "rejected":
                self._quarantine("rejected action quarantines the recovery attempt")
            self.evidence_sha = hashlib.sha256(bytes.fromhex(self.evidence_sha) + json_bytes(entry)).hexdigest()
        self.evidence_count = len(clock.evidence)
        environment = rollout.runtime.environment
        digest = environment.native._request("state-digest")["sha256"]
        if not isinstance(digest, str) or not digest:
            raise ValueError("native recovery digest is missing")
        state = dict(schema=clock.schema, tick=clock.tick, next_identity=clock.next_identity,
            pending=[asdict(clock.pending[owner]) for owner in sorted(clock.pending)], ready=sorted(clock.ready))
        return dict(tick=clock.tick, clock_sha=semantic_digest(state), evidence_count=self.evidence_count,
            evidence_sha=self.evidence_sha, native_digest=digest, ended=rollout.runtime.ended,
            fair={str(owner): info.sha256 for owner, info in information.items()})

    def _append(self, operation):
        body = dict(sequence=self.sequence, previous_sha=self.previous_sha, operation=operation)
        digest = hashlib.sha256(json_bytes(body)).hexdigest()
        durable_write(self.root / f"{self.sequence:08d}.json", json_bytes(dict(body=body, sha256=digest)))
        self.sequence += 1
        self.previous_sha = digest

    def commit(self, select):
        self._live()
        try:
            before = self._proof()
            information = self.rollout.freeze_due_information()
            if not information or self.rollout.runtime.ended:
                raise ValueError("journal commit requires a live frozen decision boundary")
            before_rng = rng_states(self.rollout)
            actions = select(information, self.rollout.generators)
            if set(actions) != set(information) or any(not isinstance(action, FullAction)
                    or not information[owner].legality.legal(action) for owner, action in actions.items()):
                raise ValueError("journal action group is incomplete or illegal")
            self._append(dict(kind="commit", before=before, rng_before=before_rng,
                rng_after=rng_states(self.rollout), actions={str(owner): asdict(action)
                    for owner, action in sorted(actions.items())}))
            self.rollout.commit_simultaneous(actions)
            return actions
        except Exception:
            self.rollout.runtime.failed = True
            raise

    def advance(self):
        self._live()
        try:
            outcomes = self.rollout.advance()
            if self.rollout.runtime.clock.ready or self.rollout.runtime.ended or self.rollout.runtime.truncated:
                self.checkpoint()
            return outcomes
        except Exception:
            self.rollout.runtime.failed = True
            raise

    def checkpoint(self):
        self._live()
        try:
            self._append(dict(kind="checkpoint", proof=self._proof(), rng=rng_states(self.rollout)))
            return dict(manifest_sha=self.manifest_sha, record_sha=self.previous_sha, records=self.sequence)
        except Exception:
            self.rollout.runtime.failed = True
            raise

    @classmethod
    def recover(cls, root, factory, *, expected_identity, expected_manifest_sha):
        validate_identity(expected_identity)
        require_sha(expected_manifest_sha)
        journal = cls(root)
        try:
            if (journal.root / "quarantine.json").exists():
                raise ValueError("quarantined recovery attempt requires a separate task attempt")
            encoded = (journal.root / "manifest.json").read_bytes()
            if hashlib.sha256(encoded).hexdigest() != expected_manifest_sha:
                raise ValueError("private recovery manifest checksum differs")
            manifest = json.loads(encoded)
            exact_fields(manifest, {"schema", "training_admissible", "identity", "episode_id",
                "private_inputs", "opening", "rng"}, "recovery manifest")
            if (manifest["schema"] != SCHEMA or manifest["training_admissible"] is not False
                    or manifest["identity"] != expected_identity):
                raise ValueError("recovery model, engine or source identity differs")
            cls._validate_inputs(manifest["private_inputs"])
            journal.manifest_sha = journal.previous_sha = expected_manifest_sha
            records = []
            for path in sorted(journal.root.glob("[0-9]*.json")):
                if not re.fullmatch(r"[0-9]{8}\.json", path.name):
                    raise ValueError("invalid recovery record filename")
                record = json.loads(path.read_bytes())
                exact_fields(record, {"body", "sha256"}, "recovery record")
                body = record["body"]
                exact_fields(body, {"sequence", "previous_sha", "operation"}, "recovery chain")
                if (path.name != f"{len(records):08d}.json" or type(body["sequence"]) is not int
                        or body["sequence"] != len(records) or body["previous_sha"] != journal.previous_sha
                        or hashlib.sha256(json_bytes(body)).hexdigest() != record["sha256"]):
                    raise ValueError("recovery record checksum or sequence differs")
                records.append(body["operation"])
                journal.previous_sha = record["sha256"]
            journal.rollout = factory(manifest["private_inputs"], manifest["episode_id"])
            if journal.rollout.runtime.environment.episode_id != manifest["episode_id"]:
                raise ValueError("recovery changed logical episode identity")
            journal._verify_opening(manifest["private_inputs"])
            if journal._proof() != manifest["opening"] or rng_states(journal.rollout) != manifest["rng"]:
                raise ValueError("recovery opening proof differs")
            for operation in records:
                journal._replay(operation)
            journal.sequence = len(records)
            return journal
        except Exception:
            if journal.rollout is not None:
                journal.rollout.runtime.failed = True
            journal.close()
            raise

    def _replay(self, operation):
        kind = operation.get("kind")
        if kind == "commit":
            exact_fields(operation, {"kind", "before", "rng_before", "rng_after", "actions"}, "commit intent")
            if self._proof() != operation["before"] or rng_states(self.rollout) != operation["rng_before"]:
                raise ValueError("recovery decision proof differs")
            actions = {int(owner): FullAction(**action) for owner, action in operation["actions"].items()}
            if {str(owner) for owner in actions} != set(operation["actions"]):
                raise ValueError("invalid recovered action owner")
            self.rollout.commit_simultaneous(actions)
            restore_rng(self.rollout, operation["rng_after"])
        elif kind == "checkpoint":
            exact_fields(operation, {"kind", "proof", "rng"}, "checkpoint record")
            target = operation["proof"]["tick"]
            integer(target, self.rollout.runtime.clock.tick, "recovery target tick")
            while self.rollout.runtime.clock.tick < target:
                self.rollout.advance()
            if self._proof() != operation["proof"] or rng_states(self.rollout) != operation["rng"]:
                raise ValueError("recovery checkpoint proof differs")
        else:
            raise ValueError("unknown recovery operation")

    def close(self):
        if not self.closed:
            self.lock.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
