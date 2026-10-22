from copy import deepcopy
from dataclasses import asdict
import gzip
import hashlib
import io
import json
from pathlib import Path

import torch

from native_runner.contracts import TerminalV1
from .fair_final.adapter import ADAPTER_ID, source_policy
from .fair_final.codec import ActionCodec
from .fair_final.contracts import exact_fields, integer, validate_frame_tensors
from .fair_final.episode import terminal_result
from .fair_final.policy import FullAction, LegalPrefixes
from .fair_final.rollout import DecisionInformation
from .fair_final.storage import durable_write, ensure_immutable, json_bytes
from .fair_search.contracts import pack, require_sha, semantic_digest, unpack
from .fair_search.storage import torch_bytes
from .v4.tensors import UniversalSemanticBatchV4, concatenate_padded_tensor_records


SCHEMA = "JointMigrationEpisode/v1"
DATASET_SCHEMA = "FairFinalMigrationDataset/v1"
IDENTITY_FIELDS = {"source_a_sha", "source_network_sha", "migration_adapter_sha", "engine_protocol_sha",
    "action_protocol_sha", "fair_protocol_sha", "observation_contract_sha"}


def validate_manifest(manifest):
    exact_fields(manifest, {"schema", "namespace_sha", "identity", "card_ids", "round", "matches"}, "migration dataset")
    if manifest["schema"] != DATASET_SCHEMA:
        raise ValueError("independent migration dataset required")
    require_sha(manifest["namespace_sha"])
    exact_fields(manifest["identity"], IDENTITY_FIELDS, "migration dataset identity")
    for value in manifest["identity"].values():
        require_sha(value)
    if manifest["identity"]["migration_adapter_sha"] != semantic_digest(ADAPTER_ID):
        raise ValueError("migration adapter semantics differ")
    ActionCodec(manifest["card_ids"])
    integer(manifest["round"], 0, "migration round")
    if manifest["round"] > 2 or not isinstance(manifest["matches"], dict) or not manifest["matches"]:
        raise ValueError("frozen initial or two supplementary migration rounds required")
    pairs = {}
    for match_id, task in manifest["matches"].items():
        require_sha(match_id)
        exact_fields(task, {"pair_seed_id", "split", "behavior_checkpoint_shas"}, "migration task")
        require_sha(task["pair_seed_id"])
        if task["split"] not in ("train", "validation", "test"):
            raise ValueError("unknown migration split")
        if pairs.setdefault(task["pair_seed_id"], task["split"]) != task["split"]:
            raise ValueError("paired migration games cross splits")
        models = task["behavior_checkpoint_shas"]
        if not isinstance(models, list) or len(models) != 2:
            raise ValueError("both behavior checkpoint identities required")
        for model in models:
            require_sha(model)
        source = manifest["identity"]["source_a_sha"]
        if ((manifest["round"] == 0 and models != [source, source])
                or (manifest["round"] > 0 and models.count(source) != 1)):
            raise ValueError("migration behavior checkpoints differ from round")
    return semantic_digest(manifest)


def validate_teacher(teacher, identity):
    if (any(module.training for module in teacher.modules())
            or any(parameter.requires_grad or parameter.dtype != torch.float32 for parameter in teacher.parameters())
            or semantic_digest(teacher.state_dict()) != identity["source_network_sha"]):
        raise ValueError("unchanged frozen FP32 migration teacher required")


def conditional_reference(identity, fair_sha):
    return dict(schema="FrozenASourceConditionals/v1", source_a_sha=identity["source_a_sha"],
        source_network_sha=identity["source_network_sha"], migration_adapter_sha=identity["migration_adapter_sha"],
        fair_sha=fair_sha)


def teacher_targets(teacher, info):
    with torch.no_grad():
        policy, logits = source_policy(teacher, info.frame, info.legality)
        action, _ = policy.sample(torch.Generator().manual_seed(0), greedy=True)
        wdl = logits[0].float().softmax(-1).cpu()
    return action, wdl


