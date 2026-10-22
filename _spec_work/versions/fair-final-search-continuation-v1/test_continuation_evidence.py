import gzip
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys

import pytest
import torch

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parents[2] / "evidence/fair-final-v1-20261019")]

from continuation_evidence import audit_checkpoint_prefix, audit_failed_prefix, json_bytes, read, sha
from test_search_episode_evidence import evidence, group
from verify_search_episode import validate_episode
from verify_continuation import validate_continuation
from native_runner.training.fair_final.policy import LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.search import SearchLimits
from native_runner.training.fair_search.contracts import unpack
from tools.probe_fair_final_search_episode import SearchTrace


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def rechain(attempt):
    episode = attempt / "evidence/episode"
    journal = episode / "private-audit"
    previous = sha(journal / "manifest.json")
    receipt = read(episode / "restart-receipt.json")
    receipt["manifest_sha"] = previous
    for index, path in enumerate(sorted(journal.glob("[0-9]*.json"))):
        value = read(path)
        value["body"]["previous_sha"] = previous
        previous = hashlib.sha256(json_bytes(value["body"])).hexdigest()
        value["sha256"] = previous
        write(path, value)
        if index == receipt["records"] - 1:
            receipt["record_sha"] = previous
    write(episode / "restart-receipt.json", receipt)


def refresh_download(attempt, remote="/fixture/parent"):
    directory = attempt / "evidence"
    files = {str(path.relative_to(directory)): dict(bytes=path.stat().st_size, sha256=sha(path))
        for path in directory.rglob("*") if path.is_file()}
    write(attempt / "download-receipt.json", dict(remote=remote, files=files,
        training_admissible=False))


def checkpoint_attempt(attempt):
    directory = attempt / "evidence"
    episode = directory / "episode"
    complete = episode / "complete-report.json"
    if complete.exists():
        complete.unlink()
    journal = episode / "private-audit"
    final_path = sorted(journal.glob("[0-9]*.json"))[-1]
    final = read(final_path)
    proof = final["body"]["operation"]["proof"]
    proof.update(ended=False, fair={"0": "a" * 64, "1": "b" * 64})
    write(final_path, final)
    rechain(attempt)
    records = sorted(journal.glob("[0-9]*.json"))
    commits = sum(read(path)["body"]["operation"]["kind"] == "commit" for path in records)
    head = dict(manifest_sha=sha(journal / "manifest.json"),
        record_sha=read(records[-1])["sha256"], records=len(records))
    write(episode / "segment-report.json", dict(status="checkpointed", training_admissible=False,
        gates_closed=False, head=head, proof=read(records[-1])["body"]["operation"]["proof"],
        committed_groups=commits, seconds=100.))
    completion = read(directory / "completion.json")
    phases = [dict(phase="continue", exit_code=0)]
    completion.update(complete=False, checkpointed=True, error=None, phases=phases)
    write(directory / "completion.json", completion)
    write(directory / "phase-results.json", phases)
    refresh_download(attempt, remote=read(attempt / "launch.json")["remote"])
    return attempt


