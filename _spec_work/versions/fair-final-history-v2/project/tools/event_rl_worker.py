"""One persistent, sequential collector attached to one native engine port."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from native_runner.training.event_inference import InferenceModel
from tools import event_rl_rollout as rollout


class Collector:
    def __init__(self, port):
        self.port = port
        self.weights = InferenceModel()
        self.opponent_weights = InferenceModel()
        self.environment = None
        self.coordinator = None
        self.games = 0
        self.inference_client = None
        self.opponent_inference_client = None
        self.capture_mode = None

    def collect(self, task):
        start = time.perf_counter()
        seed = task["seed"]
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise ValueError("invalid match seed")
        if task["port"] != self.port:
            raise ValueError("worker port mismatch")
        mixed = task.get("mixed_opponents")
        history = mixed is not None and mixed["kind"] != "self"
        learner_owner = mixed["learner_owner"] if history else 0
        if mixed is not None:
            if mixed["seed"] != seed or mixed["port"] != self.port:
                raise ValueError("worker mixed match identity mismatch")
            if mixed["owner_sha256"][learner_owner] != task["weights"]["source_checkpoint_sha256"]:
                raise ValueError("worker learner identity mismatch")
        opponent = task.get("opponent_weights")
        if history and (opponent is None or opponent["source_checkpoint_sha256"] != mixed["opponent"]["sha256"]
                        or opponent["contract_sha256"] != task["weights"]["contract_sha256"]):
            raise ValueError("worker historical model identity mismatch")
        out = Path(task["out"])
        out.mkdir(parents=True, exist_ok=False)
        with (out / "collector.log").open("x") as log, contextlib.redirect_stdout(log):
            if task.get("inference_socket"):
                from native_runner.training.event_inference_service import Client
                if self.inference_client is None:
                    self.inference_client = Client(task["inference_socket"])
                model, policy_version = self.inference_client.start_match(task["weights"], seed)
            else:
                if self.inference_client is not None:
                    raise ValueError("worker inference backend changed")
                model, policy_version = self.weights.load(task["weights"])
            opponent_model = None
            if history:
                if self.inference_client:
                    if self.opponent_inference_client is None:
                        self.opponent_inference_client = Client(task["inference_socket"])
                    opponent_model, _ = self.opponent_inference_client.start_match(opponent, seed)
                else:
                    opponent_model, _ = self.opponent_weights.load(opponent)
            capture_mode = task.get("capture_mode", "json")
            if capture_mode not in {"json", "zlib-json"}:
                raise ValueError("event collector requires complete json or zlib-json telemetry")
            if self.capture_mode is not None and self.capture_mode != capture_mode:
                raise ValueError("worker capture mode changed")
            self.capture_mode = capture_mode
            loaded_at = time.perf_counter()
            if self.environment is None:
                bundle = rollout.production_semantic_bundle()
                proxies, self.coordinator = rollout.build_resident_training_proxies_v4(
                    (0,), host="127.0.0.1", port=self.port, timeout=90.0, step_until_event=True,
                    capture_mode=capture_mode)
                self.environment = rollout.BattleEnvV1(
                    native=proxies[0], card_catalog=bundle.native_card_catalog,
                    semantic_subset_contract=bundle.semantic_subset_contract,
                    effect_catalog=rollout.RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
                    ruleset_id=hashlib.sha256(b"event-persistent-collector.v1:tournament-11-base-princess").hexdigest(),
                    terminal_margin_beta=0.05)
            # Construction/loading must not influence sampling, including after a
            # worker restart. Every job has its own deterministic sampling seed.
            torch.manual_seed(seed)
            deck = tuple(rollout._deck_registry()["golem-beatdown"])
            decks = {0: deck, 1: deck}
            if mixed is not None and mixed["version"] in ("mixed-deck.v1", "mixed-deck-eval.v1"):
                from native_runner.training.mixed_decks import DECKS, VARIANTS, RULES
                if mixed["rules"] != RULES:
                    raise ValueError("mixed-deck rules mismatch")
                decks = {o: tuple(mixed["owner_decks"][o]) for o in (0, 1)}
                registry = {**DECKS, **(VARIANTS if task.get("evaluation") else {})}
                if any(decks[o] != registry[mixed["owner_deck_ids"][o]] for o in (0, 1)):
                    raise ValueError("mixed-deck worker deck mismatch")
            match_id = f"{policy_version}-{seed:08d}"
            if mixed is not None:
                match_id = mixed["game_id"]
            descriptor = task["weights"]
            recorder, counters = rollout.play_match(
                environment=self.environment, coordinator=self.coordinator, model=model,
                opponent_model=opponent_model, learner_owner=learner_owner,
                opponent_inference_client=self.opponent_inference_client if history else None,
                event_config=model.event_config, catalog=model.backbone.catalog,
                device=torch.device("cpu"), decks=decks, match_id=match_id,
                policy_version=policy_version, seed=seed, env_id=0, temperatures=(1., 1., 1., 1.),
                max_ticks=7200, max_seconds=1800., max_advance_ticks=30, max_wait_ticks=300,
                # Use the same rebind priming path on cold and warm jobs.
                first_match=False, behavior_checkpoint_sha256=descriptor["source_checkpoint_sha256"],
                behavior_model_contract_hash=descriptor["contract_sha256"],
                inference_client=self.inference_client,
                sampling_generator=(torch.Generator().manual_seed(seed)
                                    if task.get("sampling_protocol") == "per-match-cpu-uniform4.v2" else None),
                defer_observations=task.get("defer_observations", False),
                reward_config=None if mixed is None else mixed.get("reward_config"))
            if not counters["terminal"] or counters["act_violation"]:
                raise RuntimeError("collector requires a complete legal match")
            if mixed is not None and mixed["version"] in ("mixed-deck.v1", "mixed-deck-eval.v1"):
                if counters["action_constraints"] != mixed["action_constraints"]:
                    raise ValueError("mixed-deck executed action contract mismatch")
                if any(not recorder.rows[o] or not recorder.rows[o][-1].done or
                       any(row.truncated for row in recorder.rows[o])
                       for o in ([learner_owner] if history else [0, 1])):
                    raise ValueError("mixed-deck match did not reach a true terminal state")
            if task.get("evaluation"):
                from native_runner.training.event_config import EVENT_DELAY_OFFSETS_MS
                from tools.event_rl_round_validation import ERROR_COUNTERS
                if (any(counters[k] for k in ERROR_COUNTERS) or counters["executed"] != counters["confirmed"]
                        or counters["submitted"] != counters["confirmed"] + counters["cancelled_native_terminal"]):
                    raise ValueError("evaluation native execution receipts did not reconcile")
                rows = recorder.rows[learner_owner]
                delays = [EVENT_DELAY_OFFSETS_MS[r.delay_bin] for r in rows if r.act]
                if any(d < 100 for d in delays) or any(r.execution_status == "legacy_unknown" for r in rows):
                    raise ValueError("evaluation delay/execution audit failed")
                reward = float(counters["results"][learner_owner])
                score = 1. if reward > .5 else 0. if reward < -.5 else .5
                evaluation = dict(spec=task["evaluation"], score=score, reward=reward,
                    true_terminal=True, minimum_selected_delay_ms=min(delays, default=None),
                    execution_status_counts={s: sum(r.execution_status == s for r in rows)
                                             for s in {r.execution_status for r in rows}}, counters=counters)
                (out / "evaluation.json").write_text(json.dumps(evaluation))
                self.games += 1
                return dict(port=self.port, success=True, seed=seed, out=str(out),
                    source_checkpoint_sha256=descriptor["source_checkpoint_sha256"],
                    mixed_opponents=mixed, evaluation=evaluation)
            contract = rollout.default_event_contract(model.event_config)
            contract.update(policy_version=policy_version, temperatures=[1., 1., 1., 1.],
                            behavior_checkpoint_sha256=descriptor["source_checkpoint_sha256"],
                            behavior_model_contract_hash=descriptor["contract_sha256"])
            if mixed is not None:
                contract["mixed_opponents"] = mixed
            recorder.to_shard(out / "part-00-shard-000000.crev.zst",
                              shard_id="part-00-shard-000000.crev", contract=contract,
                              owner=learner_owner if history else None)
            self.games += 1
            counters.update(match=match_id, rows=sum(recorder.lane_count(o) for o in (0, 1)),
                            source_checkpoint_sha256=descriptor["source_checkpoint_sha256"],
                            inference_artifact_sha256=descriptor["artifact_sha256"],
                            worker_pid=os.getpid(), worker_games=self.games,
                            load_seconds=round(loaded_at-start, 4),
                            job_seconds=round(time.perf_counter()-start, 4),
                            capture_mode=capture_mode,
                            deferred_observations=task.get("defer_observations", False),
                            sampling_protocol=("per-match-cpu-uniform4.v2" if self.inference_client or task.get("sampling_protocol") == "per-match-cpu-uniform4.v2" else
                                               "seed-after-load-prime-every-match.v1"))
            if self.inference_client:
                counters["inference_service"] = self.inference_client.profile
            if mixed is not None:
                counters["mixed_opponents"] = mixed
            if history and self.opponent_inference_client:
                counters["opponent_inference_service"] = self.opponent_inference_client.profile
            print(json.dumps(counters), flush=True)
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
        return dict(port=self.port, success=True, seed=seed, out=str(out), files=files,
                    mixed_opponents=mixed,
                    worker_pid=os.getpid(), worker_games=self.games,
                    source_checkpoint_sha256=descriptor["source_checkpoint_sha256"],
                    load_seconds=counters["load_seconds"], job_seconds=counters["job_seconds"])


def main():
    collector = Collector(int(sys.argv[1]))
    for line in sys.stdin:
        try:
            result = collector.collect(json.loads(line))
            print(json.dumps(result), flush=True)
        except BaseException:
            print(json.dumps({"success": False, "error": traceback.format_exc()}), flush=True)
            return 1  # A failed environment is never reused.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