def information_from_row(row):
    frame = unpack(row["frame"])
    if not isinstance(frame, UniversalSemanticBatchV4) or frame.batch_size != 1:
        raise ValueError("exact single FAIR semantic frame required")
    validate_frame_tensors(frame)
    pack(frame)
    masks = row["legality"]
    exact_fields(masks, {"card_ids", "plays", "wait"}, "migration legality")
    if (not isinstance(masks["plays"], torch.Tensor) or masks["plays"].dtype != torch.bool
            or masks["plays"].shape != (8, 576, 31) or bool(masks["plays"][..., 0].any())
            or masks["wait"] is not True):
        raise ValueError("exact executable migration legal mask required")
    legality = LegalPrefixes(masks["card_ids"], masks["plays"], wait=True)
    info = DecisionInformation(row["owner"], row["tick"], frame, legality)
    if info.sha256 != row["fair_sha"]:
        raise ValueError("migration FAIR information checksum differs")
    return info


def validate_episode(episode, manifest):
    dataset_sha = validate_manifest(manifest)
    exact_fields(episode, {"schema", "status", "dataset_sha", "match_id", "attempt_id", "gameplay_end_tick",
        "result", "rows"}, "migration episode")
    require_sha(episode["match_id"])
    task = manifest["matches"].get(episode["match_id"])
    if (episode["schema"] != SCHEMA or episode["status"] != "COMPLETE" or task is None
            or episode["dataset_sha"] != dataset_sha):
        raise ValueError("complete pre-registered migration episode required")
    integer(episode["attempt_id"], 1, "migration attempt")
    integer(episode["gameplay_end_tick"], 1, "gameplay end")
    if episode["attempt_id"] > 3:
        raise ValueError("migration retry limit exceeded")
    result = episode["result"]
    exact_fields(result, {"winner", "terminal_tick", "labels", "native_evidence"}, "migration terminal")
    integer(result["terminal_tick"], 1, "native terminal tick")
    winner = result["winner"]
    if winner is not None and (type(winner) is not int or winner not in (0, 1)):
        raise ValueError("invalid migration winner")
    scores = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    terminal = TerminalV1(ended=True, winner=winner, result_by_owner=scores, terminal_tick=result["terminal_tick"])
    exact_fields(result["native_evidence"], {"finalized", "worldResult", "worldResultRaw", "winner", "crownsRaw"},
        "migration native result")
    if (terminal_result(terminal, result["native_evidence"], truncated=False, failed=False) != result
            or episode["gameplay_end_tick"] > min(6000, result["terminal_tick"])):
        raise ValueError("migration terminal evidence differs")
    rows = episode["rows"]
    if (not isinstance(rows, list) or len(rows) < 2 or [row.get("owner") for row in rows[:2]] != [0, 1]
            or rows[0].get("tick") != rows[1].get("tick")):
        raise ValueError("both-player opening decisions required")
    previous, last_key = {}, (-1, -1)
    for index, row in enumerate(rows):
        exact_fields(row, {"row_id", "reservation", "owner", "tick", "frame", "fair_sha", "legality",
            "behavior_action", "teacher_action", "teacher_wdl", "teacher_conditionals_ref", "wdl", "outcome"},
            "migration decision")
        integer(row["owner"], 0, "migration owner")
        integer(row["tick"], 0, "migration decision tick")
        integer(row["reservation"], 0, "migration reservation")
        if (row["owner"] not in (0, 1) or row["reservation"] != index
                or row["row_id"] != semantic_digest([episode["match_id"], index])
                or not last_key < (row["tick"], row["owner"]) or row["tick"] >= episode["gameplay_end_tick"]):
            raise ValueError("ordered migration boundary identity differs")
        last_key = row["tick"], row["owner"]
        prior = previous.get(row["owner"])
        if prior is not None and (prior["outcome"]["tick"] != row["tick"]
                or prior["outcome"]["reason"] == "terminal_expired"):
            raise ValueError("missing or unresolved migration decision boundary")
        info = information_from_row(row)
        if set(info.legality.card_ids) != set(manifest["card_ids"]):
            raise ValueError("migration stable deck differs")
        behavior, target = FullAction(**row["behavior_action"]), FullAction(**row["teacher_action"])
        if not info.legality.legal(behavior) or not info.legality.legal(target):
            raise ValueError("illegal migration behavior or teacher action")
        if (task["behavior_checkpoint_shas"][info.owner] == manifest["identity"]["source_a_sha"]
                and behavior != target):
            raise ValueError("source-controlled migration behavior differs from teacher greedy")
        wdl = row["teacher_wdl"]
        if (not isinstance(wdl, torch.Tensor) or wdl.dtype != torch.float32 or wdl.shape != (3,)
                or not bool(torch.isfinite(wdl).all()) or bool((wdl < 0).any())
                or not torch.isclose(wdl.sum(), torch.tensor(1.), atol=1e-6, rtol=0)):
            raise ValueError("normalized FP32 teacher WDL required")
        if (row["teacher_conditionals_ref"] != conditional_reference(manifest["identity"], info.sha256)
                or row["wdl"] != result["labels"][info.owner]):
            raise ValueError("teacher soft-target identity or terminal label differs")
        outcome = row["outcome"]
        exact_fields(outcome, {"reservation", "owner", "tick", "reason"}, "migration outcome")
        for key in ("reservation", "owner", "tick"):
            integer(outcome[key], 0, "outcome " + key)
        due = info.tick + (60 if behavior.card_id is None else 2 * behavior.delay_slot)
        reason = outcome["reason"]
        if (outcome["reservation"] != index or outcome["owner"] != info.owner
                or reason not in ("executed", "wait_expired", "preempted", "terminal_expired")
                or not info.tick < outcome["tick"] <= min(due, episode["gameplay_end_tick"])
                or (reason in ("executed", "wait_expired") and outcome["tick"] != due)
                or (reason in ("preempted", "terminal_expired") and outcome["tick"] >= due)
                or (reason == "terminal_expired" and outcome["tick"] != episode["gameplay_end_tick"])
                or (reason == "executed" and behavior.card_id is None)
                or (reason == "wait_expired" and behavior.card_id is not None)):
            raise ValueError("unconfirmed or invalid migration reservation lifecycle")
        previous[info.owner] = row
    if set(previous) != {0, 1} or any(row["outcome"]["tick"] != episode["gameplay_end_tick"]
            or row["outcome"]["reason"] == "preempted" for row in previous.values()):
        raise ValueError("both migration trajectories must reach gameplay end")
    return episode


