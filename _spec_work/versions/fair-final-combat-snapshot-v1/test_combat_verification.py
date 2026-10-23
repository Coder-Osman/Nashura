from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json

import pytest
import torch

from combat_protocol import BRANCH_TICKS, EXECUTION_SEED, MATCH_SEED, THRESHOLDS, mask_record, sha
from global_rng_audit import LIBG_SHA, STATE_SIZE, state_summary
from run_combat import PORT, ROOT, SNAPSHOT, upload_sources
from verify_combat import validate, validate_probe
from verify_snapshot import SCOPE
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_search.contracts import semantic_digest


@lru_cache(maxsize=2)
def probe_fixture(combat=True):
    process = dict(child_pid=17, child_start_ticks="91")
    original = bytes(STATE_SIZE)
    global_sha = hashlib.sha256(original).hexdigest()
    plays = torch.zeros((1, 576, 31), dtype=torch.bool)
    plays[0, 255, 1] = True
    mask = mask_record(type("Information", (), {"legality": LegalPrefixes((26000021,), plays)})())
    clock = DecisionClock()
    clock.begin_tick(90, wake_owners=(0, 1), adjudicate_due=lambda *_: {}, freeze=lambda *_: None)

    def state():
        public = dict(towers=[], entities=[])
        if combat and clock.tick >= 92:
            public["entities"] = [dict(entity_id=1, owner=0, hp=50, max_hp=100)]
        return dict(tick=clock.tick, digest=dict(tick=clock.tick, scope=SCOPE,
            sha256="2" * 64, nativeSha256="3" * 64), clock_sha=semantic_digest(clock.capture()),
            global_sha=global_sha, ready=sorted(clock.ready), next_identity=clock.next_identity,
            pending=[asdict(plan) for plan in clock.pending.values()],
            fair_sha={str(owner): "1" * 64 for owner in clock.ready},
            rng_sha={"0": "5" * 64, "1": "6" * 64}, public=public)

    def advance():
        evidence_start = len(clock.evidence)
        action = FullAction(26000021, 255, 1) if combat and clock.tick == 90 else FullAction()
        decision = {str(owner): dict(action=asdict(action), mask=mask, fair_sha="1" * 64)
            for owner in sorted(clock.ready)}
        owners = tuple(sorted(clock.ready))
        if owners:
            clock.commit({owner: action for owner in owners}, legal=lambda *_: True)
        committed = [asdict(clock.pending[owner]) for owner in owners]
        outcomes = {str(plan.identity): "executed" for plan in clock.pending.values()
            if plan.due_tick == clock.tick + 1 and plan.action.card_id is not None}
        clock.begin_tick(clock.tick + 1, wake_owners=(0, 1) if outcomes else (),
            adjudicate_due=lambda *_: {int(identity): reason for identity, reason in outcomes.items()},
            freeze=lambda *_: None)
        return dict(decision=decision, committed=committed, outcomes=outcomes,
            evidence=deepcopy(clock.evidence[evidence_start:]), after=state())

    def census(tick, epoch):
        return dict(**process, libg_sha256=LIBG_SHA, global_offset="0x19fc368", readonly=True,
            state=state_summary(original), generation=1, stateEpoch=epoch, tick=tick)

    opening, trajectory, records = state(), [], []
    for tick in THRESHOLDS:
        while clock.tick < tick:
            trajectory.append(advance())
        root, saved = state(), clock.capture()
        branches = []
        for _ in range(2):
            branches.append([advance() for _ in range(BRANCH_TICKS)])
            clock = DecisionClock.restore(saved)
        records.append(dict(tick=tick, root=root, census=census(tick, 2), global_hex=original.hex(),
            branches=branches, restored=[deepcopy(root), deepcopy(root)],
            restored_censuses=[census(tick, 3), census(tick, 4)], snapshot_released=True))
    return dict(schema="fair-final-combat-snapshot-probe.v1", status="completed_limited_scope",
        second_process=False, engine_sha256="d" * 64, global_monitor_readonly=True,
        match_seed=MATCH_SEED, execution_seed=EXECUTION_SEED, opening=opening,
        trajectory=trajectory, records=records, fixed_hog_usage_proven=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False), process