def make_checkpoint_parent(tmp_path, failed, source):
    attempt = tmp_path / "checkpoint-parent"
    shutil.copytree(failed, attempt)
    launch = read(attempt / "launch.json")
    launch.update(attempt="fixture-checkpoint-parent", remote="/fixture/checkpoint-parent",
        parent=str(failed), parent_launch_sha256=sha(failed / "launch.json"))
    prefix = audit_failed_prefix(failed, source=source)
    input_path = attempt / "evidence/continuation-input.json"
    write(input_path, prefix)
    launch["input_sha256"] = sha(input_path)
    launch["payload_sha256"]["continuation-input.json"] = launch["input_sha256"]
    launch["settings"].update(wall_seconds=21600, segment_reserve_seconds=1800,
        training_admissible=False)
    write(attempt / "launch.json", launch)
    write(attempt / "evidence/episode/recovery-continuation-report.json", dict(
        status="passed_replay_only", training_admissible=False, gates_closed=False,
        input_sha256=launch["input_sha256"], manifest_sha=prefix["manifest_sha"],
        record_sha=prefix["record_sha"], records=prefix["records"], proof=prefix["proof"],
        committed_groups=prefix["committed_groups"], episode_id=prefix["episode_id"]))
    episode = attempt / "evidence/episode"
    (episode / "resume-failure.json").unlink()
    journal = episode / "private-audit"
    sequence = prefix["records"]
    operation = dict(kind="checkpoint", proof=prefix["proof"])
    body = dict(sequence=sequence, previous_sha=prefix["record_sha"], operation=operation)
    digest = hashlib.sha256(json_bytes(body)).hexdigest()
    write(journal / f"{sequence:08d}.json", dict(body=body, sha256=digest))
    rechain(attempt)
    records = sorted(journal.glob("[0-9]*.json"))
    checkpoint_proof = read(records[-1])["body"]["operation"]["proof"]
    head = dict(manifest_sha=sha(journal / "manifest.json"),
        record_sha=read(records[-1])["sha256"], records=len(records))
    write(episode / "segment-report.json", dict(status="checkpointed", training_admissible=False,
        gates_closed=False, head=head, proof=checkpoint_proof,
        committed_groups=prefix["committed_groups"], seconds=100.))
    phases = [dict(phase="continue", exit_code=0)]
    completion = read(attempt / "evidence/completion.json")
    completion.update(complete=False, checkpointed=True, error=None, phases=phases)
    write(attempt / "evidence/completion.json", completion)
    write(attempt / "evidence/phase-results.json", phases)
    refresh_download(attempt, remote=launch["remote"])
    return attempt


@pytest.fixture
def failed_prefix(tmp_path, evidence):
    directory, launch, source = evidence
    attempt = tmp_path / "parent"
    shutil.copytree(directory, attempt / "evidence")
    directory = attempt / "evidence"
    launch.update(attempt="fixture-parent", remote="/fixture/parent")
    write(attempt / "launch.json", launch)
    (directory / "episode/complete-report.json").unlink()
    completion = read(directory / "completion.json")
    completion.update(complete=False, error="RuntimeError('complete search phase resume failed')")
    completion["phases"][1]["exit_code"] = 1
    write(directory / "completion.json", completion)
    write(directory / "phase-results.json", completion["phases"])
    write(directory / "episode/resume-failure.json", dict(status="failed", training_admissible=False,
        tick=108, committed_groups=4,
        error="TimeoutError('search deadline before complete effective budget')"))
    journal = directory / "episode/private-audit"
    manifest = read(journal / "manifest.json")
    manifest["episode_id"] = "fixture-logical-match"
    write(journal / "manifest.json", manifest)
    last = journal / "00000007.json"
    record = read(last)
    record["body"]["operation"]["proof"].update(ended=False, fair={"0": "a" * 64, "1": "b" * 64})
    write(last, record)
    rechain(attempt)
    refresh_download(attempt)
    return attempt, source


def test_failed_prefix_is_a_recovery_input_and_never_a_terminal_pass(failed_prefix):
    attempt, source = failed_prefix
    result = audit_failed_prefix(attempt, source=source)
    assert result["status"] == "verified_failed_prefix"
    assert result["committed_groups"] == 4
    assert result["roots"] == 8
    assert result["valid_simulations"] == 1024
    assert result["records"] == 8 and result["proof"]["tick"] == 108
    assert result["episode_id"] == "fixture-logical-match"
    assert result["training_admissible"] is False and result["gates_closed"] is False


def test_continuation_phase_requires_explicit_verifier_selection(evidence):
    directory, launch, source = evidence
    completion = read(directory / "completion.json")
    completion["phases"] = [dict(phase="continue", exit_code=0)]
    write(directory / "completion.json", completion)
    write(directory / "phase-results.json", completion["phases"])
    with pytest.raises(ValueError, match="phase"):
        validate_episode(directory, launch=launch, source=source)
    report = validate_episode(directory, launch=launch, source=source, expected_phase_names=("continue",))
    assert report["roots"] == 8 and report["gates_closed"] is False


