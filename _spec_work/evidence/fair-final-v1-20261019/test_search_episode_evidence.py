from copy import deepcopy
import hashlib
import io
import json
import tarfile

import pytest

from verify_search_episode import SOURCE_SHA, json_bytes, sha, validate_episode
from native_runner.tests.test_fair_final_search_episode_probe import group
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.policy import LegalPrefixes
from native_runner.training.fair_final.recovery import SCHEMA
from native_runner.training.fair_final.search import SearchLimits
from native_runner.training.fair_search.contracts import unpack
from tools.probe_fair_final_search_episode import SearchTrace


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture
def evidence(tmp_path, group):
    directory, source = tmp_path / "evidence", tmp_path / "source"
    source.mkdir()
    (source / "fixture.py").write_text("fixture = True\n")
    manifest = {"fixture.py": sha(source / "fixture.py")}
    write(directory / "project/probe-source-manifest.json", manifest)
    with tarfile.open(directory / "source.tar.gz", "w:gz") as archive:
        archive.add(source / "fixture.py", arcname="fixture.py")
    settings = dict(ports=[1, 2], engine_sha256="c" * 64)
    write(directory / "settings.json", settings)
    launch = dict(settings=settings, source_sha256=manifest, payload_sha256={relative: sha(directory / relative)
        for relative in ("settings.json", "source.tar.gz", "project/probe-source-manifest.json")})
    phases = [dict(phase=name, exit_code=0) for name in ("prepare", "resume")]
    write(directory / "phase-results.json", phases)
    write(directory / "completion.json", dict(error=None, clean=True, complete=True, training_admissible=False,
        phases=phases, engines=[dict(port=port, listening=False, process=dict(phase="stopped"),
            pids_alive=dict(pid=False, child_pid=False)) for port in (1, 2)]))
    identity = dict(checkpoint_sha256=SOURCE_SHA, engine_sha256="c" * 64,
        source_manifest_sha256=sha(directory / "project/probe-source-manifest.json"), controller="fixture", adapter="fixture")
    episode = directory / "episode"
    trace = SearchTrace(episode / "search-boundaries")
    roots, groups, outcomes = [], [], []
    for tick in (100, 102, 104, 106):
        value = deepcopy(group)
        value["tick"], value["pending"] = tick, []
        for seat in ("0", "1"):
            value["information"][seat] = deepcopy(group["information"]["0"])
            value["outcomes"][seat] = deepcopy(group["outcomes"]["0"])
            encoded = value["information"][seat]
            info = DecisionInformation(int(seat), tick, unpack(encoded["frame"]),
                LegalPrefixes(encoded["card_ids"], unpack(encoded["plays"])))
            encoded["fair_sha"] = info.sha256
            result = value["outcomes"][seat]["signature"]["result"]
            result.update(root_fair_state_sha=info.sha256, root_player=int(seat), value_perspective=int(seat))
            roots.append(dict(tick=tick, owner=int(seat), signature=value["outcomes"][seat]["signature"], pending=[]))
            outcomes.append(dict(reservation=len(outcomes), owner=int(seat), tick=tick + 2, reason="executed"))
        for seat in ("blue", "red"):
            value["identity"]["checkpoint_" + seat + "_sha"] = SOURCE_SHA
        trace.write(value)
        groups.append(value)
    result = dict(winner=0, terminal_tick=108, labels=["WIN", "LOSS"],
        native_evidence=dict(finalized=True, worldResult=0, worldResultRaw=0, winner=0, crownsRaw=[1, 0]))
    report = dict(status="passed", training_admissible=False, purpose="complete-full128-engineering-not-formal-teacher",
        identity=identity, limits=SearchLimits().__dict__, result=result, roots=roots, evidence=outcomes,
        source_sha256=manifest, total_decisions=8, total_valid_simulations=1024, model_unchanged=True)
    write(episode / "complete-report.json", report)
    journal = episode / "private-audit"
    write(journal / "manifest.json", dict(schema=SCHEMA, identity=identity, training_admissible=False))
    previous, sequence = sha(journal / "manifest.json"), 0

    def append(operation):
        nonlocal previous, sequence
        body = dict(sequence=sequence, previous_sha=previous, operation=operation)
        previous = hashlib.sha256(json_bytes(body)).hexdigest()
        write(journal / f"{sequence:08d}.json", dict(body=body, sha256=previous))
        sequence += 1

    for index, value in enumerate(groups):
        append(dict(kind="commit", before=dict(tick=value["tick"],
            fair={seat: row["fair_sha"] for seat, row in value["information"].items()}),
            actions={seat: row["signature"]["selected"] for seat, row in value["outcomes"].items()}))
        if index < 3:
            append(dict(kind="checkpoint", proof=dict(tick=groups[index + 1]["tick"])))
        if index == 2:
            write(episode / "restart-receipt.json", dict(manifest_sha=sha(journal / "manifest.json"),
                record_sha=previous, records=sequence))
    digest = hashlib.sha256(b"fair-final-reservation-evidence.v1").hexdigest()
    for entry in outcomes:
        digest = hashlib.sha256(bytes.fromhex(digest) + json_bytes(entry)).hexdigest()
    append(dict(kind="checkpoint", proof=dict(ended=True, tick=108, fair={}, evidence_count=8, evidence_sha=digest)))
    write(episode / "prepare-report.json", dict(status="checkpointed", tick=106, committed_groups=3, training_admissible=False))
    write(episode / "recovery-search-report.json", dict(status="passed", tick=106, committed_groups=3,
        full128_roots=2, training_admissible=False))
    return directory, launch, source


