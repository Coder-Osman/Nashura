import argparse
import json
from pathlib import Path

from seed_audit import LIBG_SHA, PROBE_SHA, REGIONS, seed_effect, sha, unsigned


ROOT = Path(__file__).resolve().parent
LIMITS = ("all_global_writers_proven", "actual_policy_search_verified", "hidden_random_coverage_closed",
    "training_admissible", "gates_closed")


def semantic(record):
    return {name: value for name, value in record.items() if name not in ("generation", "stateEpoch")}


def portable(record):
    return {name: record[name] for name in ("tick", "manager_rng", "increment")}


def validate_census(record, process, *, initialized, counters=True):
    for name in ("base_time", "cached_time") + (("generation", "stateEpoch") if counters else ()):
        unsigned(record[name], 64)
    if not counters and any(name in record for name in ("generation", "stateEpoch")):
        raise ValueError("continuation semantic census contains execution counters")
    if (type(record["tick"]) is not int or record["readonly"] is not True
            or record["training_admissible"] is not False or record["gates_closed"] is not False
            or record["child_pid"] != process["child_pid"]
            or record["child_start_ticks"] != process["child_start_ticks"]
            or record["base_time"] == 0):
        raise ValueError("seed census process or scope differs")
    if initialized:
        unsigned(record["manager_rng"], 32)
        if record["cached_time"] == 0:
            raise ValueError("initialized native seed cache is zero")
    elif record["manager_rng"] is not None:
        raise ValueError("startup census unexpectedly contains manager state")
    expected = None if record["cached_time"] == 0 else seed_effect(
        0, record["base_time"], record["cached_time"])["increment"]
    if record["increment"] != expected or type(record["increment"]) is not type(expected):
        raise ValueError("seed census increment differs")


def validate_probe(report, process, *, delayed):
    if (report["schema"] != "fair-final-native-seed-probe.v1" or report["status"] != "passed_limited_scope"
            or report["delayed"] is not delayed or report["behavior"] != "legal-WAIT-only"
            or any(report[name] is not False for name in LIMITS)):
        raise ValueError("seed native report scope differs")
    startup, before = report["startup"], report["before_reset"]
    for record in (startup, before):
        validate_census(record, process, initialized=False)
    if semantic(startup) != semantic(before) or delayed and report["reset_wall_time"] <= startup["base_time"] + 16:
        raise ValueError("seed startup delay or passive stability differs")
    initial, repeated = report["initial"], report["repeated_reset"]
    if (initial["digest"] != repeated["digest"] or semantic(initial["census"]) != semantic(repeated["census"])
            or initial["census"]["generation"] >= repeated["census"]["generation"]):
        raise ValueError("seed repeated fresh reset differs")
    censuses = [(initial["census"], True), (repeated["census"], True)]
    rows = report["records"]
    if [row["threshold"] for row in rows] != [90, 1200, 3600, 5400]:
        raise ValueError("seed trajectory stage coverage differs")
    for row in rows:
        census, restored, continuations = row["census"], row["restored"], row["continuations"]
        if (census["tick"] != row["threshold"] or semantic(census) != semantic(restored)
                or row["digest"] != row["restored_digest"] or row["snapshot_released"] is not True
                or row["stale_rejected"] != ["generation", "stateEpoch", "tick"]
                or len(continuations) != 2 or continuations[0] != continuations[1]
                or continuations[0]["census"]["tick"] != census["tick"] + 1
                or continuations[0]["digest"] == row["digest"]):
            raise ValueError("seed snapshot or repeated continuation differs")
        censuses.extend([(census, True), (restored, True),
            *((entry["census"], False) for entry in continuations)])
    for census, counters in censuses:
        validate_census(census, process, initialized=True, counters=counters)
        if any(census[name] != initial["census"][name] for name in ("base_time", "cached_time", "increment")):
            raise ValueError("seed process globals changed during bounded trajectory")
    if initial["census"]["base_time"] != before["base_time"] or (
            before["cached_time"] and initial["census"]["cached_time"] != before["cached_time"]):
        raise ValueError("seed globals changed unexpectedly during first reset")


def validate_pair(immediate, delayed):
    if immediate["initial"]["census"]["child_pid"] == delayed["initial"]["census"]["child_pid"]:
        raise ValueError("seed restart did not use a new native process")
    for first, second in [(immediate["initial"], delayed["initial"]),
            *[(dict(digest=first["digest"], census=first["census"]),
                dict(digest=second["digest"], census=second["census"]))
                for first, second in zip(immediate["records"], delayed["records"])]]:
        if first["digest"] != second["digest"] or portable(first["census"]) != portable(second["census"]):
            raise ValueError("same-seed cross-process startup or continuation differs")


