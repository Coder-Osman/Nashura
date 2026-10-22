import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
import tarfile

WORKSPACE = Path(__file__).resolve().parents[3]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"
sys.path[:0] = [str(EVIDENCE), str(WORKSPACE / "firstlight_CR")]

from verify_search_episode import SOURCE, SOURCE_SHA, read, sha
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.recovery import SCHEMA, validate_identity
from native_runner.training.fair_final.search import SearchLimits
from native_runner.training.fair_final.storage import json_bytes
from tools.probe_fair_final_search_episode import SearchTrace


def validate_payload(directory, launch, source):
    manifest = read(directory / "project/probe-source-manifest.json")
    if not manifest or manifest != launch["source_sha256"]:
        raise ValueError("parent source manifest differs")
    for relative, expected in launch["payload_sha256"].items():
        if sha(directory / relative) != expected:
            raise ValueError("parent payload differs: " + relative)
    with tarfile.open(directory / "source.tar.gz") as archive:
        members = archive.getmembers()
        if len(members) != len(manifest) or {entry.name for entry in members} != set(manifest):
            raise ValueError("parent source archive coverage differs")
        for entry in members:
            if (not entry.isfile() or Path(entry.name).is_absolute() or ".." in Path(entry.name).parts
                    or hashlib.sha256(archive.extractfile(entry).read()).hexdigest() != manifest[entry.name]
                    or sha(source / entry.name) != manifest[entry.name]):
                raise ValueError("parent frozen source differs: " + entry.name)
    return manifest