def test_combat_probe_verifies_bounded_scope():
    report, process = probe_fixture()
    result = validate_probe(report, process, second_process=False, engine_sha="d" * 64)
    assert result["executed_by_owner"] == {0: 1, 1: 1}
    assert result["damaged_frames"] > 0
    assert result["main_trajectory_global_changes"] == 0


def test_empty_wait_trajectory_is_rejected():
    report, process = probe_fixture(False)
    with pytest.raises(ValueError, match="live ACT, entity or HP-loss"):
        validate_probe(report, process, second_process=False, engine_sha="d" * 64)


@pytest.mark.parametrize("mutation", ["gate", "seed", "missing-tick", "stage", "branch", "short-branch",
    "both-branches", "root", "restore", "epoch", "generation", "process", "global", "release"])
def test_probe_rejects_semantic_corruption(mutation):
    report, process = deepcopy(probe_fixture())
    row = report["records"][0]
    if mutation == "gate":
        report["gates_closed"] = True
    elif mutation == "seed":
        report["execution_seed"] += 1
    elif mutation == "missing-tick":
        report["trajectory"].pop()
    elif mutation == "stage":
        row["tick"] += 1
    elif mutation == "branch":
        row["branches"][1][0]["after"]["digest"]["sha256"] = "f" * 64
    elif mutation == "short-branch":
        row["branches"][0].pop()
    elif mutation == "both-branches":
        for branch in row["branches"]:
            branch[0]["after"]["rng_sha"]["0"] = "f" * 64
    elif mutation == "root":
        row["root"]["global_sha"] = "f" * 64
    elif mutation == "restore":
        row["restored"][0]["global_sha"] = "f" * 64
    elif mutation == "epoch":
        row["restored_censuses"][1]["stateEpoch"] = row["restored_censuses"][0]["stateEpoch"]
    elif mutation == "generation":
        row["restored_censuses"][0]["generation"] += 1
    elif mutation == "process":
        row["restored_censuses"][0]["child_start_ticks"] = "other"
    elif mutation == "global":
        row["global_hex"] = (bytes(STATE_SIZE - 1) + b"\x01").hex()
    else:
        row["snapshot_released"] = False
    with pytest.raises(ValueError):
        validate_probe(report, process, second_process=False, engine_sha="d" * 64)