def test_complete_search_episode_evidence_has_limited_acceptance(evidence):
    directory, launch, source = evidence
    result = validate_episode(directory, launch=launch, source=source)
    assert result["roots"] == 8 and result["valid_simulations"] == 1024
    assert result["training_admissible"] is False and result["gates_closed"] is False


@pytest.mark.parametrize("damage", ["completion", "engine", "payload", "source", "terminal", "labels",
    "visits", "target", "frame", "unresolved", "timing", "model", "chain", "receipt", "recovery", "failure"])
def test_invalid_search_episode_evidence_never_passes(evidence, damage):
    directory, launch, source = evidence
    episode = directory / "episode"
    if damage == "payload":
        (directory / "settings.json").write_text("{}")
    elif damage == "source":
        (source / "fixture.py").write_text("fixture = False\n")
    elif damage == "failure":
        write(episode / "resume-failure.json", dict(status="failed"))
    elif damage in ("visits", "target", "frame", "model"):
        trace = SearchTrace(episode / "search-boundaries")
        value = trace.load(100)
        if damage == "visits":
            value["outcomes"]["0"]["signature"]["result"]["valid_simulations"] = 127
        elif damage == "target":
            value["outcomes"]["0"]["signature"]["target"][0][1] += 1
        elif damage == "frame":
            value["information"]["0"]["fair_sha"] = "d" * 64
        else:
            value["identity"]["checkpoint_blue_sha"] = "d" * 64
        import gzip
        import torch
        buffer = io.BytesIO()
        torch.save(value, buffer)
        trace.path(100).write_bytes(gzip.compress(buffer.getvalue()))
    else:
        path = {"completion": directory / "completion.json", "engine": directory / "completion.json",
            "terminal": episode / "complete-report.json", "labels": episode / "complete-report.json",
            "unresolved": episode / "complete-report.json", "timing": episode / "complete-report.json",
            "chain": episode / "private-audit/00000006.json", "receipt": episode / "restart-receipt.json",
            "recovery": episode / "recovery-search-report.json"}[damage]
        value = json.loads(path.read_text())
        if damage == "completion":
            value["complete"] = False
        elif damage == "engine":
            value["engines"][0]["pids_alive"]["child_pid"] = True
        elif damage == "terminal":
            value["result"]["native_evidence"]["finalized"] = False
        elif damage == "labels":
            value["result"]["labels"] = ["LOSS", "WIN"]
        elif damage == "unresolved":
            value["evidence"].pop()
        elif damage == "timing":
            value["evidence"][0]["tick"] = 100
        elif damage == "chain":
            value["body"]["operation"]["actions"]["0"] = dict(card_id=None, cell=None, delay_slot=None)
            value["sha256"] = "e" * 64
        elif damage == "receipt":
            value["record_sha"] = "e" * 64
        else:
            value["full128_roots"] = 1
        write(path, value)
    with pytest.raises(ValueError):
        validate_episode(directory, launch=launch, source=source)


