import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

from run_combat import PORT, ROOT, SNAPSHOT, upload_sources
from combat_protocol import BRANCH_TICKS, EXECUTION_SEED, MATCH_SEED, THRESHOLDS, sha, validate_selection
from verify_snapshot import SCOPE, validate_digest
from verify_global import LIMITS, validate_state
from native_runner.training.fair_search.contracts import semantic_digest


def valid_sha(value):
    if not isinstance(value, str) or len(value) != 64 or len(bytes.fromhex(value)) != 32:
        raise ValueError("combat semantic identity differs")


def validate_position(state, history):
    tick = state["tick"]
    if type(tick) is not int or tick < 0:
        raise ValueError("combat tick differs")
    validate_digest(state["digest"], tick)
    for name in ("clock_sha", "global_sha"):
        valid_sha(state[name])
    if (state["ready"] != sorted(set(state["ready"])) or not set(state["ready"]) <= {0, 1}
            or set(state["fair_sha"]) != {str(owner) for owner in state["ready"]}
            or set(state["rng_sha"]) != {"0", "1"}):
        raise ValueError("combat frozen owner coverage differs")
    for digest in (*state["fair_sha"].values(), *state["rng_sha"].values()):
        valid_sha(digest)
    capture = dict(schema="fair-final-decision-clock.v1", tick=tick, next_identity=state["next_identity"],
        pending=state["pending"], ready=state["ready"], evidence=history)
    if semantic_digest(capture) != state["clock_sha"]:
        raise ValueError("combat complete reservation history differs")
    for group in ("towers", "entities"):
        entities = state["public"][group]
        if len({item["entity_id"] for item in entities}) != len(entities):
            raise ValueError("combat duplicate public entity")
        for item in entities:
            if item["owner"] not in (0, 1):
                raise ValueError("combat public owner differs")
            for field in ("hp", "max_hp"):
                value = item[field]
                if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or value < 0):
                    raise ValueError("combat public HP differs")


def validate_steps(opening, steps, history):
    previous, history = opening, deepcopy(history)
    validate_position(previous, history)
    for step in steps:
        after = step["after"]
        if after["tick"] != previous["tick"] + 1:
            raise ValueError("combat continuation skipped a tick")
        if set(step["decision"]) != {str(owner) for owner in previous["ready"]}:
            raise ValueError("combat simultaneous frozen decision missing")
        pending = {plan["owner"]: deepcopy(plan) for plan in previous["pending"]}
        identity = previous["next_identity"]
        expected = []
        for owner in previous["ready"]:
            choice = step["decision"][str(owner)]
            validate_selection(choice)
            if choice["fair_sha"] != previous["fair_sha"][str(owner)] or owner in pending:
                raise ValueError("combat decision information identity differs")
            action = choice["action"]
            duration = 60 if action["card_id"] is None else action["delay_slot"] * 2
            plan = dict(identity=identity, owner=owner, action=action,
                created_tick=previous["tick"], due_tick=previous["tick"] + duration)
            identity += 1
            pending[owner] = plan
            expected.append(plan)
        if step["committed"] != expected or after["next_identity"] != identity:
            raise ValueError("combat simultaneous reservation identity differs")
        due = sorted((plan for plan in pending.values() if plan["due_tick"] == after["tick"]),
            key=lambda plan: (plan["due_tick"], plan["owner"]))
        outcomes = {str(plan["identity"]): "executed" for plan in due if plan["action"]["card_id"] is not None}
        if step["outcomes"] != outcomes:
            raise ValueError("combat execution confirmation differs")
        evidence = []
        owners = {0, 1} if outcomes else set()
        for plan in due:
            evidence.append(dict(reservation=plan["identity"], owner=plan["owner"], tick=after["tick"],
                reason="wait_expired" if plan["action"]["card_id"] is None else "executed"))
            owners.add(plan["owner"])
            del pending[plan["owner"]]
        for owner in sorted(owners):
            if owner in pending:
                plan = pending.pop(owner)
                evidence.append(dict(reservation=plan["identity"], owner=owner, tick=after["tick"], reason="preempted"))
        if step["evidence"] != evidence or after["ready"] != sorted(owners) or after["pending"] != list(pending.values()):
            raise ValueError("combat cancellation or decision boundary differs")
        if after["rng_sha"] != previous["rng_sha"]:
            raise ValueError("deterministic combat probe consumed execution RNG")
        history.extend(evidence)
        validate_position(after, history)
        previous = after
    return previous, history


