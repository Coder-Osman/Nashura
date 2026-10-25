import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
specification = importlib.util.spec_from_file_location("threshold_verification", ROOT / "verify.py")
verification = importlib.util.module_from_spec(specification)
specification.loader.exec_module(verification)


def publish(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def pins(attempt):
    return verification.sha(attempt / "launch.json"), verification.sha(attempt / "receipt.json")


def refresh_report(attempt, report):
    publish(attempt / "archive/report.json", report)
    receipt = json.loads((attempt / "receipt.json").read_text())
    receipt["files"]["report.json"] = dict(sha256=verification.sha(attempt / "archive/report.json"),
        bytes=(attempt / "archive/report.json").stat().st_size)
    publish(attempt / "receipt.json", receipt)


@pytest.fixture
def attempt(tmp_path):
    sources = {"probe.py": hashlib.sha256(b"fixture probe").hexdigest()}
    publish(tmp_path / "payload/project/probe-source-manifest.json", sources)
    (tmp_path / "payload/project/probe.py").write_bytes(b"fixture probe")
    trace = dict(boundaries=[dict(tick=tick, fair_sha={"0": "fair0", "1": "fair1"})
        for tick in (90, 110, 2598, 5602)])
    publish(tmp_path / "payload/project/input-trace.json", trace)
    payloads = {str(path.relative_to(tmp_path / "payload")): verification.sha(path)
        for path in (tmp_path / "payload").rglob("*") if path.is_file()}
    masks = {}
    for owner in ("0", "1"):
        values = bytearray(576 * 31)
        values[12 * 31 + 4:12 * 31 + 31] = bytes([1]) * 27
        masks[owner] = dict(card_ids=[26000021], shape=[1, 576, 31], plays_hex=values.hex())
    records = []
    for tick in (90, 110, 2598, 5602):
        record = dict(tick=tick, masks=copy.deepcopy(masks), snapshot_released=True,
            root=dict(tick=tick, fair={"0": "fair0", "1": "fair1"}, rng={"0": "rng0", "1": "rng1"},
                clock=dict(ready=[0, 1], pending=[])), trials=[])
        for trial in verification.expected_trials(record):
            reservations = [dict(identity=owner, owner=int(owner), action=action,
                created_tick=tick, due_tick=tick + (60 if action["card_id"] is None
                    else 2 * action["delay_slot"])) for owner, action in trial["actions"].items()]
            end = min(plan["due_tick"] for plan in reservations)
            outcomes = {plan["identity"]: "executed" for plan in reservations if plan["due_tick"] == end}
            evidence = [dict(owner=plan["owner"], reservation=plan["identity"], tick=end,
                reason="executed" if plan["due_tick"] == end else "preempted") for plan in reservations]
            branch = dict(actions=trial["actions"], selected_legal=True, mask_bypass=False,
                reservations=reservations, steps=[dict(tick=current,
                    outcomes=outcomes if current == end else {}) for current in range(tick + 1, end + 1)],
                evidence=evidence, rejected=False, single_ready=False,
                final=dict(tick=end, clock=dict(tick=end, ready=[0, 1], pending=[]), rng=record["root"]["rng"]))
            record["trials"].append(dict(kind=trial["kind"], repetitions=[branch, copy.deepcopy(branch)], exact=True))
        records.append(record)
    report = dict(schema="fair-final-legal-reservations.v1", status="completed", source_files=sources,
        training_admissible=False, mask_bypass=False,
        source_sha="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015",
        trace_sha=payloads["project/input-trace.json"], model_unchanged=True, records=records,
        boundary_protocol="fair-final-earliest-legal-delay.v1", general_pending_gate_closed=False,
        gates_closed=False, exclusions=["all-spatial-cells", "all-trajectories", "global-unreachability",
            "pending-belief-reconstruction", "complete-game-search"])
    cleanup = dict(exit_code=0, error=None, clean=True, port=29803,
        process=dict(phase="stopped"), listening=False, pids_alive={"pid": False, "child_pid": False})
    publish(tmp_path / "archive/report.json", report)
    publish(tmp_path / "archive/cleanup.json", cleanup)
    launch = dict(schema="FairFinalThresholdLaunch/v1", engine_sha=verification.ENGINE_SHA,
        base_verifier_sha=verification.BASE_SHA, verifier_sha=verification.sha(ROOT / "verify.py"),
        runner_sha=verification.sha(ROOT / "run_native.py"), contract_sha=verification.sha(ROOT / "CONTRACT.md"),
        payload_sha=payloads, source_files=sources, port=29803, training_admissible=False, gates_closed=False)
    publish(tmp_path / "launch.json", launch)
    files = {name: dict(sha256=verification.sha(tmp_path / "archive" / name),
        bytes=(tmp_path / "archive" / name).stat().st_size) for name in ("report.json", "cleanup.json")}
    publish(tmp_path / "receipt.json", dict(launch_sha=verification.sha(tmp_path / "launch.json"),
        worker_stopped=True, status="stopped_requires_verification", files=files,
        training_admissible=False, gates_closed=False))
    return tmp_path


def test_fixture_exercises_full_verifier_but_closes_no_gate(attempt):
    result = verification.verify(attempt, *pins(attempt))
    assert result["status"] == "passed_limited_scope"
    assert result["threshold_trials"] == result["trials"] == 16
    assert result["continuations"] == 32
    assert result["gates_closed"] is result["training_admissible"] is False


@pytest.mark.parametrize("mutation", ["source-subset", "wrong-threshold", "missing-trial", "restore-mismatch",
    "missing-outcome", "incomplete-boundary", "illegal-action", "missing-root", "gate-claim"])
def test_resigned_semantic_tampering_is_rejected(attempt, mutation):
    report = json.loads((attempt / "archive/report.json").read_text())
    branch = report["records"][0]["trials"][0]["repetitions"][0]
    if mutation == "source-subset":
        report["source_files"] = {}
    elif mutation == "wrong-threshold":
        branch["actions"]["0"]["delay_slot"] = 3
    elif mutation == "missing-trial":
        report["records"][0]["trials"].pop()
    elif mutation == "restore-mismatch":
        branch["final"]["rng"]["0"] = "changed"
    elif mutation == "missing-outcome":
        for repetition in report["records"][0]["trials"][0]["repetitions"]:
            repetition["steps"][-1]["outcomes"] = {}
    elif mutation == "incomplete-boundary":
        for repetition in report["records"][0]["trials"][0]["repetitions"]:
            repetition["final"]["tick"] -= 1
    elif mutation == "illegal-action":
        report["records"][0]["masks"]["0"]["plays_hex"] = bytes(576 * 31).hex()
    elif mutation == "missing-root":
        report["records"].pop()
    else:
        report["general_pending_gate_closed"] = True
    refresh_report(attempt, report)
    with pytest.raises(ValueError):
        verification.verify(attempt, *pins(attempt))


def test_external_pin_and_payload_byte_tampering_are_rejected(attempt):
    launch_sha, receipt_sha = pins(attempt)
    with pytest.raises(ValueError, match="external identity"):
        verification.verify(attempt, "0" * 64, receipt_sha)
    (attempt / "payload/project/probe.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="payload changed"):
        verification.verify(attempt, launch_sha, receipt_sha)


def test_unbound_base_verifier_is_rejected(attempt, monkeypatch):
    replacement = attempt / "changed-verifier.py"
    replacement.write_text("changed")
    monkeypatch.setattr(verification, "BASE", replacement)
    with pytest.raises(ValueError, match="scope differs"):
        verification.verify(attempt, *pins(attempt))


def test_wrong_preloaded_native_package_is_rejected():
    active = ROOT.parents[2] / "firstlight_CR"
    if not active.exists():
        pytest.skip("active package is outside this snapshot")
    code = ("import importlib.util,sys; sys.path.insert(0," + repr(str(active)) + "); "
        "import native_runner; sys.path.insert(0," + repr(str(ROOT)) + "); "
        "spec=importlib.util.spec_from_file_location('boundary_check'," + repr(str(ROOT / "verify.py"))
        + "); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)")
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "fresh process with frozen history-v2 package required" in result.stderr