def audit_failed_prefix(attempt, *, source=SOURCE):
    attempt = Path(attempt)
    directory = attempt / "evidence"
    launch = read(attempt / "launch.json")
    download = read(attempt / "download-receipt.json")
    if download["remote"] != launch["remote"] or download["training_admissible"] is not False:
        raise ValueError("parent download identity differs")
    for relative, expected in download["files"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("parent evidence path escapes attempt")
        if ((directory / path).stat().st_size != expected["bytes"]
                or sha(directory / path) != expected["sha256"]):
            raise ValueError("parent downloaded evidence differs: " + relative)
    manifest = validate_payload(directory, launch, source)
    completion = read(directory / "completion.json")
    phases = [dict(phase="prepare", exit_code=0), dict(phase="resume", exit_code=1)]
    if (completion["complete"] is not False or completion["clean"] is not True
            or completion["training_admissible"] is not False or not completion["error"]
            or completion["phases"] != phases or read(directory / "phase-results.json") != phases):
        raise ValueError("parent is not a terminated bounded failed episode")
    if (len(completion["engines"]) != 2
            or {entry["port"] for entry in completion["engines"]} != set(launch["settings"]["ports"])
            or any(entry["listening"] is not False or entry["process"]["phase"] != "stopped"
                or entry["pids_alive"] != dict(pid=False, child_pid=False)
                for entry in completion["engines"])):
        raise ValueError("parent owned-engine cleanup differs")
    episode = directory / "episode"
    if any((episode / name).exists() for name in ("complete-report.json", "prepare-failure.json",
            "private-audit/quarantine.json")):
        raise ValueError("parent has contradictory or quarantined evidence")
    failure = read(episode / "resume-failure.json")
    if (failure["status"] != "failed" or failure["training_admissible"] is not False
            or failure["error"] != "TimeoutError('search deadline before complete effective budget')"):
        raise ValueError("only the verified bounded search timeout permits continuation")
    journal = episode / "private-audit"
    private = read(journal / "manifest.json")
    validate_identity(private["identity"])
    if (private["schema"] != SCHEMA or private["training_admissible"] is not False
            or private["identity"]["checkpoint_sha256"] != SOURCE_SHA
            or private["identity"]["engine_sha256"] != launch["settings"]["engine_sha256"]
            or private["identity"]["source_manifest_sha256"] != sha(directory / "project/probe-source-manifest.json")):
        raise ValueError("parent recovery identity differs")
    trace = SearchTrace(episode / "search-boundaries")
    groups, roots = {}, 0
    search_identity = None
    for path in sorted(trace.root.glob("*.pt.gz")):
        tick = int(path.name.removesuffix(".pt.gz"))
        group = trace.load(tick)
        if path != trace.path(tick) or tick != group["tick"]:
            raise ValueError("parent boundary filename differs")
        if (group["identity"]["search_protocol_sha"] != SearchLimits().sha256
                or any(group["identity"]["checkpoint_" + seat + "_sha"] != SOURCE_SHA
                    for seat in ("blue", "red"))):
            raise ValueError("parent root identity differs")
        if search_identity is not None and group["identity"] != search_identity:
            raise ValueError("parent root protocol changed")
        search_identity = group["identity"]
        for outcome in group["outcomes"].values():
            audit, result = outcome["audit"], outcome["signature"]["result"]
            if (audit["attempts"] != 128 + result["invalid_attempts"]
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                        for value in [audit["seconds"], *audit["timings"].values()])):
                raise ValueError("parent search audit differs")
        roots += len(group["outcomes"])
        groups[tick] = group
    previous = sha(journal / "manifest.json")
    initial_sha = previous
    records, commits = [], []
    for path in sorted(journal.glob("[0-9]*.json")):
        record = read(path)
        body = record["body"]
        if (path.name != f"{len(records):08d}.json" or type(body["sequence"]) is not int
                or body["sequence"] != len(records) or body["previous_sha"] != previous
                or hashlib.sha256(json_bytes(body)).hexdigest() != record["sha256"]):
            raise ValueError("parent recovery chain differs")
        operation = body["operation"]
        if operation["kind"] == "commit":
            tick = operation["before"]["tick"]
            group = groups[tick]
            if (commits and tick <= commits[-1]) or operation["actions"] != {
                    owner: row["signature"]["selected"] for owner, row in group["outcomes"].items()}:
                raise ValueError("parent commit action or order differs")
            if operation["before"]["fair"] != {
                    owner: row["fair_sha"] for owner, row in group["information"].items()}:
                raise ValueError("parent commit FAIR frame differs")
            commits.append(tick)
        elif operation["kind"] != "checkpoint":
            raise ValueError("parent contains unknown journal operation")
        records.append(record)
        previous = record["sha256"]
    if not records or not commits or commits != list(groups):
        raise ValueError("parent durable boundary coverage differs")
    last = records[-1]["body"]["operation"]
    proof = last.get("proof", {})
    if (last["kind"] != "checkpoint" or proof.get("ended") is not False
            or not proof.get("fair") or proof["tick"] <= commits[-1]
            or proof["tick"] != failure["tick"] or failure["committed_groups"] != len(commits)):
        raise ValueError("parent head is not the failed uncommitted decision boundary")
    receipt = read(episode / "restart-receipt.json")
    count = receipt["records"]
    if (receipt["manifest_sha"] != initial_sha or type(count) is not int
            or not 1 <= count < len(records) or records[count - 1]["sha256"] != receipt["record_sha"]):
        raise ValueError("parent original restart receipt differs")
    return dict(schema="FairFinalSearchContinuationInput/v1", status="verified_failed_prefix",
        training_admissible=False, gates_closed=False, parent_attempt=launch["attempt"],
        parent_remote=launch["remote"], parent_launch_sha256=sha(attempt / "launch.json"),
        parent_completion_sha256=sha(directory / "completion.json"),
        parent_download_receipt_sha256=sha(attempt / "download-receipt.json"),
        identity=private["identity"], episode_id=private["episode_id"],
        manifest_sha=initial_sha, record_sha=previous, records=len(records), proof=proof,
        committed_groups=len(commits), roots=roots, valid_simulations=128 * roots,
        source_sha256=manifest, boundary_sha256={path.name: sha(path) for path in sorted(trace.root.glob("*.pt.gz"))})


