from dataclasses import dataclass, fields
import math

import torch

from native_runner.contracts import TerminalV1
from ..fair_search.contracts import pack, require_sha, semantic_digest, unpack
from ..v4.tensors import UniversalSemanticBatchV4
from .codec import ActionCodec
from .episode import terminal_result
from .policy import LegalPrefixes
from .rollout import DecisionInformation


EPISODE_SCHEMA = "AZJointEpisode/v1"
IMPLEMENTATION = "fair-final-data.v1"
SEARCH_SCHEMA = "SearchResult/v1"
IDENTITY_FIELDS = {"engine_protocol_sha", "action_protocol_sha", "fair_protocol_sha",
    "observation_contract_sha", "root_belief_prior_sha", "checkpoint_blue_sha", "checkpoint_red_sha",
    "controller_blue_sha", "controller_red_sha", "search_protocol_sha", "opponent_pool_sha"}
STREAMS = {"world", "behavior", "opponent", "candidate", "root_noise", "engine", "replay", "evaluation"}


def exact_fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(label + " fields differ")


def integer(value, minimum, label):
    if type(value) is not int or value < minimum:
        raise ValueError("invalid " + label)


def validate_identity(identity):
    exact_fields(identity, IDENTITY_FIELDS, "episode identity")
    for value in identity.values():
        require_sha(value)