def test_continuation_failure_marker_blocks_terminal_pass(evidence):
    directory, launch, source = evidence
    write(directory / "episode/continuation-failure.json", dict(status="failed"))
    with pytest.raises(ValueError, match="failed or quarantined"):
        validate_episode(directory, launch=launch, source=source)


@pytest.mark.parametrize("damage", ["raw_file", "chain", "commit", "fair", "visits", "deadline",
    "clean", "engine", "ended", "missing_boundary", "quarantine", "receipt", "final_kind", "duplicate"])
def test_bad_prefix_stays_rejected_even_with_refreshed_download_hashes(failed_prefix, damage):
    attempt, source = failed_prefix
    directory = attempt / "evidence"
    if damage in ("raw_file", "clean", "engine"):
        path = directory / "completion.json"
        value = read(path)
        if damage == "engine":
            value["engines"][0]["pids_alive"]["child_pid"] = True
        else:
            value["clean"] = False
        write(path, value)
    elif damage == "deadline":
        path = directory / "episode/resume-failure.json"
        value = read(path)
        value["error"] = "ValueError('protocol rejected')"
        write(path, value)
    elif damage == "visits":
        path = directory / "episode/search-boundaries/00000100.pt.gz"
        value = torch.load(io.BytesIO(gzip.decompress(path.read_bytes())), weights_only=True)
        value["outcomes"]["0"]["signature"]["result"]["valid_simulations"] = 127
        buffer = io.BytesIO()
        torch.save(value, buffer)
        path.write_bytes(gzip.compress(buffer.getvalue()))
    elif damage == "missing_boundary":
        (directory / "episode/search-boundaries/00000100.pt.gz").unlink()
    elif damage == "quarantine":
        write(directory / "episode/private-audit/quarantine.json", {"reason": "rejected"})
    elif damage == "receipt":
        path = directory / "episode/restart-receipt.json"
        value = read(path)
        value["record_sha"] = "f" * 64
        write(path, value)
    else:
        index = "00000007" if damage in ("ended", "final_kind") else "00000006"
        path = directory / f"episode/private-audit/{index}.json"
        value = read(path)
        operation = value["body"]["operation"]
        if damage == "ended":
            operation["proof"]["ended"] = True
        elif damage == "final_kind":
            operation["kind"] = "unknown"
        elif damage == "commit":
            operation["actions"]["0"] = dict(card_id=None, cell=None, delay_slot=None)
        elif damage == "fair":
            operation["before"]["fair"]["0"] = "d" * 64
        elif damage == "duplicate":
            earlier = read(directory / "episode/private-audit/00000004.json")
            value["body"]["operation"] = earlier["body"]["operation"]
        else:
            value["sha256"] = "f" * 64
        write(path, value)
        if damage != "chain":
            rechain(attempt)
    if damage != "raw_file":
        refresh_download(attempt)
    with pytest.raises((ValueError, KeyError)):
        audit_failed_prefix(attempt, source=source)


