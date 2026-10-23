import argparse
import json
from pathlib import Path

from prior_sampling import PORT, PROBE_SHA, REPEAT_COUNT, diagnose, read_rows, seeds, sha, validate_pair


ROOT = Path(__file__).resolve().parent
SOURCES = ("prior_sampling.py", "probe_openings.py", "opening_worker.py")
VARIANTS = ("primary", "repeat")
NAMES = ("report.json", "rows.jsonl", "cleanup.json", "probe.log", "engine.log", "engine/process.json")
FIXED_DECK = [26000021, 26000038, 26000014, 26000010, 26000030, 27000000, 28000000, 28000011]


def validate(directory, launch_sha, receipt_sha):
    if sha(directory / "launch.json") != launch_sha or sha(directory / "download.json") != receipt_sha:
        raise ValueError("opening external launch or receipt identity differs")
    launch = json.loads((directory / "launch.json").read_text())
    receipt = json.loads((directory / "download.json").read_text())
    if (launch["schema"] != "fair-final-native-opening-launch.v1"
            or receipt["schema"] != "fair-final-native-opening-download.v1"
            or receipt["source_worker_stopped"] is not True or receipt["launch_sha256"] != launch_sha
            or any(value[name] is not False for value in (launch, receipt)
                for name in ("training_admissible", "gates_closed"))):
        raise ValueError("opening launch scope differs")
    if sha(ROOT / "run_openings.py") != launch["runner_sha256"] or sha(ROOT / "CONTRACT.md") != launch["contract_sha256"]:
        raise ValueError("opening runner or preregistered contract differs")
    archive = directory / "archive"
    expected_names = {*SOURCES, "settings.json", "worker.log",
        *(variant + "/" + name for variant in VARIANTS for name in NAMES)}
    if set(receipt["files"]) != expected_names:
        raise ValueError("opening archive coverage differs")
    for name, expected in receipt["files"].items():
        path = archive / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink()
                or sha(path) != expected):
            raise ValueError("opening archive bytes differ")
    if set(launch["uploaded_sha256"]) != {*SOURCES, "settings.json"}:
        raise ValueError("opening uploaded source coverage differs")
    for name, expected in launch["uploaded_sha256"].items():
        if receipt["files"][name] != expected or name in SOURCES and sha(ROOT / name) != expected:
            raise ValueError("opening uploaded source identity differs")
    settings = json.loads((archive / "settings.json").read_text())
    if (settings != launch["settings"] or settings["port"] != PORT
            or settings["seeds"] != seeds() or settings["repeat_count"] != REPEAT_COUNT
            or settings["timeout_seconds"] != 14400 or settings["engine_sha256"] != PROBE_SHA
            or settings["root"] != launch["remote"]
            or settings["source_sha256"] != {name: launch["uploaded_sha256"][name] for name in SOURCES}):
        raise ValueError("opening preregistered settings differ")
    rows, processes, reports = [], [], []
    for variant in VARIANTS:
        root = archive / variant
        cleanup = json.loads((root / "cleanup.json").read_text())
        process = json.loads((root / "engine/process.json").read_text())
        if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
                or cleanup["listening"] is not False or cleanup["pids_alive"] != dict(pid=False, child_pid=False)
                or cleanup["port"] != PORT or process != cleanup["process"] or process["phase"] != "stopped"
                or process["port"] != PORT or process["phase_snapshot_engine"]["path"] != settings["probe"]
                or process["phase_snapshot_engine"]["sha256"] != PROBE_SHA
                or type(process["child_pid"]) is not int or process["child_pid"] <= 0):
            raise ValueError("opening owned engine cleanup differs")
        report = json.loads((root / "report.json").read_text())
        if (report["schema"] != "fair-final-native-opening-probe.v1" or report["status"] != "complete"
                or report["variant"] != variant or report["rows_sha256"] != sha(root / "rows.jsonl")
                or report["source_sha256"] != settings["source_sha256"]
                or report["runtime_sha256"] != settings["runtime_sha256"]
                or report["training_admissible"] is not False or report["gates_closed"] is not False
                or not 0 < report["seconds"] < settings["timeout_seconds"]):
            raise ValueError("opening native report identity differs")
        sampled = read_rows(root / "rows.jsonl")
        if report["rows"] != len(sampled):
            raise ValueError("opening native row count differs")
        rows.append(sampled)
        reports.append(report)
        processes.append(process)
    if (processes[0]["child_pid"] == processes[1]["child_pid"]
            or any(report["deck"] != FIXED_DECK for report in reports)):
        raise ValueError("opening processes or decks differ")
    deck = reports[0]["deck"]
    repeat = validate_pair(*rows, deck)
    distribution = diagnose(rows[0], seeds(), deck)
    return dict(schema="fair-final-native-opening-verification.v1", status="verified_diagnostic",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, probe_sha256=PROBE_SHA,
        distribution=distribution, repeat=repeat, engine_cleanup_passed=True,
        seconds=[report["seconds"] for report in reports], native_processes=2,
        training_admissible=False, gates_closed=False, verifier_sha256=sha(__file__))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    args = parser.parse_args()
    report = validate(args.attempt, args.launch_sha, args.receipt_sha)
    target = args.attempt / "verification.json"
    if target.exists() and json.loads(target.read_text()) != report:
        raise ValueError("opening verification already exists with different content")
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], distribution=report["distribution"]["status"],
        repeated_seeds=report["repeat"]["repeated_seeds"], verification_sha256=sha(target))))
