import argparse
import hashlib
import json
from pathlib import Path
import sys

from build_engine import ROOT, sha


sys.path.insert(0, str(ROOT.parent / "fair-final-native-global-rng-audit-v1"))
from global_rng_audit import perturb_state
from verify_global import LIMITS, validate_state
from run_snapshot import PORT, upload_sources


SCOPE = "native-payload+global-mt2504.v1"


def validate_digest(record, tick):
    if record["scope"] != SCOPE or type(record["tick"]) is not int or record["tick"] != tick:
        raise ValueError("snapshot digest scope or tick differs")
    for name in ("sha256", "nativeSha256"):
        if len(record[name]) != 64 or len(bytes.fromhex(record[name])) != 32:
            raise ValueError("snapshot digest shape differs")


def validate_probe(report, process, *, second_process, engine_sha):
    if (report["schema"] != "fair-final-global-rng-snapshot-probe.v1"
            or report["status"] != "completed_limited_scope"
            or report["second_process"] is not second_process or report["engine_sha256"] != engine_sha
            or any(report[name] is not False for name in LIMITS)):
        raise ValueError("snapshot probe scope differs")
    validate_state(report["startup"], bytes.fromhex(report["startup_hex"]), process)
    if [row["threshold"] for row in report["records"]] != [90, 1200, 3600, 5400]:
        raise ValueError("snapshot boundary coverage differs")
    for row in report["records"]:
        tick = row["threshold"]
        original, perturbed, restored, repeated = (bytes.fromhex(row[name]) for name in
            ("original_hex", "perturbed_hex", "restored_hex", "repeated_hex"))
        if perturb_state(original) != perturbed or restored != original or repeated != original:
            raise ValueError("snapshot bytewise perturbation or restoration differs")
        for name, content in (("before", original), ("restored", restored), ("repeated", repeated)):
            validate_state(row[name], content, process, tick=tick)
        if (len({row[name]["generation"] for name in ("before", "restored", "repeated")}) != 1
                or not row["before"]["stateEpoch"] < row["restored"]["stateEpoch"] < row["repeated"]["stateEpoch"]
                or row["snapshot_released"] is not True or row["no_native_steps_while_perturbed"] is not True):
            raise ValueError("snapshot generation, epoch or lifecycle differs")
        for name in ("before_digest", "perturbed_digest", "final_digest"):
            validate_digest(row[name], tick)
        before, changed = row["before_digest"], row["perturbed_digest"]
        if (before != row["final_digest"] or before["sha256"] == changed["sha256"]
                or before["nativeSha256"] != changed["nativeSha256"]):
            raise ValueError("snapshot global-only digest sensitivity differs")
        branches = row["branches"]
        if len(branches) != 2 or any(len(branch) != 10 for branch in branches) or branches[0] != branches[1]:
            raise ValueError("snapshot repeated continuation differs")
        for branch in branches:
            for step, digest in enumerate(branch, 1):
                validate_digest(digest, tick + step)


def validate(attempt, launch_sha, receipt_sha):
    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "download.json") != receipt_sha:
        raise ValueError("snapshot external identities differ")
    launch = json.loads((attempt / "launch.json").read_text())
    receipt = json.loads((attempt / "download.json").read_text())
    if (launch["schema"] != "fair-final-global-rng-snapshot-launch.v1"
            or receipt["schema"] != "fair-final-global-rng-snapshot-download.v1"
            or receipt["launch_sha256"] != launch_sha or receipt["source_worker_stopped"] is not True
            or any(record[name] is not False for record in (launch, receipt)
                for name in ("training_admissible", "gates_closed"))):
        raise ValueError("snapshot archive scope differs")
    for name, expected in (("run_snapshot.py", launch["runner_sha256"]),
            ("CONTRACT.md", launch["contract_sha256"])):
        if sha(ROOT / name) != expected:
            raise ValueError("snapshot local implementation differs")
    build = launch["build_manifest"]
    if (build["schema"] != "fair-final-global-rng-snapshot-build.v1"
            or hashlib.sha256((json.dumps(build, indent=2) + "\n").encode()).hexdigest() != launch["build_manifest_sha256"]
            or build["builder_sha256"] != sha(ROOT / "build_engine.py")
            or build["module_sha256"] != sha(ROOT / "global_snapshot.inc")
            or build["artifacts"]["probe/global_snapshot.inc"] != build["module_sha256"]
            or build["artifacts"]["libcrprobe.so"] != build["engine_sha256"]
            or any(build[name] is not False for name in ("training_admissible", "gates_closed"))):
        raise ValueError("snapshot build implementation differs")
    archive = attempt / "archive"
    required = {*launch["uploaded_sha256"], "worker.log"}
    allowed = set(required)
    for variant in ("immediate", "delayed"):
        required.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json", "engine/process.json"))
        allowed.update(variant + "/" + name for name in ("report.json", "progress.json", "cleanup.json",
            "engine/process.json", "probe.log", "engine.log"))
    if not required.issubset(receipt["files"]) or not set(receipt["files"]).issubset(allowed):
        raise ValueError("snapshot archive missing evidence or contains failures")
    for name, expected in receipt["files"].items():
        path = archive / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or sha(path) != expected:
            raise ValueError("snapshot archived bytes differ")
    sources = {name: hashlib.sha256(content).hexdigest() for name, content in upload_sources().items()}
    if launch["uploaded_sha256"] != {**sources, "libcrprobe.so": build["engine_sha256"],
            "settings.json": receipt["files"]["settings.json"]}:
        raise ValueError("snapshot frozen source identity differs")
    for name, expected in launch["uploaded_sha256"].items():
        if receipt["files"].get(name) != expected:
            raise ValueError("snapshot upload differs from archive")
    settings = json.loads((archive / "settings.json").read_text())
    if (settings != launch["settings"] or settings["port"] != PORT
            or settings["engine_sha256"] != build["engine_sha256"] or settings["source_sha256"] != sources):
        raise ValueError("snapshot runtime settings differ")
    processes, reports = [], []
    for variant in ("immediate", "delayed"):
        folder = archive / variant
        cleanup = json.loads((folder / "cleanup.json").read_text())
        process = json.loads((folder / "engine/process.json").read_text())
        if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
                or cleanup["listening"] is not False or cleanup["pids_alive"] != dict(pid=False, child_pid=False)
                or cleanup["port"] != PORT or cleanup["process"] != process or process["phase"] != "stopped"
                or process["phase_snapshot_engine"]["path"] != settings["probe"]
                or process["phase_snapshot_engine"]["sha256"] != build["engine_sha256"]):
            raise ValueError("snapshot cleanup or process ownership differs")
        report = json.loads((folder / "report.json").read_text())
        if (report["source_sha256"] != launch["frozen_runtime_sha256"]
                or json.loads((folder / "progress.json").read_text()) != dict(records=report["records"])):
            raise ValueError("snapshot runtime or progress differs")
        validate_probe(report, process, second_process=variant == "delayed", engine_sha=build["engine_sha256"])
        reports.append(report)
        processes.append((process["child_pid"], process["child_start_ticks"]))
    if processes[0] == processes[1]:
        raise ValueError("snapshot second native process reused")
    return dict(schema="fair-final-global-rng-snapshot-verification.v1", status="verified_limited_scope",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, engine_sha256=build["engine_sha256"],
        archived_files=len(receipt["files"]), native_processes=2, tested_boundaries=8,
        restored_bytes_per_boundary=2504, repeated_wait_steps_per_branch=10,
        digest_scope=SCOPE, fixed_hog_usage_proven=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)


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
