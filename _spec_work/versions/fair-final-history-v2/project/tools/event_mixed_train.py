"""Mixed-deck PPO with explicit budgets and optional native evaluation."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.training.event_opponents import checkpoint_records
from tools.event_mixed_evaluator import MixedEvaluator, write_json
from tools.event_rl_persistent_fleet import PersistentFleet
from tools.event_rl_persistent_learner import PersistentLearner
from tools.event_rl_round_validation import validate_round


def completed_rounds(root, config):
    records = []
    expected = config["start_iteration"] + 1
    parent_sha = config["initial_checkpoint_sha256"]
    for path in sorted(root.glob("iteration-*/success.json")):
        record = json.loads(path.read_text())
        if (record["iteration"] != expected or path.parent.name != f"iteration-{expected:06d}"
                or record["mixed_round"] != expected - config["start_iteration"]
                or record["parent_checkpoint_sha256"] != parent_sha):
            raise ValueError("mixed-deck resume needs contiguous completed rounds and parent identities")
        weight = Path(record["checkpoint"])
        if weight.parent != root / "weights" or hashlib.sha256(weight.read_bytes()).hexdigest() != weight.stem:
            raise ValueError("mixed-deck resume checkpoint integrity mismatch")
        records.append(record)
        parent_sha, expected = weight.stem, expected + 1
    return records


def validate_run_config(config):
    from native_runner.training.mixed_decks import RECRUITS_HOGS_POLICY, deck_ids
    mixed = config["mixed_opponents"]
    deck_ids(mixed.get("deck_policy"))
    if (mixed["version"] != "mixed-deck.v1"
            or config["start_iteration"] != mixed["start_iteration"]):
        raise ValueError("mixed-deck run configuration mismatch")
    if config.get("training_mode", "bounded") == "continuous":
        if (any(config.get(k) is not None for k in ("additional_iterations", "target_iteration", "duration_seconds"))
                or mixed.get("deck_policy") != RECRUITS_HOGS_POLICY):
            raise ValueError("continuous mixed-deck run requires an unbounded curriculum and no limits")
        limit = None
    elif config.get("training_mode", "bounded") == "bounded":
        if config["additional_iterations"] != 100:
            raise ValueError("mixed-deck bounded run requires 100 updates")
        limit = 100
    else:
        raise ValueError("unknown mixed-deck training mode")
    return limit, config.get("evaluation", {}).get("enabled", True)


def archive_checkpoint(fleet, weight):
    archive = fleet.FLEET["37"]["run"] + "/weights/" + weight.name
    fleet.transport("37", str(weight), archive)
    check = "import pathlib,hashlib; p=pathlib.Path(" + repr(archive) + "); assert hashlib.sha256(p.read_bytes()).hexdigest()==" + repr(weight.stem)
    fleet.ssh("37", "python3 -c " + shlex.quote(check), timeout=60)
    return archive


def recover_pending(fleet, iteration):
    """Keep interrupted data for audit; recollect from the last committed round."""
    if not (fleet.ROOT / f"iteration-{iteration:06d}").exists():
        return
    stamp = str(time.time_ns())
    for host, config in fleet.FLEET.items():
        script = (
            "from pathlib import Path; r=Path(" + repr(config["run"]) + "); "
            "p=r/" + repr(f"iteration-{iteration:06d}") + "; "
            "a=r/'interrupted-rounds'; a.mkdir(exist_ok=True); "
            "assert not (p/'success.json').exists(); "
            "p.rename(a/" + repr(f"iteration-{iteration:06d}-{stamp}") + ") if p.exists() else None"
        )
        fleet.ssh(host, "python3 -c " + shlex.quote(script), timeout=30)
    fleet.event("pending-round-preserved", iteration=iteration, audit_suffix=stamp)


def prune_inference_caches(fleet, collector, learner):
    """Remove only verified, reconstructible artifacts produced by this run."""
    path = fleet.ROOT / "inference-assets.json"
    known = json.loads(path.read_text()) if path.exists() else {}
    descriptors = [collector.descriptor, learner.descriptor, *collector.histories.values()]
    for descriptor in descriptors:
        known[descriptor["artifact_sha256"]] = descriptor["source_checkpoint_sha256"]
    write_json(path, known)
    keep = {d["artifact_sha256"] for d in descriptors}
    archive = fleet.FLEET["37"]["run"]
    old = {artifact: source for artifact, source in known.items() if artifact not in keep}
    if not old:
        return
    check = """from pathlib import Path
import hashlib
r=Path(ROOT)
for sha in SOURCES:
 p=r/'weights'/(sha+'.pt')
 assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==sha
""".replace("ROOT", repr(archive)).replace("SOURCES", repr(sorted(set(old.values()))))
    fleet.ssh("37", "python3 -c " + shlex.quote(check), timeout=120)
    for host, config in fleet.FLEET.items():
        cleanup = """from pathlib import Path