def validate_search(result, *, info, codec, identity, budget):
    exact_fields(result, {"schema", "root_fair_state_sha", "root_player", "value_perspective",
        "world_sampler_sha", "world_commitment", "search_protocol_sha", "opponent_controller_sha",
        "valid_simulations", "invalid_attempts", "unfinished_edge_backups", "candidates",
        "mode_mass", "noise_fraction"}, "search result")
    opponent = "controller_red_sha" if info.owner == 0 else "controller_blue_sha"
    if (result["schema"] != SEARCH_SCHEMA or result["root_fair_state_sha"] != info.sha256
            or type(result["root_player"]) is not int or result["root_player"] != info.owner
            or type(result["value_perspective"]) is not int or result["value_perspective"] != info.owner
            or result["world_sampler_sha"] != identity["root_belief_prior_sha"]
            or result["search_protocol_sha"] != identity["search_protocol_sha"]
            or result["opponent_controller_sha"] != identity[opponent]):
        raise ValueError("search root, perspective or protocol identity differs")
    require_sha(result["world_commitment"])
    for field in ("valid_simulations", "invalid_attempts", "unfinished_edge_backups"):
        integer(result[field], 0, field)
    if result["valid_simulations"] != budget or result["unfinished_edge_backups"] != 0:
        raise ValueError("incomplete search budget or unfinished edge backup")
    noise = result["noise_fraction"]
    if type(noise) not in (float, int) or not math.isfinite(noise) or not 0 <= noise <= 1:
        raise ValueError("invalid root noise")
    modes = result["mode_mass"]
    if (not isinstance(modes, list) or len(modes) != 2
            or any(type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1 for value in modes)
            or not math.isclose(sum(modes), 1., abs_tol=1e-5)):
        raise ValueError("invalid original mode mass")
    candidates = result["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 32:
        raise ValueError("invalid fixed candidate pool")
    seen, projected, searched, card_masses, visits = set(), {}, {}, {}, {}
    for entry in candidates:
        exact_fields(entry, {"action_id", "proposal", "original_joint_logp", "prior_projected",
            "prior_search", "prior_card", "visits", "q"}, "candidate")
        action = codec.decode(entry["action_id"])
        if entry["action_id"] in seen or not info.legality.legal(action):
            raise ValueError("duplicate or illegal candidate")
        seen.add(entry["action_id"])
        if entry["proposal"] not in ("WAIT", "CARD_ELITE", "SPATIAL_PEAK", "TIME_NEIGHBOR", "RANDOM"):
            raise ValueError("unknown candidate proposal")
        if (action.card_id is None) != (entry["proposal"] == "WAIT"):
            raise ValueError("WAIT candidate identity differs")
        for field in ("original_joint_logp", "prior_projected", "prior_search", "prior_card", "q"):
            if type(entry[field]) not in (float, int) or not math.isfinite(entry[field]):
                raise ValueError("nonfinite candidate statistic")
        if (entry["original_joint_logp"] > 1e-6 or not -1 <= entry["q"] <= 1
                or any(not 0 <= entry[field] <= 1 for field in ("prior_projected", "prior_search", "prior_card"))):
            raise ValueError("invalid candidate probability or root value")
        integer(entry["visits"], 0, "visits")
        visits[entry["action_id"]] = entry["visits"]
        card = action.card_id
        if card in card_masses and not math.isclose(card_masses[card], entry["prior_card"], abs_tol=1e-5):
            raise ValueError("candidate card mass identity differs")
        card_masses[card] = entry["prior_card"]
        projected[card] = projected.get(card, 0.) + entry["prior_projected"]
        searched[card] = searched.get(card, 0.) + entry["prior_search"]
        if noise == 0 and not math.isclose(entry["prior_projected"], entry["prior_search"], abs_tol=1e-5):
            raise ValueError("noise-free search changed its prior")
    required = {None, *[info.legality.card_ids[index] for index in torch.where(info.legality.cards)[0].tolist()]}
    if set(projected) != required or sum(visits.values()) != budget:
        raise ValueError("candidate coverage or visit count differs")
    if (not math.isclose(sum(projected.values()), 1., abs_tol=1e-5)
            or not math.isclose(sum(searched.values()), 1., abs_tol=1e-5)
            or not math.isclose(projected[None], modes[0], abs_tol=1e-5)
            or not math.isclose(searched[None], modes[0], abs_tol=1e-5)
            or any(not math.isclose(mass, card_masses[card], abs_tol=1e-5) for card, mass in projected.items())):
        raise ValueError("projected mode/card mass or ACT-only noise contract differs")
    return visits


@dataclass(frozen=True)
class AZJointEpisode:
    payload: dict

    def __post_init__(self):
        object.__setattr__(self, "payload", pack(self.payload))
        self.validate()

    @property
    def match_id(self):
        return self.payload["match_id"]

    @property
    def sha256(self):
        return semantic_digest(self.payload)

    def to_payload(self):
        self.validate()
        return pack(self.payload)

    def validate(self):
        payload = self.payload
        exact_fields(payload, {"schema", "implementation", "status", "match_id", "pair_seed_id", "gameplay_end_tick",
            "attempt_id", "split", "dataset_manifest_sha", "identity", "rng_stream_ids",
            "card_ids", "result", "decisions"}, "AZJointEpisode")
        if (payload["schema"] != EPISODE_SCHEMA or payload["implementation"] != IMPLEMENTATION
                or payload["status"] != "COMPLETE" or payload["split"] not in ("train", "validation", "test", "confirmation")):
            raise ValueError("independent completed FAIR final episode required")
        for field in ("match_id", "pair_seed_id", "dataset_manifest_sha"):
            require_sha(payload[field])
        integer(payload["attempt_id"], 1, "attempt_id")
        if payload["attempt_id"] > 3:
            raise ValueError("attempt limit exceeded")
        validate_identity(payload["identity"])
        exact_fields(payload["rng_stream_ids"], STREAMS, "RNG stream identities")
        for value in payload["rng_stream_ids"].values():
            require_sha(value)
        if len(set(payload["rng_stream_ids"].values())) != len(STREAMS):
            raise ValueError("RNG streams must have separate identities")
        codec = ActionCodec(payload["card_ids"])
        result = payload["result"]
        exact_fields(result, {"winner", "terminal_tick", "labels", "native_evidence"}, "terminal result")
        exact_fields(result["native_evidence"], {"finalized", "worldResult", "worldResultRaw", "winner", "crownsRaw"}, "native evidence")
        integer(result["terminal_tick"], 1, "terminal tick")
        integer(payload["gameplay_end_tick"], 1, "gameplay end")
        if payload["gameplay_end_tick"] > min(6000, result["terminal_tick"]):
            raise ValueError("gameplay boundary exceeds native terminal or game limit")
        winner = result["winner"]
        if winner is not None and (type(winner) is not int or winner not in (0, 1)):
            raise ValueError("invalid terminal winner")
        scores = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
        terminal = TerminalV1(ended=True, winner=winner, result_by_owner=scores, terminal_tick=result["terminal_tick"])
        if terminal_result(terminal, result["native_evidence"], truncated=False, failed=False) != result:
            raise ValueError("terminal labels differ from native evidence")
        rows = payload["decisions"]
        if (not isinstance(rows, list) or len(rows) < 2
                or [row.get("owner") for row in rows[:2]] != [0, 1]
                or rows[0].get("tick") != rows[1].get("tick")):
            raise ValueError("complete decisions required")
        previous, last_key = {}, (-1, -1)
        for index, row in enumerate(rows):
            exact_fields(row, {"row_id", "tick", "owner", "frame", "fair_sha", "legality",
                "pending_status", "behavior_action_id", "controller_sha", "search_mode", "search_result",
                "policy_target", "wdl", "outcome"}, "decision")
            integer(row["owner"], 0, "owner")
            integer(row["tick"], 0, "decision tick")
            integer(row["row_id"], 0, "row_id")
            if (row["owner"] not in (0, 1) or row["row_id"] != index or row["pending_status"] != "none"
                    or not last_key < (row["tick"], row["owner"]) or row["tick"] >= payload["gameplay_end_tick"]):
                raise ValueError("row is not an ordered gameplay decision boundary")
            last_key = row["tick"], row["owner"]
            if row["owner"] in previous:
                prior = previous[row["owner"]]
                if prior["outcome"]["tick"] != row["tick"] or prior["outcome"]["reason"] == "terminal_expired":
                    raise ValueError("missing decision boundary or unresolved prior action")
            frame = unpack(row["frame"])
            if not isinstance(frame, UniversalSemanticBatchV4) or frame.batch_size != 1:
                raise ValueError("exact single FAIR semantic frame required")
            validate_frame_tensors(frame)
            masks = row["legality"]
            exact_fields(masks, {"card_ids", "plays", "wait"}, "legal masks")
            if (not isinstance(masks["plays"], torch.Tensor) or masks["plays"].shape != (8, 576, 31)
                    or set(masks["card_ids"]) != set(codec.card_ids)):
                raise ValueError("legal masks differ from stable deck vocabulary")
            if bool(masks["plays"][..., 0].any()) or masks["wait"] is not True:
                raise ValueError("reserved zero delay or missing WAIT")
            legality = LegalPrefixes(masks["card_ids"], masks["plays"], wait=masks["wait"])
            info = DecisionInformation(row["owner"], row["tick"], frame, legality)
            if row["fair_sha"] != info.sha256:
                raise ValueError("FAIR frame or mask identity differs")
            controller = "controller_blue_sha" if info.owner == 0 else "controller_red_sha"
            if row["controller_sha"] != payload["identity"][controller] or row["wdl"] != result["labels"][info.owner]:
                raise ValueError("row controller or player-relative WDL differs")
            action = codec.decode(row["behavior_action_id"])
            if not legality.legal(action):
                raise ValueError("illegal behavior action")
            outcome = row["outcome"]
            exact_fields(outcome, {"reservation", "owner", "tick", "reason"}, "action outcome")
            integer(outcome["tick"], 1, "outcome tick")
            due = info.tick + (60 if action.card_id is None else 2 * action.delay_slot)
            reason = outcome["reason"]
            if (outcome["reservation"] != index or outcome["owner"] != info.owner
                    or reason not in ("executed", "wait_expired", "preempted", "terminal_expired")
                    or type(outcome["reservation"]) is not int or type(outcome["owner"]) is not int
                    or not info.tick < outcome["tick"] <= min(due, payload["gameplay_end_tick"])
                    or (reason in ("executed", "wait_expired") and outcome["tick"] != due)
                    or (reason in ("preempted", "terminal_expired") and outcome["tick"] >= due)
                    or (reason == "terminal_expired" and outcome["tick"] != payload["gameplay_end_tick"])
                    or (reason == "executed" and action.card_id is None)
                    or (reason == "wait_expired" and action.card_id is not None)):
                raise ValueError("missing or invalid final action attestation")
            mode = row["search_mode"]
            if mode == "NONE":
                if row["search_result"] is not None or row["policy_target"] is not None:
                    raise ValueError("direct decisions cannot manufacture search targets")
            elif mode in ("FULL", "FAST"):
                visits = validate_search(row["search_result"], info=info, codec=codec, identity=payload["identity"],
                    budget=128 if mode == "FULL" else 16)
                if visits.get(row["behavior_action_id"], 0) <= 0:
                    raise ValueError("behavior action must have a positive root visit")
                expected = [[action_id, count] for action_id, count in sorted(visits.items()) if count > 0]
                if mode == "FULL" and (not isinstance(row["policy_target"], list)
                        or any(not isinstance(entry, list) or len(entry) != 2
                            or any(type(value) is not int for value in entry) for entry in row["policy_target"])):
                    raise ValueError("policy visits must be typed positive integer counts")
                if (mode == "FULL" and row["policy_target"] != expected) or (mode == "FAST" and row["policy_target"] is not None):
                    raise ValueError("FULL/Fast target differs from this row's root visits")
            else:
                raise ValueError("unknown search mode")
            previous[info.owner] = row
        if set(previous) != {0, 1} or any(row["outcome"]["reason"] not in ("terminal_expired", "executed", "wait_expired")
                or row["outcome"]["tick"] != payload["gameplay_end_tick"]
                for row in previous.values()):
            raise ValueError("both player trajectories must reach the terminal boundary")


def validate_frame_tensors(record):
    for field in fields(record):
        value = getattr(record, field.name)
        if isinstance(value, torch.Tensor):
            if not value.ndim or value.shape[0] != 1 or (value.is_floating_point() and value.dtype != torch.float32):
                raise ValueError("FAIR frame must preserve exact single-row FP32 tensors")
        elif hasattr(type(value), "__dataclass_fields__"):
            validate_frame_tensors(value)
        else:
            raise ValueError("untyped field in FAIR tensor frame")
