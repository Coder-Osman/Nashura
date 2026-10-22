from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("legal_reservation_verifier", ROOT / "verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def native():
    directories = sorted(directory for directory in ROOT.glob("native-legal-reservations-*")
        if (directory / "report.json").exists())
    assert directories, "collect the bounded native legal reservation probe first"
    directory = directories[-1]
    records = [json.loads((directory / name).read_text()) for name in ("attempt.json", "report.json", "cleanup.json")]
    return (*records, json.loads(verifier.TRACE.read_text()))


def test_real_native_legal_reservations_have_independent_limited_verification(native):
    result = verifier.verify(*native)
    assert result["status"] == "passed_limited_scope"
    assert result["trials"] > 0 and result["continuations"] == 2 * result["trials"]
    assert result["general_pending_gate_closed"] is False and result["training_admissible"] is False


@pytest.mark.parametrize("mutation", ["engine", "checkpoint", "trace", "source-files", "report-status",
    "training", "bypass", "root-fair", "root-coverage", "mask", "deadline", "outcomes", "evidence",
    "ready", "rng", "repeat", "cleanup", "listening"])
def test_native_evidence_tampering_is_rejected(native, mutation):
    attempt, report, cleanup, trace = deepcopy(native)
    record = report["records"][0]
    branch = record["trials"][0]["repetitions"][0]
    if mutation == "engine":
        attempt["engine_sha"] = "0" * 64
    elif mutation == "checkpoint":
        report["source_sha"] = "0" * 64
    elif mutation == "trace":
        report["trace_sha"] = "0" * 64
    elif mutation == "source-files":
        report["source_files"]["probe.py"] = "0" * 64
    elif mutation == "report-status":
        report["status"] = "running"
    elif mutation == "training":
        report["training_admissible"] = True
    elif mutation == "bypass":
        report["mask_bypass"] = True
    elif mutation == "root-fair":
        record["root"]["fair"]["0"] = "0" * 64
    elif mutation == "root-coverage":
        report["records"].pop()
    elif mutation == "mask":
        owner, action = next((owner, action) for owner, action in branch["actions"].items()
            if action["card_id"] is not None)
        mask = record["masks"][owner]
        values = bytearray.fromhex(mask["plays_hex"])
        card = mask["card_ids"].index(action["card_id"])
        values[(card * 576 + action["cell"]) * 31 + action["delay_slot"]] = 0
        mask["plays_hex"] = values.hex()
    elif mutation == "deadline":
        branch["reservations"][0]["due_tick"] += 1
    elif mutation == "outcomes":
        branch["steps"][-1]["outcomes"] = {}
    elif mutation == "evidence":
        branch["evidence"].pop()
    elif mutation == "ready":
        branch["final"]["clock"]["ready"] = []
    elif mutation == "rng":
        branch["final"]["rng"]["0"] = "0" * 64
    elif mutation == "repeat":
        record["trials"][0]["repetitions"][1]["final"]["native_sha"] = "0" * 64
    elif mutation == "cleanup":
        cleanup["clean"] = False
    elif mutation == "listening":
        cleanup["listening"] = True
    if mutation in {"deadline", "outcomes", "evidence", "ready", "rng"}:
        record["trials"][0]["repetitions"][1] = deepcopy(branch)
    with pytest.raises(ValueError):
        verifier.verify(attempt, report, cleanup, trace)