def archive_fixture(folder):
    archive = folder / "archive"
    archive.mkdir()

    def write(name, value):
        path = archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return sha(path)

    sources = {name: write(name, content) for name, content in upload_sources().items()}
    engine_sha = write("libcrprobe.so", b"synthetic combat verification fixture")
    settings = dict(port=PORT, probe="/fixture/probe", engine_sha256=engine_sha, source_sha256=dict(sources))
    identities = {**sources, "libcrprobe.so": engine_sha, "settings.json": write("settings.json", settings)}
    frozen = {"fixture-runtime.py": "a" * 64}
    for second, variant in enumerate(("immediate", "delayed")):
        report, process = deepcopy(probe_fixture())
        process["child_pid"] += second
        for row in report["records"]:
            for census in [row["census"], *row["restored_censuses"]]:
                census["child_pid"] = process["child_pid"]
        process.update(phase="stopped", phase_snapshot_engine=dict(path=settings["probe"], sha256=engine_sha))
        report.update(second_process=bool(second), source_sha256=frozen, engine_sha256=engine_sha)
        write(variant + "/engine/process.json", process)
        write(variant + "/cleanup.json", dict(exit_code=0, error=None, clean=True, listening=False,
            pids_alive=dict(pid=False, child_pid=False), port=PORT, process=process))
        write(variant + "/report.json", report)
        write(variant + "/progress.json", dict(records=report["records"], trajectory_ticks=len(report["trajectory"])))
    write("worker.log", b"")
    build = dict(schema="fair-final-global-rng-snapshot-build.v1", builder_sha256=sha(SNAPSHOT / "build_engine.py"),
        module_sha256=sha(SNAPSHOT / "global_snapshot.inc"), engine_sha256=engine_sha,
        artifacts={"probe/global_snapshot.inc": sha(SNAPSHOT / "global_snapshot.inc"), "libcrprobe.so": engine_sha},
        training_admissible=False, gates_closed=False)
    parent = dict(status="verified_limited_scope", digest_scope=SCOPE, launch_sha256="1" * 64,
        receipt_sha256="2" * 64, engine_sha256=engine_sha, fixed_hog_usage_proven=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)
    launch = dict(schema="fair-final-combat-snapshot-launch.v1", settings=settings, uploaded_sha256=identities,
        runner_sha256=sha(ROOT / "run_combat.py"), verifier_sha256=sha(ROOT / "verify_combat.py"),
        contract_sha256=sha(ROOT / "CONTRACT.md"), build_manifest=build,
        build_manifest_sha256=hashlib.sha256((json.dumps(build, indent=2) + "\n").encode()).hexdigest(),
        frozen_runtime_sha256=frozen, parent_verification=parent,
        parent_launch_sha256=parent["launch_sha256"], parent_receipt_sha256=parent["receipt_sha256"],
        training_admissible=False, gates_closed=False)
    (folder / "launch.json").write_text(json.dumps(launch))
    launch_sha = sha(folder / "launch.json")

    def receipt():
        record = dict(schema="fair-final-combat-snapshot-download.v1", launch_sha256=launch_sha,
            files={str(path.relative_to(archive)): sha(path) for path in archive.rglob("*") if path.is_file()},
            source_worker_stopped=True, training_admissible=False, gates_closed=False)
        (folder / "download.json").write_text(json.dumps(record))
        return sha(folder / "download.json")

    return archive, launch_sha, receipt


def test_complete_archive_verifies_two_processes_and_retains_limits(tmp_path):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    result = validate(tmp_path, launch_sha, receipt())
    assert result["tested_boundaries"] == 6 and result["native_processes"] == 2
    assert not result["gates_closed"] and not result["training_admissible"]


@pytest.mark.parametrize("mutation", ["failure", "cleanup", "source", "binary", "runtime", "progress",
    "missing", "branch", "gate"])
def test_archive_rejects_corruption_even_with_rehashed_receipt(tmp_path, mutation):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    if mutation == "failure":
        (archive / "immediate/failure.json").write_text("{}")
    elif mutation in ("source", "binary"):
        path = archive / ("probe_seed.py" if mutation == "source" else "libcrprobe.so")
        path.write_bytes(path.read_bytes() + b"changed")
    elif mutation == "missing":
        (archive / "delayed/report.json").rename(archive / "delayed/report.missing")
    else:
        path = archive / ("delayed/cleanup.json" if mutation == "cleanup" else
            "delayed/progress.json" if mutation == "progress" else "delayed/report.json")
        content = json.loads(path.read_text())
        if mutation == "cleanup":
            content["pids_alive"]["child_pid"] = True
        elif mutation == "runtime":
            content["source_sha256"] = {}
        elif mutation == "branch":
            content["records"][0]["branches"][1][0]["after"]["digest"]["sha256"] = "f" * 64
            (archive / "delayed/progress.json").write_text(json.dumps(dict(
                records=content["records"], trajectory_ticks=len(content["trajectory"]))))
        elif mutation == "gate":
            content["gates_closed"] = True
        else:
            content["records"].pop()
        path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        validate(tmp_path, launch_sha, receipt())


def test_archive_rejects_wrong_external_hashes(tmp_path):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    receipt_sha = receipt()
    for launch_id, receipt_id in (("0" * 64, receipt_sha), (launch_sha, "0" * 64)):
        with pytest.raises(ValueError, match="external identities"):
            validate(tmp_path, launch_id, receipt_id)