@pytest.fixture
def completed_continuation(tmp_path, failed_prefix):
    parent, source = failed_prefix
    parent_directory = parent / "evidence"
    parent_trace = SearchTrace(parent_directory / "episode/search-boundaries")
    next_group = deepcopy(parent_trace.load(106))
    next_group["tick"] = 108
    for seat in ("0", "1"):
        encoded = next_group["information"][seat]
        info = DecisionInformation(int(seat), 108, unpack(encoded["frame"]),
            LegalPrefixes(encoded["card_ids"], unpack(encoded["plays"])))
        encoded["fair_sha"] = info.sha256
        next_group["outcomes"][seat]["signature"]["result"]["root_fair_state_sha"] = info.sha256
    path = parent_directory / "episode/private-audit/00000007.json"
    value = read(path)
    value["body"]["operation"]["proof"]["fair"] = {
        seat: row["fair_sha"] for seat, row in next_group["information"].items()}
    write(path, value)
    rechain(parent)
    refresh_download(parent)
    receipt = audit_failed_prefix(parent, source=source)
    attempt = tmp_path / "continuation"
    directory = attempt / "evidence"
    shutil.copytree(parent_directory, directory)
    (directory / "episode/resume-failure.json").unlink()
    trace = SearchTrace(directory / "episode/search-boundaries")
    trace.write(next_group)
    identity = read(directory / "episode/private-audit/manifest.json")["identity"]
    roots, outcomes = [], []
    for tick in (100, 102, 104, 106, 108):
        row = trace.load(tick)
        for owner, outcome in sorted(row["outcomes"].items()):
            roots.append(dict(tick=tick, owner=int(owner), signature=outcome["signature"], pending=[]))
            outcomes.append(dict(reservation=len(outcomes), owner=int(owner), tick=tick + 2, reason="executed"))
    write(directory / "episode/complete-report.json", dict(status="passed", training_admissible=False,
        purpose="complete-full128-engineering-not-formal-teacher", identity=identity,
        limits=SearchLimits().__dict__, model_unchanged=True, total_decisions=10, total_valid_simulations=1280,
        source_sha256=receipt["source_sha256"], roots=roots, evidence=outcomes,
        result=dict(winner=0, terminal_tick=110, labels=["WIN", "LOSS"],
            native_evidence=dict(finalized=True, worldResult=0, worldResultRaw=0, winner=0, crownsRaw=[1, 0]))))
    journal = directory / "episode/private-audit"
    operations = [dict(kind="commit", before=dict(tick=108, fair={
        seat: row["fair_sha"] for seat, row in next_group["information"].items()}),
        actions={seat: row["signature"]["selected"] for seat, row in next_group["outcomes"].items()})]
    digest = hashlib.sha256(b"fair-final-reservation-evidence.v1").hexdigest()
    for outcome in outcomes:
        digest = hashlib.sha256(bytes.fromhex(digest) + json_bytes(outcome)).hexdigest()
    operations.append(dict(kind="checkpoint", proof=dict(ended=True, tick=110, fair={},
        evidence_count=10, evidence_sha=digest)))
    previous = receipt["record_sha"]
    for sequence, operation in enumerate(operations, start=8):
        body = dict(sequence=sequence, previous_sha=previous, operation=operation)
        previous = hashlib.sha256(json_bytes(body)).hexdigest()
        write(journal / f"{sequence:08d}.json", dict(body=body, sha256=previous))
    completion = read(directory / "completion.json")
    completion.update(complete=True, error=None, phases=[dict(phase="continue", exit_code=0)])
    write(directory / "completion.json", completion)
    write(directory / "phase-results.json", completion["phases"])
    launch = read(parent / "launch.json")
    launch.update(parent=str(parent), parent_launch_sha256=sha(parent / "launch.json"))
    launch["settings"].update(wall_seconds=21600, segment_reserve_seconds=1800, training_admissible=False)
    write(directory / "settings.json", launch["settings"])
    write(directory / "continuation-input.json", receipt)
    launch["input_sha256"] = sha(directory / "continuation-input.json")
    launch["payload_sha256"]["settings.json"] = sha(directory / "settings.json")
    launch["payload_sha256"]["continuation-input.json"] = launch["input_sha256"]
    write(attempt / "launch.json", launch)
    write(directory / "episode/recovery-continuation-report.json", dict(status="passed_replay_only",
        training_admissible=False, gates_closed=False, input_sha256=launch["input_sha256"],
        manifest_sha=receipt["manifest_sha"], record_sha=receipt["record_sha"], records=receipt["records"],
        proof=receipt["proof"], committed_groups=receipt["committed_groups"], episode_id=receipt["episode_id"]))
    return attempt, source


def test_complete_continuation_requires_terminal_and_preserves_parent_failure(completed_continuation):
    attempt, source = completed_continuation
    report = validate_continuation(attempt, source=source)
    assert report["roots"] == 10 and report["valid_simulations"] == 1280
    assert report["continued_from_tick"] == 108 and report["parent_prefix_roots"] == 8
    assert report["parent_failure_preserved"] is True
    assert report["training_admissible"] is False and report["gates_closed"] is False


def test_checkpoint_prefix_validates_search_actions_and_origin_failure(completed_continuation):
    attempt, source = completed_continuation
    checkpoint_attempt(attempt)
    receipt = audit_checkpoint_prefix(attempt, source=source)
    assert receipt["status"] == "verified_checkpoint_prefix"
    assert receipt["roots"] == 10 and receipt["valid_simulations"] == 1280
    assert receipt["origin_failed_attempt"] == "fixture-parent"
    assert receipt["training_admissible"] is False and receipt["gates_closed"] is False