def audit_checkpoint_prefix(attempt, *, source=SOURCE, _ancestors=None):
    """Validate a clean segment checkpoint before allowing another segment."""
    attempt = Path(attempt).resolve()
    ancestors = set() if _ancestors is None else set(_ancestors)
    if attempt in ancestors:
        raise ValueError("continuation parent chain contains a cycle")
    ancestors.add(attempt)
    directory = attempt / "evidence"
    launch = read(attempt / "launch.json")
    download = read(attempt / "download-receipt.json")
    if download["remote"] != launch["remote"] or download["training_admissible"] is not False:
        raise ValueError("checkpoint download identity differs")
    for relative, expected in download["files"].items():
        path = directory / relative
        if path.is_file() is False or path.stat().st_size != expected["bytes"] or sha(path) != expected["sha256"]:
            raise ValueError("checkpoint evidence differs: " + relative)
    manifest = validate_payload(directory, launch, source)
    completion = read(directory / "completion.json")
    phases = [dict(phase="continue", exit_code=0)]
    if (completion["complete"] is not False or completion["checkpointed"] is not True
            or completion["clean"] is not True or completion["error"] is not None
            or completion["training_admissible"] is not False or completion["phases"] != phases
            or read(directory / "phase-results.json") != phases):
        raise ValueError("checkpoint is not a clean bounded segment")
    if (len(completion["engines"]) != 2
            or {entry["port"] for entry in completion["engines"]} != set(launch["settings"]["ports"])
            or any(entry["listening"] is not False or entry["process"]["phase"] != "stopped"
            or entry["pids_alive"] != dict(pid=False, child_pid=False)
            for entry in completion["engines"])):
        raise ValueError("checkpoint engine cleanup differs")
    episode = directory / "episode"
    if any((episode / name).exists() for name in ("complete-report.json", "continuation-failure.json",
            "prepare-failure.json", "resume-failure.json", "private-audit/quarantine.json")):
        raise ValueError("checkpoint has contradictory evidence")
    segment = read(episode / "segment-report.json")
    head = segment["head"]
    journal = episode / "private-audit"
    private = read(journal / "manifest.json")
    validate_identity(private["identity"])
    if (private["schema"] != SCHEMA or private["training_admissible"] is not False
            or private["identity"]["checkpoint_sha256"] != SOURCE_SHA
            or private["identity"]["engine_sha256"] != launch["settings"]["engine_sha256"]
            or private["identity"]["source_manifest_sha256"] != sha(directory / "project/probe-source-manifest.json")):
        raise ValueError("checkpoint recovery identity differs")
    previous = sha(journal / "manifest.json")
    records, commits = [], []
    for path in sorted(journal.glob("[0-9]*.json")):
        record = read(path)
        body = record["body"]
        if (path.name != f"{len(records):08d}.json" or body["sequence"] != len(records)
                or body["previous_sha"] != previous or hashlib.sha256(json_bytes(body)).hexdigest() != record["sha256"]):
            raise ValueError("checkpoint recovery chain differs")
        operation = body["operation"]
        if operation["kind"] == "commit":
            tick = operation["before"]["tick"]
            if tick in commits or (commits and tick <= commits[-1]):
                raise ValueError("checkpoint commit order differs")
            commits.append(tick)
        elif operation["kind"] != "checkpoint":
            raise ValueError("checkpoint contains unknown journal operation")
        records.append(record)
        previous = record["sha256"]
    proof = segment["proof"]
    if not isinstance(head, dict) or set(head) != {"manifest_sha", "record_sha", "records"}:
        raise ValueError("checkpoint head fields differ")
    if (segment["status"] != "checkpointed" or segment["training_admissible"] is not False
            or segment["gates_closed"] is not False or head["record_sha"] != previous
            or head["manifest_sha"] != sha(journal / "manifest.json")
            or type(head["records"]) is not int or head["records"] != len(records)
            or type(segment["committed_groups"]) is not int or segment["committed_groups"] != len(commits)
            or not records or not isinstance(proof, dict)
            or proof["ended"] is not False or not proof["fair"]
            or records[-1]["body"]["operation"]["kind"] != "checkpoint"
            or records[-1]["body"]["operation"]["proof"] != proof):
        raise ValueError("checkpoint head differs")
    if not commits:
        raise ValueError("checkpoint has no durable decisions")
    trace = SearchTrace(episode / "search-boundaries")
    groups = sorted(trace.root.glob("*.pt.gz"))
    if len(groups) != len(commits):
        raise ValueError("checkpoint search group coverage differs")
    groups_by_tick = {}
    search_identity = None
    for path in groups:
        tick = int(path.name.removesuffix(".pt.gz"))
        group = trace.load(tick)
        if (group["tick"] != tick
                or group["identity"]["search_protocol_sha"] != SearchLimits().sha256
                or group["identity"]["checkpoint_blue_sha"] != SOURCE_SHA
                or group["identity"]["checkpoint_red_sha"] != SOURCE_SHA):
            raise ValueError("checkpoint root identity differs")
        if search_identity is not None and group["identity"] != search_identity:
            raise ValueError("checkpoint root protocol changed")
        search_identity = group["identity"]
        for outcome in group["outcomes"].values():
            audit, result = outcome["audit"], outcome["signature"]["result"]
            if (audit["attempts"] != 128 + result["invalid_attempts"]
                    or result["valid_simulations"] != 128
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                        for value in [audit["seconds"], *audit["timings"].values()])):
                raise ValueError("checkpoint search audit differs")
        groups_by_tick[tick] = group
    if commits != list(groups_by_tick):
        raise ValueError("checkpoint commits do not cover search boundaries in order")
    for record in records:
        operation = record["body"]["operation"]
        if operation["kind"] != "commit":
            continue
        tick = operation["before"]["tick"]
        group = groups_by_tick[tick]
        if (operation["actions"] != {
                owner: row["signature"]["selected"] for owner, row in group["outcomes"].items()}
                or operation["before"]["fair"] != {
                    owner: row["fair_sha"] for owner, row in group["information"].items()}):
            raise ValueError("checkpoint actions or FAIR frames differ from search evidence")

    receipt = read(directory / "continuation-input.json")
    if (sha(directory / "continuation-input.json") != launch["input_sha256"]
            or sha(Path(launch["parent"]) / "launch.json") != launch["parent_launch_sha256"]):
        raise ValueError("checkpoint continuation input or parent launch differs")
    parent = Path(launch["parent"]).resolve()
    parent_completion = read(parent / "evidence/completion.json")
    if parent_completion.get("checkpointed") is True:
        parent_prefix = audit_checkpoint_prefix(parent, source=source, _ancestors=ancestors)
    else:
        parent_prefix = audit_failed_prefix(parent, source=source)
    if receipt != parent_prefix:
        raise ValueError("checkpoint does not extend its verified parent prefix")
    if (type(receipt["records"]) is not int or receipt["records"] < 1
            or receipt["manifest_sha"] != sha(journal / "manifest.json")
            or receipt["records"] > len(records)
            or records[receipt["records"] - 1]["sha256"] != receipt["record_sha"]
            or receipt["committed_groups"] > len(commits)):
        raise ValueError("checkpoint does not retain its pinned recovery prefix")
    inherited_boundaries = {
        name: sha(trace.root / name) for name in receipt["boundary_sha256"]
        if (trace.root / name).is_file()
    }
    if inherited_boundaries != receipt["boundary_sha256"]:
        raise ValueError("checkpoint does not retain its pinned search boundaries")
    prefix_operation = records[receipt["records"] - 1]["body"]["operation"]
    prefix_proof = prefix_operation.get("proof")
    if prefix_operation["kind"] != "checkpoint" or prefix_proof != receipt["proof"]:
        raise ValueError("checkpoint recovery head differs from its pinned parent")
    recovery = read(episode / "recovery-continuation-report.json")
    expected_recovery = dict(status="passed_replay_only", training_admissible=False, gates_closed=False,
        input_sha256=launch["input_sha256"], manifest_sha=receipt["manifest_sha"],
        record_sha=receipt["record_sha"], records=receipt["records"], proof=receipt["proof"],
        committed_groups=receipt["committed_groups"], episode_id=receipt["episode_id"])
    if recovery != expected_recovery:
        raise ValueError("checkpoint native replay witness differs")
    return dict(schema="FairFinalSearchContinuationInput/v1", status="verified_checkpoint_prefix",
        training_admissible=False, gates_closed=False, parent_attempt=launch["attempt"],
        parent_remote=launch["remote"], parent_launch_sha256=sha(attempt / "launch.json"),
        parent_completion_sha256=sha(directory / "completion.json"),
        parent_download_receipt_sha256=sha(attempt / "download-receipt.json"), identity=private["identity"],
        episode_id=private["episode_id"], manifest_sha=sha(journal / "manifest.json"),
        record_sha=previous, records=len(records), proof=proof, committed_groups=len(commits),
        roots=sum(len(group["outcomes"]) for group in groups_by_tick.values()),
        valid_simulations=128 * sum(len(group["outcomes"]) for group in groups_by_tick.values()),
        source_sha256=manifest, origin_failed_attempt=parent_prefix.get(
            "origin_failed_attempt", parent_prefix["parent_attempt"]),
        boundary_sha256={path.name: sha(path) for path in groups})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    report = audit_failed_prefix(args.attempt)
    atomic_json(args.attempt / "continuation-input.json", report)
    print(json.dumps({key: value for key, value in report.items()
        if key not in ("source_sha256", "boundary_sha256")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
