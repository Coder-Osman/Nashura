from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pocket_verifier", ROOT / "verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def native():
    directories = sorted(directory for directory in ROOT.glob("native-pocket-collision-*")
        if (directory / "report.json").exists()
        and json.loads((directory / "report.json").read_text()).get("replay"))
    assert directories, "collect a native collision report with fresh-reset evidence first"
    directory = directories[-1]
    return [json.loads((directory / name).read_text()) for name in ("attempt.json", "report.json", "cleanup.json")]


def test_native_collision_evidence_is_limited(native):
    result = verifier.verify(*native)
    assert result["status"] == "passed_limited_scope"
    assert result["trials"] == 2 and result["continuations"] == 4
    assert result["general_pending_gate_closed"] is False and result["training_admissible"] is False


@pytest.mark.parametrize("mutation", ["engine", "checkpoint", "source-files", "status", "training", "bypass",
    "seed", "tower", "trajectory", "replay", "root", "repeat", "mask", "deadline", "outcomes", "evidence",
    "rng", "ready", "classification", "coverage", "cleanup", "listening"])
def test_native_tampering_is_rejected(native, mutation):
    attempt, report, cleanup = deepcopy(native)
    branch = report["records"][0]["repetitions"][0]
    if mutation == "engine":
        attempt["engine_sha"] = "0" * 64
    elif mutation == "checkpoint":
        report["source_sha"] = "0" * 64
    elif mutation == "source-files":
        report["source_files"]["probe.py"] = "0" * 64
    elif mutation == "status":
        report["status"] = "running"
    elif mutation == "training":
        report["training_admissible"] = True
    elif mutation == "bypass":
        report["mask_bypass"] = True
    elif mutation == "seed":
        report["seed"] += 1
    elif mutation == "tower":
        for tower in report["root_public_towers"]:
            tower["hp"], tower["active"] = 1, True
    elif mutation == "trajectory":
        report["trajectory"][0]["selected_legal"] = False
    elif mutation == "replay":
        report["replay"]["root"]["fair"]["0"] = "0" * 64
    elif mutation == "root":
        report["root"]["clock"]["ready"] = []
    elif mutation == "repeat":
        report["records"][0]["repetitions"][1]["final"]["native_sha"] = "0" * 64
    elif mutation == "mask":
        mask = report["masks"]["0"]
        action = branch["actions"]["0"]
        values = bytearray.fromhex(mask["plays_hex"])
        index = mask["card_ids"].index(27000000)
        values[(index * 576 + action["cell"]) * 31 + action["delay_slot"]] = 0
        mask["plays_hex"] = values.hex()
    elif mutation == "deadline":
        branch["reservations"][0]["due_tick"] += 1
    elif mutation == "outcomes":
        branch["steps"][-1]["outcomes"] = {}
    elif mutation == "evidence":
        branch["evidence"].pop()
    elif mutation == "rng":
        branch["final"]["rng"]["0"] = "0" * 64
    elif mutation == "ready":
        branch["final"]["clock"]["ready"] = []
    elif mutation == "classification":
        branch["rejected"] = not branch["rejected"]
    elif mutation == "coverage":
        report["records"].pop()
    elif mutation == "cleanup":
        cleanup["clean"] = False
    elif mutation == "listening":
        cleanup["listening"] = True
    if mutation in {"deadline", "outcomes", "evidence", "rng", "ready", "classification"}:
        report["records"][0]["repetitions"][1] = deepcopy(branch)
    with pytest.raises(ValueError):
        verifier.verify(attempt, report, cleanup)