class MigrationStore:
    def __init__(self, root, *, manifest):
        self.manifest = deepcopy(manifest)
        self.dataset_sha = validate_manifest(self.manifest)
        self.root = Path(root)
        ensure_immutable(self.root / "dataset.json", json_bytes(self.manifest))

    def publish(self, episode):
        if validate_manifest(self.manifest) != self.dataset_sha:
            raise ValueError("migration manifest changed after freezing")
        validate_episode(episode, self.manifest)
        if (self.root / "attempts" / episode["match_id"] / str(episode["attempt_id"]) / "quarantine.json").exists():
            raise ValueError("quarantined migration attempt")
        digest = semantic_digest(episode)
        data = gzip.compress(torch_bytes(dict(schema=SCHEMA, sha256=digest, episode=pack(episode))),
            compresslevel=1, mtime=0)
        path = self.root / "episodes" / f"{digest}.pt.gz"
        try:
            durable_write(path, data)
        except FileExistsError:
            self._read(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        receipt = dict(schema="FairFinalMigrationReceipt/v1", dataset_sha=self.dataset_sha,
            match_id=episode["match_id"], semantic_sha=digest, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            attempt_id=episode["attempt_id"], rows=len(episode["rows"]))
        try:
            durable_write(self.root / "completed" / f"{episode['match_id']}.json", json_bytes(receipt))
        except FileExistsError:
            previous, _ = self.load(episode["match_id"])
            return dict(status="duplicate", semantic_sha=semantic_digest(previous))
        return dict(status="published", semantic_sha=digest)

    def _read(self, digest, expected_sha):
        require_sha(digest)
        data = (self.root / "episodes" / f"{digest}.pt.gz").read_bytes()
        if hashlib.sha256(data).hexdigest() != require_sha(expected_sha):
            raise ValueError("migration file checksum differs")
        envelope = torch.load(io.BytesIO(gzip.decompress(data)), weights_only=True, map_location="cpu")
        exact_fields(envelope, {"schema", "sha256", "episode"}, "migration envelope")
        episode = envelope["episode"]
        if envelope["schema"] != SCHEMA or envelope["sha256"] != digest or semantic_digest(episode) != digest:
            raise ValueError("migration semantic checksum differs")
        return validate_episode(episode, self.manifest)

    def load(self, match_id):
        if validate_manifest(self.manifest) != self.dataset_sha:
            raise ValueError("migration manifest changed after freezing")
        require_sha(match_id)
        receipt = json.loads((self.root / "completed" / f"{match_id}.json").read_text())
        exact_fields(receipt, {"schema", "dataset_sha", "match_id", "semantic_sha", "sha256", "attempt_id", "rows"},
            "migration receipt")
        integer(receipt["attempt_id"], 1, "migration receipt attempt")
        integer(receipt["rows"], 2, "migration receipt rows")
        if (receipt["schema"] != "FairFinalMigrationReceipt/v1" or receipt["dataset_sha"] != self.dataset_sha
                or receipt["match_id"] != match_id):
            raise ValueError("migration completion identity differs")
        episode = self._read(receipt["semantic_sha"], receipt["sha256"])
        if (episode["match_id"] != match_id or episode["attempt_id"] != receipt["attempt_id"]
                or len(episode["rows"]) != receipt["rows"]):
            raise ValueError("migration receipt accounting differs")
        if (self.root / "attempts" / match_id / str(receipt["attempt_id"]) / "quarantine.json").exists():
            raise ValueError("quarantined migration attempt")
        return episode, receipt


class MigrationRecorder:
    def __init__(self, store, *, match_id, attempt_id, teacher):
        require_sha(match_id)
        integer(attempt_id, 1, "migration attempt")
        if match_id not in store.manifest["matches"] or attempt_id > 3:
            raise ValueError("unregistered migration attempt")
        validate_teacher(teacher, store.manifest["identity"])
        self.store, self.teacher = store, teacher
        self.match_id, self.attempt_id = match_id, attempt_id
        self.rows, self.resolved, self.sealed = [], 0, False
        self.directory = store.root / "attempts" / match_id / str(attempt_id)
        self.directory.mkdir(parents=True, exist_ok=False)
        durable_write(self.directory / "attempt.json", json_bytes(dict(schema=SCHEMA, match_id=match_id,
            attempt_id=attempt_id, dataset_sha=store.dataset_sha, status="collecting")))

    def record_committed(self, information, actions, clock):
        if (self.sealed or not information or set(information) != set(actions) or clock.ready
                or clock.next_identity != len(self.rows) + len(information)):
            raise ValueError("record a complete committed migration boundary")
        prepared = []
        for owner in sorted(information):
            info, action = information[owner], actions[owner]
            plan = clock.pending[owner]
            index = len(self.rows) + len(prepared)
            if (info.owner != owner or info.tick != clock.tick or plan.created_tick != info.tick
                    or plan.action != action or plan.identity != index or not info.legality.legal(action)):
                raise ValueError("migration row differs from committed reservation")
            target, wdl = teacher_targets(self.teacher, info)
            prepared.append(dict(row_id=semantic_digest([self.match_id, index]), reservation=index, owner=owner,
                tick=info.tick, frame=pack(info.frame), fair_sha=info.sha256,
                legality=dict(card_ids=list(info.legality.card_ids), plays=pack(info.legality.plays), wait=True),
                behavior_action=asdict(action), teacher_action=asdict(target), teacher_wdl=wdl,
                teacher_conditionals_ref=conditional_reference(self.store.manifest["identity"], info.sha256)))
        self.rows.extend(prepared)

    def reconcile(self, clock):
        if self.sealed or len(clock.evidence) < self.resolved:
            raise ValueError("migration lifecycle moved backwards or is sealed")
        for outcome in clock.evidence[self.resolved:]:
            index = outcome["reservation"]
            if (type(index) is not int or not 0 <= index < len(self.rows)
                    or self.rows[index]["owner"] != outcome["owner"] or "outcome" in self.rows[index]
                    or outcome["reason"] not in ("executed", "wait_expired", "preempted", "terminal_expired")):
                raise ValueError("rejected, duplicate or unconfirmed migration outcome")
            self.rows[index]["outcome"] = deepcopy(outcome)
            self.resolved += 1

    def publish(self, *, terminal, raw, runtime):
        self.reconcile(runtime.clock)
        result = terminal_result(terminal, raw, truncated=runtime.truncated, failed=runtime.failed)
        if (not runtime.ended or runtime.clock.pending or runtime.clock.ready or runtime.frozen
                or self.resolved != len(self.rows) or runtime.clock.next_identity != len(self.rows)
                or runtime.clock.tick != result["terminal_tick"]):
            raise ValueError("migration game has unresolved final boundaries")
        validate_teacher(self.teacher, self.store.manifest["identity"])
        for row in self.rows:
            row["wdl"] = result["labels"][row["owner"]]
        episode = dict(schema=SCHEMA, status="COMPLETE", dataset_sha=self.store.dataset_sha,
            match_id=self.match_id, attempt_id=self.attempt_id,
            gameplay_end_tick=max(row["outcome"]["tick"] for row in self.rows),
            result=result, rows=self.rows)
        result = self.store.publish(episode)
        self.sealed = True
        return result

    def quarantine(self, reason):
        if self.sealed:
            raise ValueError("published migration attempt is immutable")
        durable_write(self.directory / "quarantine.json", json_bytes(dict(reason=str(reason), published=False)))
        self.sealed = True


class MigrationDataset:
    def __init__(self, store, *, teacher):
        validate_teacher(teacher, store.manifest["identity"])
        self.store, self.teacher = store, teacher
        self.index, self.receipts = {}, {}
        self.splits = {name: [] for name in ("train", "validation", "test")}
        for match_id, task in sorted(store.manifest["matches"].items()):
            episode, receipt = store.load(match_id)
            self.receipts[match_id] = semantic_digest(receipt)
            for offset, row in enumerate(episode["rows"]):
                row_id = row["row_id"]
                if row_id in self.index:
                    raise ValueError("duplicate migration row identity")
                self.index[row_id] = (match_id, offset, task["split"])
                self.splits[task["split"]].append(row_id)
        if any(not rows for rows in self.splits.values()):
            raise ValueError("complete migration train, validation and test games required")
        self.identity = dict(source_a_sha=store.manifest["identity"]["source_a_sha"],
            migration_adapter_sha=store.manifest["identity"]["migration_adapter_sha"],
            dataset_sha=semantic_digest(dict(manifest_sha=store.dataset_sha, receipts=self.receipts)))

    def batch(self, row_ids, *, split="train"):
        ids = list(row_ids)
        if (split not in self.splits or not ids or len(set(ids)) != len(ids)
                or any(row_id not in self.index or self.index[row_id][2] != split for row_id in ids)):
            raise ValueError("migration batch crosses its explicit whole-game split")
        validate_teacher(self.teacher, self.store.manifest["identity"])
        loaded, frames, legalities = {}, [], []
        for row_id in ids:
            match_id, offset, _ = self.index[row_id]
            if match_id not in loaded:
                episode, receipt = self.store.load(match_id)
                if semantic_digest(receipt) != self.receipts[match_id]:
                    raise ValueError("migration data changed after indexing")
                loaded[match_id] = episode
            row = loaded[match_id]["rows"][offset]
            info = information_from_row(row)
            action, wdl = teacher_targets(self.teacher, info)
            if action != FullAction(**row["teacher_action"]) or not torch.allclose(wdl, row["teacher_wdl"], atol=1e-6, rtol=1e-5):
                raise ValueError("recorded migration teacher target differs from live frozen A")
            frames.append(info.frame)
            legalities.append(info.legality)
        return concatenate_padded_tensor_records(tuple(frames)), legalities