def test_checkpoint_prefix_accepts_top_level_segment_summary_fields(completed_continuation):
    attempt, source = completed_continuation
    checkpoint_attempt(attempt)
    report_path = attempt / "evidence/episode/segment-report.json"
    report = read(report_path)
    assert set(report["head"]) == {"manifest_sha", "record_sha", "records"}
    receipt = audit_checkpoint_prefix(attempt, source=source)
    assert receipt["status"] == "verified_checkpoint_prefix"
    assert receipt["committed_groups"] == 5


@pytest.mark.parametrize("field", ["manifest_sha", "records", "committed_groups", "proof", "nested_proof"])
def test_checkpoint_summary_tampering_is_rejected_after_rehash(completed_continuation, field):
    attempt, source = completed_continuation
    checkpoint_attempt(attempt)
    report_path = attempt / "evidence/episode/segment-report.json"
    report = read(report_path)
    if field == "manifest_sha":
        report["head"][field] = "0" * 64
    elif field == "records":
        report["head"][field] += 1
    elif field == "committed_groups":
        report[field] += 1
    elif field == "proof":
        report[field]["native_digest"] = "0" * 64
    else:
        report["head"]["proof"] = report["proof"]
    write(report_path, report)
    refresh_download(attempt, remote=read(attempt / "launch.json")["remote"])
    with pytest.raises(ValueError, match="checkpoint head"):
        audit_checkpoint_prefix(attempt, source=source)


def test_checkpoint_chain_recursively_preserves_each_parent(completed_continuation, tmp_path):
    parent, source = completed_continuation
    checkpoint_attempt(parent)
    prefix = audit_checkpoint_prefix(parent, source=source)
    child = tmp_path / "second-segment"
    shutil.copytree(parent, child)
    launch = read(child / "launch.json")
    launch.update(attempt="fixture-second-segment", remote="/fixture/second-segment",
        parent=str(parent), parent_launch_sha256=sha(parent / "launch.json"))
    directory = child / "evidence"
    input_path = directory / "continuation-input.json"
    write(input_path, prefix)
    launch["input_sha256"] = sha(input_path)
    launch["payload_sha256"]["continuation-input.json"] = launch["input_sha256"]
    write(child / "launch.json", launch)
    write(directory / "episode/recovery-continuation-report.json", dict(
        status="passed_replay_only", training_admissible=False, gates_closed=False,
        input_sha256=launch["input_sha256"], manifest_sha=prefix["manifest_sha"],
        record_sha=prefix["record_sha"], records=prefix["records"], proof=prefix["proof"],
        committed_groups=prefix["committed_groups"], episode_id=prefix["episode_id"]))
    refresh_download(child, remote=launch["remote"])
    second_prefix = audit_checkpoint_prefix(child, source=source)
    assert second_prefix["origin_failed_attempt"] == "fixture-parent"
    assert second_prefix["roots"] == prefix["roots"]


