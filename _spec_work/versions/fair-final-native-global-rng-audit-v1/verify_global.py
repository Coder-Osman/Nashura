import argparse
import hashlib
import json
from pathlib import Path

from global_rng_audit import GLOBAL_OFFSET, LIBG_SHA, PORT, classify_restore, sha, state_summary
from run_global import ROOT, upload_sources
from seed_audit import PROBE_SHA


LIMITS = ("fixed_hog_usage_proven", "hidden_random_coverage_closed", "training_admissible", "gates_closed")


def validate_state(record, content, process, *, tick=None):
    if (record["child_pid"] != process["child_pid"]
            or record["child_start_ticks"] != process["child_start_ticks"]
            or record["libg_sha256"] != LIBG_SHA or record["global_offset"] != hex(GLOBAL_OFFSET)
            or record["readonly"] is not True or record["state"] != state_summary(content)):
        raise ValueError("global RNG census identity or content differs")
    if tick is not None and record["tick"] != tick:
        raise ValueError("global RNG census tick differs")
    for name in ("generation", "stateEpoch"):
        if type(record[name]) is not int or record[name] < 0:
            raise ValueError("global RNG census counters differ")


def validate_probe(report, process, *, second_process):
    if (report["schema"] != "fair-final-native-global-rng-probe.v1"
            or report["status"] != "completed_diagnostic" or report["second_process"] is not second_process
            or report["behavior"] != "legal-WAIT-with-isolated-global-perturbation-no-step"
            or any(report[name] is not False for name in LIMITS)):
        raise ValueError("global RNG report scope differs")
    validate_state(report["startup"], bytes.fromhex(report["startup_hex"]), process)
    if [row["threshold"] for row in report["records"]] != [90, 1200, 3600, 5400]:
        raise ValueError("global RNG stage coverage differs")
    for row in report["records"]:
        original, perturbed, restored = (bytes.fromhex(row[name])
            for name in ("original_hex", "perturbed_hex", "restored_hex"))
        if row["classification"] != classify_restore(original, perturbed, restored):
            raise ValueError("global RNG restore classification differs")
        for name, content in (("before", original), ("restored", restored), ("repaired", original)):
            validate_state(row[name], content, process, tick=row["threshold"])
        if (row["before_digest"] != row["final_digest"] or row["snapshot_released"] is not True
                or row["no_native_steps_while_perturbed"] is not True
                or row["stale_rejected"] != ["generation", "stateEpoch", "tick"]
                or row["before"]["generation"] != row["restored"]["generation"]
                or row["restored"]["generation"] != row["repaired"]["generation"]
                or not row["before"]["stateEpoch"] < row["restored"]["stateEpoch"]
                or row["restored"]["stateEpoch"] != row["repaired"]["stateEpoch"]):
            raise ValueError("global RNG repair, stale rejection or snapshot lifecycle differs")
        for name in ("before_digest", "perturbed_digest", "final_digest"):
            if len(row[name]) != 64 or len(bytes.fromhex(row[name])) != 32:
                raise ValueError("global RNG native digest shape differs")


def validate(attempt, launch_sha, receipt_sha):
    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "download.json") != receipt_sha:
        raise ValueError("global RNG external identities differ")
    launch = json.loads((attempt / "launch.json").read_text())
    receipt = json.loads((attempt / "download.json").read_text())
    if (launch["schema"] != "fair-final-native-global-rng-launch.v1"
            or receipt["schema"] != "fair-final-native-global-rng-download.v1"
            or receipt["launch_sha256"] != launch_sha or receipt["source_worker_stopped"] is not True
            or any(record[name] is not False for record in (launch, receipt)
                for name in ("training_admissible", "gates_closed"))):
        raise ValueError("global RNG archive scope differs")
    for name, expected in (("run_global.py", launch["runner_sha256"]),
            ("static-report.json", launch["static_report_sha256"]), ("CONTRACT.md", launch["contract_sha256"])):
        if sha(ROOT / name) != expected:
            raise ValueError("global RNG local contract or implementation differs")
    archive = attempt / "archive"
    required = {*launch["uploaded_sha256"], "worker.log"}
    allowed = set(required)
    for variant in ("immediate", "delayed"):
        required.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json", "engine/process.json"))
        allowed.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json",
            "engine/process.json", "probe.log", "engine.log"))
    if not required.issubset(receipt["files"]) or not set(receipt["files"]).issubset(allowed):
        raise ValueError("global RNG archive missing evidence or contains failures")
    for name, expected in receipt["files"].items():
        path = archive / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or sha(path) != expected:
            raise ValueError("global RNG archived bytes differ")
    expected_sources = {name: hashlib.sha256(content).hexdigest() for name, content in upload_sources().items()}
    if launch["uploaded_sha256"] != {**expected_sources,
            "settings.json": receipt["files"]["settings.json"]}:
        raise ValueError("global RNG source identity differs")
    for name, expected in launch["uploaded_sha256"].items():
        if receipt["files"].get(name) != expected:
            raise ValueError("global RNG upload differs from archive")
    settings = json.loads((archive / "settings.json").read_text())
    if (settings != launch["settings"] or settings["port"] != PORT
            or settings["engine_sha256"] != PROBE_SHA or settings["source_sha256"] != expected_sources):
        raise ValueError("global RNG frozen runtime settings differ")
    reports, processes = [], []
    for variant in ("immediate", "delayed"):
        folder = archive / variant
        cleanup = json.loads((folder / "cleanup.json").read_text())
        process = json.loads((folder / "engine/process.json").read_text())
        if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
                or cleanup["listening"] is not False or cleanup["pids_alive"] != dict(pid=False, child_pid=False)
                or cleanup["port"] != PORT or cleanup["process"] != process or process["phase"] != "stopped"
                or process["phase_snapshot_engine"]["path"] != settings["probe"]
                or process["phase_snapshot_engine"]["sha256"] != PROBE_SHA):
            raise ValueError("global RNG engine cleanup or ownership differs")
        report = json.loads((folder / "report.json").read_text())
        if (report["source_sha256"] != launch["frozen_runtime_sha256"]
                or json.loads((folder / "progress.json").read_text()) != dict(records=report["records"])):
            raise ValueError("global RNG runtime or progress differs")
        validate_probe(report, process, second_process=variant == "delayed")
        reports.append(report)
        processes.append((process["child_pid"], process["child_start_ticks"]))
    if processes[0] == processes[1]:
        raise ValueError("global RNG second native process reused")
    rows = [row for report in reports for row in report["records"]]
    return dict(schema="fair-final-native-global-rng-verification.v1", status="verified_diagnostic_only",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, archived_files=len(receipt["files"]),
        native_processes=2, tested_boundaries=len(rows), classifications=[row["classification"] for row in rows],
        perturbation_visible_to_digest=[row["before_digest"] != row["perturbed_digest"] for row in rows],
        global_unchanged_along_wait=[all(row["original_hex"] == report["startup_hex"]
            for row in report["records"]) for report in reports],
        fixed_hog_usage_proven=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    arguments = parser.parse_args()
    report = validate(arguments.attempt, arguments.launch_sha, arguments.receipt_sha)
    with (arguments.attempt / "verification.json").open("x") as handle:
        handle.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
