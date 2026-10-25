import argparse
from dataclasses import asdict
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent / "fair-final-history-v2/project"
sys.path.insert(0, str(PROJECT))
package = importlib.import_module("native_runner")
if Path(package.__file__).resolve() != PROJECT / "native_runner/__init__.py":
    raise ValueError("fresh process with frozen history-v2 package required")
import torch
from native_runner.training.fair_final.policy import LegalPrefixes
from cases import trials


ENGINE_SHA = "fbd97f8f02621d37adc41b9782440950d9e0346ad2cffcb68601c01fb237141e"
BASE = ROOT.parent / "fair-final-legal-reservations-v1/verify.py"
BASE_SHA = "42acd66f7b779b8757318b9a6959b49ea770bfabecd77df14107817afbed2102"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def expected_trials(record):
    views = {}
    for owner, mask in record["masks"].items():
        shape = mask["shape"]
        values = bytes.fromhex(mask["plays_hex"])
        if shape != [len(mask["card_ids"]), 576, 31] or len(values) != shape[0] * 576 * 31 or set(values) - {0, 1}:
            raise ValueError("threshold mask dimensions or values differ")
        tensor = torch.tensor(list(values), dtype=torch.bool).reshape(shape)
        views[int(owner)] = SimpleNamespace(legality=LegalPrefixes(mask["card_ids"], tensor))
    return [dict(kind=trial["kind"], actions={str(owner): asdict(action)
        for owner, action in trial["actions"].items()}) for trial in trials(views)]


def verify(attempt, launch_sha, receipt_sha):
    attempt = Path(attempt)
    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "receipt.json") != receipt_sha:
        raise ValueError("threshold external identity differs")
    launch = json.loads((attempt / "launch.json").read_text())
    receipt = json.loads((attempt / "receipt.json").read_text())
    if (launch["schema"] != "FairFinalThresholdLaunch/v1" or launch["engine_sha"] != ENGINE_SHA
            or launch["base_verifier_sha"] != BASE_SHA or sha(BASE) != BASE_SHA
            or launch["verifier_sha"] != sha(__file__) or launch["runner_sha"] != sha(ROOT / "run_native.py")
            or launch["contract_sha"] != sha(ROOT / "CONTRACT.md")
            or receipt["launch_sha"] != launch_sha or receipt["worker_stopped"] is not True
            or receipt["status"] != "stopped_requires_verification"
            or any(record[key] is not False for record in (launch, receipt)
                for key in ("training_admissible", "gates_closed"))):
        raise ValueError("threshold launch or receipt scope differs")
    for name, expected in launch["payload_sha"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("threshold payload path differs")
        if sha(attempt / "payload" / name) != expected:
            raise ValueError("threshold payload changed")
    manifest = json.loads((attempt / "payload/project/probe-source-manifest.json").read_text())
    if manifest != launch["source_files"] or any(
            launch["payload_sha"].get("project/" + name) != expected
            for name, expected in manifest.items()):
        raise ValueError("threshold complete source manifest differs")
    if not {"report.json", "cleanup.json"} <= receipt["files"].keys():
        raise ValueError("threshold required archive missing")
    for name, expected in receipt["files"].items():
        path = attempt / "archive" / name
        if name not in {"report.json", "cleanup.json", "progress.json", "failure.json", "worker.log", "probe.log", "engine.log"}:
            raise ValueError("threshold archive name differs")
        if sha(path) != expected["sha256"] or path.stat().st_size != expected["bytes"]:
            raise ValueError("threshold archive changed")
    if "failure.json" in receipt["files"]:
        raise ValueError("threshold diagnostic failed")
    report = json.loads((attempt / "archive/report.json").read_text())
    cleanup = json.loads((attempt / "archive/cleanup.json").read_text())
    trace = json.loads((attempt / "payload/project/input-trace.json").read_text())
    if (report["boundary_protocol"] != "fair-final-earliest-legal-delay.v1"
            or report["source_files"] != manifest
            or report["exclusions"] != ["all-spatial-cells", "all-trajectories", "global-unreachability",
                "pending-belief-reconstruction", "complete-game-search"]
            or report["general_pending_gate_closed"] is not False or report["gates_closed"] is not False):
        raise ValueError("threshold report scope differs")
    thresholds = 0
    for record in report["records"]:
        expected = expected_trials(record)
        actual = [dict(kind=trial["kind"], actions=trial["repetitions"][0]["actions"])
            for trial in record["trials"]]
        if expected != actual:
            raise ValueError("threshold trials differ from archived legal masks")
        thresholds += sum(trial["kind"] == "earliest-legal-delay-and-next" for trial in expected)
    if not thresholds:
        raise ValueError("no delayed legality threshold exercised")
    specification = importlib.util.spec_from_file_location("threshold_base_verifier", BASE)
    verifier = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(verifier)
    verifier.ENGINE_SHA = ENGINE_SHA
    legacy_attempt = dict(launch, schema="fair-final-legal-reservations-attempt.v1", exit_code=cleanup["exit_code"])
    result = verifier.verify(legacy_attempt, report, cleanup, trace)
    return dict(result, threshold_trials=thresholds, launch_sha=launch_sha, receipt_sha=receipt_sha,
        base_verifier_sha=BASE_SHA, verifier_sha=sha(__file__), gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    args = parser.parse_args()
    result = verify(args.attempt, args.launch_sha, args.receipt_sha)
    with (args.attempt / "verification.json").open("x") as handle:
        handle.write(json.dumps(result, indent=2))
    print(json.dumps(result))