def validate_probe(report, process, *, second_process, engine_sha):
    if (report["schema"] != "fair-final-combat-snapshot-probe.v1"
            or report["status"] != "completed_limited_scope" or report["second_process"] is not second_process
            or report["engine_sha256"] != engine_sha or report["global_monitor_readonly"] is not True
            or report["match_seed"] != MATCH_SEED or report["execution_seed"] != EXECUTION_SEED
            or any(report[name] is not False for name in LIMITS)):
        raise ValueError("combat probe scope differs")
    opening = report["opening"]
    if (opening["tick"] != 90 or opening["ready"] != [0, 1] or opening["pending"]
            or opening["next_identity"] != 0):
        raise ValueError("combat opening differs")
    trajectory = report["trajectory"]
    if len(trajectory) != THRESHOLDS[-1] - opening["tick"]:
        raise ValueError("combat main trajectory incomplete")
    final, _ = validate_steps(opening, trajectory, [])
    if final["tick"] != THRESHOLDS[-1] or [row["tick"] for row in report["records"]] != list(THRESHOLDS):
        raise ValueError("combat registered boundary coverage differs")
    history, observed = [], [opening]
    for step in trajectory:
        history.extend(step["evidence"])
        observed.append(step["after"])
    for row in report["records"]:
        tick = row["tick"]
        root = trajectory[tick - opening["tick"] - 1]["after"]
        prefix = [event for step in trajectory[:tick - opening["tick"]] for event in step["evidence"]]
        original = bytes.fromhex(row["global_hex"])
        validate_state(row["census"], original, process, tick=tick)
        if row["root"] != root or row["root"]["global_sha"] != row["census"]["state"]["sha256"]:
            raise ValueError("combat root differs from main trajectory")
        branches = row["branches"]
        if (len(branches) != 2 or any(len(branch) != BRANCH_TICKS for branch in branches)
                or branches[0] != branches[1] or row["restored"] != [root, root]
                or len(row["restored_censuses"]) != 2 or row["snapshot_released"] is not True):
            raise ValueError("combat repeated continuation or restoration differs")
        epoch = row["census"]["stateEpoch"]
        for branch, census in zip(branches, row["restored_censuses"]):
            validate_steps(root, branch, prefix)
            validate_state(census, original, process, tick=tick)
            if census["generation"] != row["census"]["generation"] or census["stateEpoch"] <= epoch:
                raise ValueError("combat snapshot generation or epoch differs")
            epoch = census["stateEpoch"]
            observed.extend(step["after"] for step in branch)
    executed = {owner: sum(event["reason"] == "executed" and event["owner"] == owner for event in history)
        for owner in (0, 1)}
    entities = sum(bool(state["public"]["entities"]) for state in observed)
    damaged = sum(any(item["hp"] is not None and item["max_hp"] is not None and item["hp"] < item["max_hp"]
        for group in ("towers", "entities") for item in state["public"][group]) for state in observed)
    if not all(executed.values()) or not entities or not damaged:
        raise ValueError("combat live ACT, entity or HP-loss evidence missing")
    changes = sum(previous["global_sha"] != step["after"]["global_sha"] for previous, step in
        zip([opening, *(step["after"] for step in trajectory[:-1])], trajectory))
    return dict(executed_by_owner=executed, public_entity_frames=entities, damaged_frames=damaged,
        main_trajectory_global_changes=changes, preemptions=sum(event["reason"] == "preempted" for event in history))