def test_complete_continuation_accepts_a_verified_checkpoint_parent(
        completed_continuation, failed_prefix, tmp_path):
    child, source = completed_continuation
    failed, _ = failed_prefix
    checkpoint = make_checkpoint_parent(tmp_path, failed, source)
    prefix = audit_checkpoint_prefix(checkpoint, source=source)
    directory = child / "evidence"
    launch = read(child / "launch.json")
    launch.update(parent=str(checkpoint), parent_launch_sha256=sha(checkpoint / "launch.json"))
    journal = directory / "episode/private-audit"
    shutil.rmtree(journal)
    shutil.copytree(checkpoint / "evidence/episode/private-audit", journal)
    group = SearchTrace(directory / "episode/search-boundaries").load(108)
    operations = [dict(kind="commit", before=dict(tick=108, fair={
        seat: row["fair_sha"] for seat, row in group["information"].items()}),
        actions={seat: row["signature"]["selected"] for seat, row in group["outcomes"].items()})]
    terminal = read(directory / "episode/complete-report.json")["result"]["terminal_tick"]
    outcomes = read(directory / "episode/complete-report.json")["evidence"]
    evidence_sha = hashlib.sha256(b"fair-final-reservation-evidence.v1").hexdigest()
    for outcome in outcomes:
        evidence_sha = hashlib.sha256(bytes.fromhex(evidence_sha) + json_bytes(outcome)).hexdigest()
    operations.append(dict(kind="checkpoint", proof=dict(ended=True, tick=terminal, fair={},
        evidence_count=len(outcomes), evidence_sha=evidence_sha)))
    previous = prefix["record_sha"]
    for offset, operation in enumerate(operations):
        sequence = prefix["records"] + offset
        body = dict(sequence=sequence, previous_sha=previous, operation=operation)
        previous = hashlib.sha256(json_bytes(body)).hexdigest()
        write(journal / f"{sequence:08d}.json", dict(body=body, sha256=previous))
    rechain(child)
    write(directory / "continuation-input.json", prefix)
    launch["input_sha256"] = sha(directory / "continuation-input.json")
    launch["payload_sha256"]["continuation-input.json"] = launch["input_sha256"]
    write(child / "launch.json", launch)
    write(directory / "episode/recovery-continuation-report.json", dict(
        status="passed_replay_only", training_admissible=False, gates_closed=False,
        input_sha256=launch["input_sha256"], manifest_sha=prefix["manifest_sha"],
        record_sha=prefix["record_sha"], records=prefix["records"], proof=prefix["proof"],
        committed_groups=prefix["committed_groups"], episode_id=prefix["episode_id"]))
    refresh_download(child, remote=launch["remote"])
    report = validate_continuation(child, source=source)
    assert report["parent_failure_preserved"] is False
    assert report["parent_checkpoint_preserved"] is True
    assert report["origin_failed_attempt"] == "fixture-parent"


def test_checkpoint_prefix_rejects_rechained_actor_action_tampering(completed_continuation):
    attempt, source = completed_continuation
    checkpoint_attempt(attempt)
    journal = attempt / "evidence/episode/private-audit"
    path = journal / "00000008.json"
    record = read(path)
    record["body"]["operation"]["actions"]["0"] = dict(card_id=None, cell=None, delay_slot=None)
    write(path, record)
    rechain(attempt)
    segment_path = attempt / "evidence/episode/segment-report.json"
    segment = read(segment_path)
    records = sorted(journal.glob("[0-9]*.json"))
    segment["head"]["record_sha"] = read(records[-1])["sha256"]
    write(segment_path, segment)
    refresh_download(attempt, remote=read(attempt / "launch.json")["remote"])
    with pytest.raises(ValueError, match="actions or FAIR"):
        audit_checkpoint_prefix(attempt, source=source)


@pytest.mark.parametrize("damage", ["head", "prefix", "input", "limit", "terminal", "phase", "failure"])
def test_continuation_does_not_bypass_original_full_episode_validation(completed_continuation, damage):
    attempt, source = completed_continuation
    directory = attempt / "evidence"
    if damage == "head":
        path = directory / "episode/recovery-continuation-report.json"
        value = read(path)
        value["proof"]["native_digest"] = "f" * 64
    elif damage == "prefix":
        path = directory / "episode/private-audit/00000007.json"
        value = read(path)
        value["body"]["operation"]["proof"]["tick"] = 109
    elif damage == "input":
        path = directory / "continuation-input.json"
        value = read(path)
        value["record_sha"] = "f" * 64
    elif damage == "limit":
        path = attempt / "launch.json"
        value = read(path)
        value["settings"]["wall_seconds"] += 1
    elif damage == "terminal":
        path = directory / "episode/complete-report.json"
        value = read(path)
        value["result"]["native_evidence"]["finalized"] = False
    elif damage == "failure":
        path = directory / "episode/continuation-failure.json"
        value = {"status": "failed"}
    else:
        path = directory / "completion.json"
        value = read(path)
        value["phases"][0]["exit_code"] = 1
    write(path, value)
    with pytest.raises(ValueError):
        validate_continuation(attempt, source=source)