def test_incomplete_live_episode_has_no_acceptance(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_episode(tmp_path, launch={})


@pytest.mark.parametrize("reservation,tick,reason", [
    (0, 101, "preempted"),
    (6, 107, "preempted"),
])
def test_search_episode_rejects_missing_next_boundary_or_early_terminal_settlement(
        evidence, reservation, tick, reason):
    directory, launch, source = evidence
    report_path = directory / "episode/complete-report.json"
    report = json.loads(report_path.read_text())
    report["evidence"][reservation]["tick"] = tick
    report["evidence"][reservation]["reason"] = reason
    write(report_path, report)
    with pytest.raises(ValueError, match="trajectory|timing|gameplay"):
        validate_episode(directory, launch=launch, source=source)


def replace_outcomes(directory, update):
    episode = directory / "episode"
    report_path = episode / "complete-report.json"
    report = json.loads(report_path.read_text())
    update(report)
    write(report_path, report)
    digest = hashlib.sha256(b"fair-final-reservation-evidence.v1").hexdigest()
    for entry in report["evidence"]:
        digest = hashlib.sha256(bytes.fromhex(digest) + json_bytes(entry)).hexdigest()
    path = episode / "private-audit/00000007.json"
    record = json.loads(path.read_text())
    record["body"]["operation"]["proof"].update(tick=report["result"]["terminal_tick"], evidence_sha=digest)
    record["sha256"] = hashlib.sha256(json_bytes(record["body"])).hexdigest()
    write(path, record)


@pytest.mark.parametrize("terminal_tick", [107, 108, 109])
def test_gameplay_close_and_native_finalization_are_distinct(evidence, terminal_tick):
    directory, launch, source = evidence
    def update(report):
        report["result"]["terminal_tick"] = terminal_tick
        for entry in report["evidence"][-2:]:
            entry.update(tick=107, reason="terminal_expired")
    replace_outcomes(directory, update)
    result = validate_episode(directory, launch=launch, source=source)
    assert result["result"]["terminal_tick"] == terminal_tick
    assert result["lifecycle"]["terminal_expired"] == 2


def test_deadline_at_gameplay_close_still_requires_due_settlement(evidence):
    directory, launch, source = evidence
    def update(report):
        for entry in report["evidence"][-2:]:
            entry["reason"] = "terminal_expired"
    replace_outcomes(directory, update)
    with pytest.raises(ValueError, match="terminal timing"):
        validate_episode(directory, launch=launch, source=source)


@pytest.mark.parametrize("public_execution", [False, True])
def test_preemption_requires_public_execution_after_hashes_match(evidence, public_execution):
    directory, launch, source = evidence
    episode = directory / "episode"
    trace = SearchTrace(episode / "search-boundaries")
    value = trace.load(100)
    preempted_owners = (0,) if public_execution else (0, 1)
    for owner in preempted_owners:
        value["outcomes"][str(owner)]["signature"]["selected"]["delay_slot"] = 2
    import gzip
    import torch
    buffer = io.BytesIO()
    torch.save(value, buffer)
    trace.path(100).write_bytes(gzip.compress(buffer.getvalue()))
    def update(report):
        for owner in preempted_owners:
            report["roots"][owner]["signature"] = value["outcomes"][str(owner)]["signature"]
            report["evidence"][owner]["reason"] = "preempted"
    replace_outcomes(directory, update)
    journal = episode / "private-audit"
    receipt_path = episode / "restart-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    previous = sha(journal / "manifest.json")
    for path in sorted(journal.glob("[0-9]*.json")):
        record = json.loads(path.read_text())
        body = record["body"]
        if body["sequence"] == 0:
            body["operation"]["actions"] = {
                owner: outcome["signature"]["selected"] for owner, outcome in value["outcomes"].items()}
        body["previous_sha"] = previous
        previous = record["sha256"] = hashlib.sha256(json_bytes(body)).hexdigest()
        write(path, record)
        if body["sequence"] == receipt["records"] - 1:
            receipt["record_sha"] = previous
    write(receipt_path, receipt)
    if public_execution:
        result = validate_episode(directory, launch=launch, source=source)
        assert result["lifecycle"]["preempted"] == 1
    else:
        with pytest.raises(ValueError, match="public execution"):
            validate_episode(directory, launch=launch, source=source)


@pytest.mark.parametrize("damage", ["action", "fair", "ended", "evidence_hash"])
def test_rehashed_journal_still_requires_matching_actor_and_terminal_evidence(evidence, damage):
    directory, launch, source = evidence
    journal = directory / "episode/private-audit"
    previous = json.loads((journal / "00000005.json").read_text())["sha256"]
    for sequence in (6, 7):
        path = journal / f"{sequence:08d}.json"
        value = json.loads(path.read_text())
        operation = value["body"]["operation"]
        if sequence == 6 and damage == "action":
            operation["actions"].pop("1")
        elif sequence == 6 and damage == "fair":
            operation["before"]["fair"]["0"] = "d" * 64
        elif sequence == 7 and damage == "ended":
            operation["proof"]["ended"] = False
        elif sequence == 7 and damage == "evidence_hash":
            operation["proof"]["evidence_sha"] = "d" * 64
        value["body"]["previous_sha"] = previous
        previous = value["sha256"] = hashlib.sha256(json_bytes(value["body"])).hexdigest()
        write(path, value)
    with pytest.raises(ValueError, match="recovered actions|final recovery proof"):
        validate_episode(directory, launch=launch, source=source)