import hashlib,json
r=Path(ROOT); old=ARTIFACTS; removed=[]
for folder in FOLDERS:
 for sha in old:
  assert len(sha)==64 and all(c in '0123456789abcdef' for c in sha)
  p=r/folder/(sha+'.pt')
  if not p.exists(): continue
  assert not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==sha
  p.unlink(); removed.append(str(p))
print(json.dumps({'removed':removed,'preserved_complete_weights':True}))
""".replace("ROOT", repr(config["run"])).replace("ARTIFACTS", repr(old)).replace(
            "FOLDERS", repr(("collector-weights",) if host == "37" else ("collector-weights", "history-inference")))
        response = fleet.ssh(host, "python3 -c " + shlex.quote(cleanup), capture_output=True, text=True, timeout=60)
        with (fleet.ROOT / "cache-audit.jsonl").open("a") as stream:
            stream.write(json.dumps(dict(host=host, **json.loads(response.stdout))) + "\n")
    # An artifact no longer needed can be re-exported from its immutable complete checkpoint.
    write_json(path, {k: v for k, v in known.items() if k in keep})


def finalize(fleet, evaluator, records, terminal):
    root = fleet.ROOT
    baseline = fleet.CFG["initial_checkpoint_sha256"]
    candidates = []
    for path in sorted((root / "evaluations").glob("round-*.json")):
        result = json.loads(path.read_text())
        if result["protection"]["passed"]:
            candidates.append(result)
    best = max(candidates, key=lambda r: r["report"]["equal_deck_average"]["score_rate"], default=None)
    best_sha = baseline if best is None else best["checkpoint_sha256"]
    final_sha = baseline if not records else Path(records[-1]["checkpoint"]).stem
    write_json(root / "selected-weights.json", dict(B0=baseline, best_mixed=best_sha, final=final_sha,
        best_round=0 if best is None else best["mixed_round"], terminal=terminal))
    independent = evaluator.compare_independent(best_sha)
    heldout = evaluator.compare_independent(final_sha, variants=True)
    intermediate = [r for r in candidates if r["mixed_round"] < len(records)]
    best_middle = max(intermediate, key=lambda r: r["report"]["equal_deck_average"]["score_rate"], default=None)
    opponents = [Path(r["checkpoint"]).stem for r in records if r["mixed_round"] in (20, 50)]
    if best_middle:
        opponents.append(best_middle["checkpoint_sha256"])
    direct = evaluator.final_head_to_head(final_sha, opponents)
    report = dict(terminal=terminal, completed_updates=len(records), B0=baseline, best_mixed=best_sha,
                  final=final_sha, independent_selection=independent, heldout=heldout,
                  final_head_to_head=direct,
                  unavailable_intermediate_rounds=[n for n in (20, 50) if n > len(records)])
    write_json(root / "final-report.json", report)
    lines = ["# Four-deck RL report", "", f"Completed PPO updates: {len(records)} / 100", "",
             f"Stop reason: {terminal['reason']}", f"B0: `{baseline}`", f"Best protected model: `{best_sha}`",
             f"Final model: `{final_sha}`", "", "Scores: (wins + 0.5 * draws) / games.",
             "Intervals resample whole swapped seed pairs within each matchup.", "",
             "| Evaluation | Equal-deck score | Delta vs B0 | 95% paired interval | Status |",
             "|---|---:|---:|---|---|"]
    for name, value in (("Independent best model", independent), ("Unseen combinations (final)", heldout)):
        avg = value["equal_deck_average"]
        lines.append(f"| {name} | {avg['score_rate']:.2%} | {avg['delta']:+.2%} | {avg['delta_ci95']} | {value['improvement_status']} |")
    lines += ["", "Complete matchup matrices and weight identities are in final-report.json.",
              "B0 on new decks is an initialization baseline, not a mature deck specialist."]
    (root / "final-report.md").write_text("\n".join(lines) + "\n")
    archive = fleet.FLEET["37"]["run"] + "/reports/"
    fleet.ssh("37", "mkdir -p " + shlex.quote(archive), timeout=30)
    for name in ("final-report.json", "final-report.md", "selected-weights.json"):
        fleet.transport("37", str(root / name), archive + name)
    fleet.event("completed", completed_updates=len(records), best_mixed=best_sha,
                final=final_sha, report=str(root / "final-report.json"))


def run(root):
    spec = importlib.util.spec_from_file_location("fleet_runtime", root / "fleet_runtime.py")
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    config = fleet.CFG
    limit, evaluation_enabled = validate_run_config(config)
    locks = []
    for path in (Path(config["parent_run"]) / "orchestrator.lock", root / "orchestrator.lock"):
        stream = path.open("a")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(stream)
    (root / "orchestrator.pid").write_text(str(os.getpid()))
    initial = root / "weights" / (config["initial_checkpoint_sha256"] + ".pt")
    if hashlib.sha256(initial.read_bytes()).hexdigest() != initial.stem:
        raise ValueError("B0 SHA mismatch")
    records = completed_rounds(root, config)
    weight = initial if not records else Path(records[-1]["checkpoint"])
    collector, learner = PersistentFleet(fleet), None
    try:
        # Initialization preserves AdamW counters; the new reference controller starts at .01.
        learner = PersistentLearner(fleet, weight)
        evaluator = None
        if evaluation_enabled:
            evaluator = MixedEvaluator(collector, learner.current["contract_sha256"])
            evaluator.smoke_and_baseline()
        terminal_path = root / "terminal-reason.json"
        terminal = json.loads(terminal_path.read_text()) if terminal_path.exists() else None
        while terminal is None:
            fleet.stop_check("complete-round-boundary")
            round_number = len(records)
            if evaluator is not None and round_number and round_number % 10 == 0:
                review = evaluator.evaluate(weight.stem, round_number)
                if review["protection"]["passed"]:
                    write_json(root / "last-qualified.json", dict(mixed_round=round_number, checkpoint_sha256=weight.stem))
                if review["protection"]["stop"]:
                    terminal = dict(reason="golem-protection-stop", mixed_round=round_number, protection=review["protection"])
            if terminal is None and limit is not None and round_number >= limit:
                terminal = dict(reason="100-updates-completed", mixed_round=round_number)
            if terminal is not None:
                write_json(terminal_path, terminal)
                break
            fleet.stop_check("complete-round-boundary")
            iteration = config["start_iteration"] + round_number + 1
            if shutil.disk_usage(root).free < 3_000_000_000:
                raise RuntimeError("mixed-deck learner disk reserve reached")
            recover_pending(fleet, iteration)
            fleet.event("iteration-start", iteration=iteration, mixed_round=round_number + 1, checkpoint=str(weight),
                        deck_policy=config["mixed_opponents"].get("deck_policy"),
                        training_mode=config.get("training_mode", "bounded"), target_iteration=config.get("target_iteration"),
                        evaluation_enabled=evaluation_enabled)
            collector.prepare_iteration(weight, learner.prepare_iteration(weight), iteration=iteration)
            with concurrent.futures.ThreadPoolExecutor(len(fleet.FLEET)) as pool:
                futures = [pool.submit(collector.rollout_host, h, iteration, weight) for h in fleet.FLEET]
                for future in futures:
                    future.result()
            directory = root / f"iteration-{iteration:06d}"
            cache = directory / "collected"
            games = collector.plan["expected_games"]
            validation = validate_round(cache, games, weight.stem)
            write_json(directory / "native-validation.json", validation)
            fleet.event("update-start", iteration=iteration, mixed_round=round_number + 1)
            child = learner.update(cache, weight, directory / "updated.pt", games, directory / "update.log")
            archive = archive_checkpoint(fleet, child)
            record = dict(iteration=iteration, mixed_round=round_number + 1, checkpoint=str(child),
                          parent_checkpoint_sha256=weight.stem, archive_host="37", archive_checkpoint=archive,
                          plan_sha256=collector.plan["sha256"], phase=collector.plan["phase"],
                          learning_weights=collector.plan["learning_weights"], shards=games)
            if "allocation_policy" in collector.plan:
                record["allocation_policy"] = collector.plan["allocation_policy"]
            if "deck_policy" in collector.plan:
                record["deck_policy"] = collector.plan["deck_policy"]
            if "reward_config" in collector.plan:
                record["reward_config"] = collector.plan["reward_config"]
            write_json(directory / "success.json", record)
            if "reward_config" in collector.plan:
                from native_runner.training.event_reward_stats import recent_overflow_summary
                write_json(root / "overflow-recent-10.json",
                           recent_overflow_summary(root, collector.plan["reward_config"]))
            # Full rollout audit remains on the learner; .37 also receives each plan and validation report.
            remote = fleet.FLEET["37"]["run"] + f"/learner-audit/iteration-{iteration:06d}/"
            fleet.ssh("37", "mkdir -p " + shlex.quote(remote), timeout=30)
            for name in ("success.json", "match-plan.json", "native-validation.json", "update.log"):
                fleet.transport("37", str(directory / name), remote + name)
            records.append(record)
            weight = child
            prune_inference_caches(fleet, collector, learner)
            fleet.event("iteration-success", **record)
        learner.close()
        learner = None
        if evaluator is not None:
            finalize(fleet, evaluator, records, terminal)
        else:
            fleet.event("completed", completed_updates=len(records), terminal=terminal,
                        checkpoint=str(weight), evaluation_enabled=False)
    except SystemExit:
        raise
    except BaseException:
        fleet.event("fatal", traceback=traceback.format_exc())
        raise
    finally:
        if learner:
            learner.close()
        collector.close()
        for stream in locks:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    args = parser.parse_args()
    run(args.run_root.resolve())


if __name__ == "__main__":
    main()