def validate(directory, launch_sha, receipt_sha):
    if sha(directory / "launch.json") != launch_sha or sha(directory / "download.json") != receipt_sha:
        raise ValueError("seed external launch or receipt identity differs")
    launch = json.loads((directory / "launch.json").read_text())
    receipt = json.loads((directory / "download.json").read_text())
    if sha(ROOT / "run_seed.py") != launch["runner_sha256"]:
        raise ValueError("seed launch runner identity differs")
    if (launch["schema"] != "fair-final-native-seed-launch.v1"
            or receipt["schema"] != "fair-final-native-seed-download.v1"
            or receipt["source_worker_stopped"] is not True or receipt["launch_sha256"] != launch_sha
            or any(value[name] is not False for value in (launch, receipt)
                for name in ("training_admissible", "gates_closed"))):
        raise ValueError("seed launch or receipt scope differs")
    archive = directory / "archive"
    required = {*launch["uploaded_sha256"]}
    allowed = {*required, "worker.log"}
    for variant in ("immediate", "delayed"):
        required.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json", "engine/process.json"))
        allowed.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json",
            "engine/process.json", "probe.log", "engine.log"))
    if not required.issubset(receipt["files"]) or not set(receipt["files"]).issubset(allowed):
        raise ValueError("seed required archive coverage differs or retained failure exists")
    for name, expected in receipt["files"].items():
        if (Path(name).is_absolute() or ".." in Path(name).parts or (archive / name).is_symlink()
                or sha(archive / name) != expected):
            raise ValueError("seed archive content identity differs")
    for name, expected in launch["uploaded_sha256"].items():
        if receipt["files"].get(name) != expected or name.endswith(".py") and sha(ROOT / name) != expected:
            raise ValueError("seed source or settings identity differs")
    settings = json.loads((archive / "settings.json").read_text())
    if (settings != launch["settings"] or settings["port"] != 29775
            or settings["engine_sha256"] != PROBE_SHA or settings["source_sha256"] != {
                name: value for name, value in launch["uploaded_sha256"].items() if name.endswith(".py")}):
        raise ValueError("seed runtime settings differ")
    if sha(ROOT / "static-report.json") != launch["static_report_sha256"]:
        raise ValueError("seed static report identity differs")
    static = json.loads((ROOT / "static-report.json").read_text())
    if (static["libg_sha256"] != LIBG_SHA or static["status"] != "passed_limited_scope"
            or {name: row["sha256"] for name, row in static["regions"].items()} != {
                name: region[2] for name, region in REGIONS.items()}):
        raise ValueError("seed static instruction identity differs")
    reports = []
    for variant in ("immediate", "delayed"):
        root = archive / variant
        cleanup = json.loads((root / "cleanup.json").read_text())
        process = json.loads((root / "engine/process.json").read_text())
        if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
                or cleanup["listening"] is not False or cleanup["pids_alive"] != dict(pid=False, child_pid=False)
                or cleanup["port"] != settings["port"] or process != cleanup["process"] or process["phase"] != "stopped"
                or process["phase_snapshot_engine"]["path"] != settings["probe"]
                or process["phase_snapshot_engine"]["sha256"] != PROBE_SHA):
            raise ValueError("seed owned engine cleanup or identity differs")
        report = json.loads((root / "report.json").read_text())
        if (report["source_sha256"] != launch["frozen_runtime_sha256"]
                or json.loads((root / "progress.json").read_text()) != dict(records=report["records"])):
            raise ValueError("seed frozen sources or progress differs")
        validate_probe(report, process, delayed=variant == "delayed")
        reports.append(report)
    validate_pair(*reports)
    return dict(schema="fair-final-native-seed-verification.v1", status="passed_limited_scope",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, libg_sha256=LIBG_SHA, probe_sha256=PROBE_SHA,
        static_report_sha256=launch["static_report_sha256"], native_processes=2, stages_per_process=4,
        same_seed_initial_and_stage_digests_equal=True, repeated_reset_equal=True,
        process_cache_increment=[report["initial"]["census"]["increment"] for report in reports],
        cache_nonzero_before_first_match=[bool(report["before_reset"]["cached_time"]) for report in reports],
        engine_cleanup_passed=True, behavior="legal-WAIT-only", **{name: False for name in LIMITS})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    args = parser.parse_args()
    report = validate(args.attempt, args.launch_sha, args.receipt_sha)
    report["verifier_sha256"] = sha(__file__)
    (args.attempt / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