def validate(attempt, launch_sha, receipt_sha):
    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "download.json") != receipt_sha:
        raise ValueError("combat external identities differ")
    launch = json.loads((attempt / "launch.json").read_text())
    receipt = json.loads((attempt / "download.json").read_text())
    if (launch["schema"] != "fair-final-combat-snapshot-launch.v1"
            or receipt["schema"] != "fair-final-combat-snapshot-download.v1"
            or receipt["launch_sha256"] != launch_sha or receipt["source_worker_stopped"] is not True
            or any(record[name] is not False for record in (launch, receipt)
                for name in ("training_admissible", "gates_closed"))):
        raise ValueError("combat archive scope differs")
    for name, field in (("run_combat.py", "runner_sha256"), ("verify_combat.py", "verifier_sha256"),
            ("CONTRACT.md", "contract_sha256")):
        if sha(ROOT / name) != launch[field]:
            raise ValueError("combat local implementation differs")
    build = launch["build_manifest"]
    parent = launch["parent_verification"]
    if (build["schema"] != "fair-final-global-rng-snapshot-build.v1"
            or hashlib.sha256((json.dumps(build, indent=2) + "\n").encode()).hexdigest() != launch["build_manifest_sha256"]
            or build["builder_sha256"] != sha(SNAPSHOT / "build_engine.py")
            or build["module_sha256"] != sha(SNAPSHOT / "global_snapshot.inc")
            or build["artifacts"]["probe/global_snapshot.inc"] != build["module_sha256"]
            or build["artifacts"]["libcrprobe.so"] != build["engine_sha256"]
            or any(build[name] is not False for name in ("training_admissible", "gates_closed"))
            or parent["status"] != "verified_limited_scope" or parent["digest_scope"] != SCOPE
            or parent["launch_sha256"] != launch["parent_launch_sha256"]
            or parent["receipt_sha256"] != launch["parent_receipt_sha256"]
            or parent["engine_sha256"] != build["engine_sha256"]
            or any(parent[name] is not False for name in LIMITS)):
        raise ValueError("combat parent build or acceptance differs")
    archive = attempt / "archive"
    required = {*launch["uploaded_sha256"], "worker.log"}
    allowed = set(required)
    for variant in ("immediate", "delayed"):
        required.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json", "engine/process.json"))
        allowed.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json",
            "engine/process.json", "probe.log", "engine.log"))
    actual = {str(path.relative_to(archive)) for path in archive.rglob("*") if path.is_file()}
    if (not required.issubset(receipt["files"]) or not set(receipt["files"]).issubset(allowed)
            or actual != set(receipt["files"])):
        raise ValueError("combat archive missing evidence or contains failures")
    for name, expected in receipt["files"].items():
        path = archive / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or sha(path) != expected:
            raise ValueError("combat archived bytes differ")
    sources = {name: hashlib.sha256(content).hexdigest() for name, content in upload_sources().items()}
    if launch["uploaded_sha256"] != {**sources, "libcrprobe.so": build["engine_sha256"],
            "settings.json": receipt["files"]["settings.json"]}:
        raise ValueError("combat frozen source identity differs")
    if any(receipt["files"].get(name) != expected for name, expected in launch["uploaded_sha256"].items()):
        raise ValueError("combat upload differs from archive")
    settings = json.loads((archive / "settings.json").read_text())
    if (settings != launch["settings"] or settings["port"] != PORT
            or settings["engine_sha256"] != build["engine_sha256"] or settings["source_sha256"] != sources):
        raise ValueError("combat runtime settings differ")
    processes, results = [], []
    for variant in ("immediate", "delayed"):
        folder = archive / variant
        cleanup = json.loads((folder / "cleanup.json").read_text())
        process = json.loads((folder / "engine/process.json").read_text())
        if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
                or cleanup["listening"] is not False or cleanup["pids_alive"] != dict(pid=False, child_pid=False)
                or cleanup["port"] != PORT or cleanup["process"] != process or process["phase"] != "stopped"
                or process["phase_snapshot_engine"]["path"] != settings["probe"]
                or process["phase_snapshot_engine"]["sha256"] != build["engine_sha256"]):
            raise ValueError("combat cleanup or process ownership differs")
        report = json.loads((folder / "report.json").read_text())
        if (report["source_sha256"] != launch["frozen_runtime_sha256"]
                or json.loads((folder / "progress.json").read_text()) != dict(
                    records=report["records"], trajectory_ticks=len(report["trajectory"]))):
            raise ValueError("combat runtime or progress differs")
        results.append(validate_probe(report, process, second_process=variant == "delayed", engine_sha=build["engine_sha256"]))
        processes.append((process["child_pid"], process["child_start_ticks"]))
    if processes[0] == processes[1]:
        raise ValueError("combat second native process reused")
    return dict(schema="fair-final-combat-snapshot-verification.v1", status="verified_limited_scope",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, engine_sha256=build["engine_sha256"],
        archived_files=len(receipt["files"]), native_processes=2, tested_boundaries=6,
        main_trajectory_ticks_per_process=1110, repeated_combat_ticks_per_branch=BRANCH_TICKS,
        digest_scope=SCOPE, process_results=results, fixed_hog_usage_proven=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    arguments = parser.parse_args()
    result = validate(arguments.attempt, arguments.launch_sha, arguments.receipt_sha)
    with (arguments.attempt / "verification.json").open("x") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
