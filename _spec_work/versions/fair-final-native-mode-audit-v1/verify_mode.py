import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from audit_mode import active_mode, LIBG_SHA, PROBE_SHA, verify_predicate


ROOT = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_census(record, process):
    expected = active_mode(record["world_type"], record["rules_mode"])
    if (any(record.get(name) != value or type(record.get(name)) is not type(value)
            for name, value in expected.items())
            or any(record.get(name) is not True for name in ("readonly", "world_backlink_matches"))
            or any(record.get(name) is not False for name in ("training_admissible", "gates_closed"))
            or record["child_pid"] != process["child_pid"]
            or record["child_start_ticks"] != process["child_start_ticks"]
            or type(record["tick"]) is not int):
        raise ValueError("native mode census identity or predicate differs")


def semantic(record):
    return {name: value for name, value in record.items() if name not in ("generation", "stateEpoch")}


def validate(directory, launch_sha, receipt_sha):
    static = json.loads((ROOT / "static-report.json").read_text())
    if (static["libg_sha256"] != LIBG_SHA or static["predicate_scope"] != "active-manager-world-only"
            or static["status"] != "passed_limited_scope"):
        raise ValueError("mode static evidence identity differs")
    verify_predicate([SimpleNamespace(address=int(item["address"], 16),
        mnemonic=item["mnemonic"], op_str=item["op_str"]) for item in static["instructions"]])
    if sha(directory / "launch.json") != launch_sha or sha(directory / "download.json") != receipt_sha:
        raise ValueError("mode external evidence identity differs")
    launch = json.loads((directory / "launch.json").read_text())
    receipt = json.loads((directory / "download.json").read_text())
    archive = directory / "archive"
    required = {*launch["uploaded_sha256"], "report.json", "progress.json",
        "cleanup.json", "engine/process.json"}
    if (not required.issubset(receipt["files"])
            or not set(receipt["files"]).issubset(required | {"worker.log", "probe.log", "engine.log"})):
        raise ValueError("mode required archive coverage differs")
    if receipt["launch_sha256"] != launch_sha or receipt["source_worker_stopped"] is not True:
        raise ValueError("mode archive process receipt differs")
    for name, expected in receipt["files"].items():
        if (Path(name).is_absolute() or ".." in Path(name).parts or (archive / name).is_symlink()
                or sha(archive / name) != expected):
            raise ValueError("mode archived artifact identity differs")
    for name, expected in launch["uploaded_sha256"].items():
        if receipt["files"].get(name) != expected:
            raise ValueError("mode uploaded source or settings differ")
    for name in ("audit_mode.py", "probe_mode.py", "mode_worker.py"):
        if sha(ROOT / name) != launch["uploaded_sha256"][name]:
            raise ValueError("mode audit source changed")
    settings = json.loads((archive / "settings.json").read_text())
    if settings != launch["settings"] or settings["port"] != 29773 or settings["engine_sha256"] != PROBE_SHA:
        raise ValueError("mode native settings differ")
    if (archive / "failure.json").exists():
        raise ValueError("mode native failure retained; verification rejected")
    cleanup = json.loads((archive / "cleanup.json").read_text())
    process = json.loads((archive / "engine/process.json").read_text())
    if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
            or cleanup["listening"] is not False or cleanup["pids_alive"] != dict(pid=False, child_pid=False)
            or cleanup["port"] != settings["port"] or process != cleanup["process"]
            or process["phase"] != "stopped" or process["phase_snapshot_engine"]["sha256"] != PROBE_SHA
            or process["phase_snapshot_engine"]["path"] != settings["probe"]):
        raise ValueError("mode owned engine cleanup or identity differs")
    report = json.loads((archive / "report.json").read_text())
    if (report["schema"] != "fair-final-native-mode-probe.v1" or report["status"] != "passed_limited_scope"
            or report["source_sha256"] != launch["frozen_runtime_sha256"] or report["behavior"] != "legal-WAIT-only"
            or any(report[name] is not False for name in ("actual_policy_search_verified",
                "legal_rejection_reachability_proven", "training_admissible", "gates_closed"))):
        raise ValueError("mode report scope or source identity differs")
    records = report["records"]
    if [row["threshold"] for row in records] != [90, 1200, 3600, 5400]:
        raise ValueError("mode native phase coverage differs")
    if json.loads((archive / "progress.json").read_text()) != dict(records=records):
        raise ValueError("mode native progress differs")
    for row in records:
        before, restored, continuation = row["census"], row["restored"], row["continuations"]
        if (before["tick"] != row["threshold"] or semantic(before) != semantic(restored)
                or row["digest"] != row["restored_digest"] or row["snapshot_released"] is not True
                or row["stale_rejected"] != ["generation", "stateEpoch", "tick"]
                or len(continuation) != 2 or continuation[0] != continuation[1]
                or continuation[0]["census"]["tick"] != before["tick"] + 1
                or continuation[0]["digest"] == row["digest"]):
            raise ValueError("mode native restore or continuation evidence differs")
        for census in (before, restored, *(item["census"] for item in continuation)):
            validate_census(census, process)
    return dict(schema="fair-final-native-mode-verification.v1", status="passed_limited_scope",
        libg_sha256=LIBG_SHA, probe_sha256=PROBE_SHA, stages=len(records), engine_cleanup_passed=True,
        static_report_sha256=sha(ROOT / "static-report.json"),
        observed_predicates=[{name: row["census"][name] for name in
            ("tick", "world_type", "rules_mode", "predicate_bit0", "queue_branch")} for row in records],
        behavior="legal-WAIT-only", actual_policy_search_verified=False,
        legal_rejection_reachability_proven=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    args = parser.parse_args()
    report = validate(args.attempt, args.launch_sha, args.receipt_sha)
    report["verifier_sha256"] = sha(Path(__file__))
    (args.attempt / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
