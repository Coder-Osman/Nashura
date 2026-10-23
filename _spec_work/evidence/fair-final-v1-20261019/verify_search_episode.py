import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2] / "firstlight_CR"
sys.path.append(str(SOURCE))

from native_runner.contracts import TerminalV1
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.recovery import SCHEMA, validate_identity
from native_runner.training.fair_final.search import SearchLimits
from native_runner.training.fair_final.storage import json_bytes
from tools.probe_fair_final_search_episode import SOURCE_SHA, SearchTrace


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_episode(directory, *, launch, source=SOURCE, expected_phase_names=("prepare", "resume")):
    directory = Path(directory)
    completion = read(directory / "completion.json")
    phases = [dict(phase=name, exit_code=0) for name in expected_phase_names]
    if (completion["error"] is not None or completion["clean"] is not True
            or completion["complete"] is not True or completion["training_admissible"] is not False
            or completion["phases"] != phases or read(directory / "phase-results.json") != phases):
        raise ValueError("search episode completion or phase execution differs")
    engines = completion["engines"]
    if (len(engines) != 2 or {entry["port"] for entry in engines} != set(launch["settings"]["ports"])
            or any(entry["listening"] is not False or entry["process"]["phase"] != "stopped"
                or entry["pids_alive"] != dict(pid=False, child_pid=False) for entry in engines)):
        raise ValueError("search episode owned-engine cleanup incomplete")
    for relative, expected in launch["payload_sha256"].items():
        if sha(directory / relative) != expected:
            raise ValueError("search episode deployed payload differs: " + relative)
    manifest_path = directory / "project/probe-source-manifest.json"
    manifest = read(manifest_path)
    if manifest != launch["source_sha256"] or not manifest:
        raise ValueError("search episode source manifest differs")
    with tarfile.open(directory / "source.tar.gz") as archive:
        members = archive.getmembers()
        if len(members) != len(manifest) or {entry.name for entry in members} != set(manifest):
            raise ValueError("search episode deployed source archive differs")
        for entry in members:
            if (not entry.isfile() or hashlib.sha256(archive.extractfile(entry).read()).hexdigest()
                    != manifest[entry.name] or sha(source / entry.name) != manifest[entry.name]):
                raise ValueError("search episode deployed or current source differs: " + entry.name)
    episode = directory / "episode"
    if any((episode / name).exists() for name in ("prepare-failure.json", "resume-failure.json",
            "continuation-failure.json", "private-audit/quarantine.json")):
        raise ValueError("failed or quarantined search episode")
    report = read(episode / "complete-report.json")
    identity = report["identity"]
    validate_identity(identity)
    if (report["status"] != "passed" or report["training_admissible"] is not False
            or report["purpose"] != "complete-full128-engineering-not-formal-teacher"
            or report["model_unchanged"] is not True or report["source_sha256"] != manifest
            or identity["checkpoint_sha256"] != SOURCE_SHA
            or identity["engine_sha256"] != launch["settings"]["engine_sha256"]
            or identity["source_manifest_sha256"] != sha(manifest_path)
            or report["limits"] != asdict(SearchLimits())):
        raise ValueError("search episode frozen identity or limits differ")
    result = report["result"]
    winner = result["winner"]
    scores = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    terminal = TerminalV1(ended=True, winner=winner, result_by_owner=scores, terminal_tick=result["terminal_tick"])
    if result != terminal_result(terminal, result["native_evidence"], truncated=False, failed=False):
        raise ValueError("search episode terminal labels differ")
    if not (episode / "search-boundaries").is_dir():
        raise ValueError("search episode durable boundaries missing")
    trace = SearchTrace(episode / "search-boundaries")
    groups, roots = {}, []
    search_identity = None
    for path in sorted(trace.root.glob("*.pt.gz")):
        tick = int(path.name.removesuffix(".pt.gz"))
        group = trace.load(tick)
        if path != trace.path(tick) or tick != group["tick"] or tick >= result["terminal_tick"]:
            raise ValueError("search episode boundary order differs")
        if (group["identity"]["search_protocol_sha"] != SearchLimits().sha256
                or any(group["identity"]["checkpoint_" + seat + "_sha"] != SOURCE_SHA for seat in ("blue", "red"))):
            raise ValueError("search episode root model or search protocol differs")
        if search_identity is not None and group["identity"] != search_identity:
            raise ValueError("search episode belief or controller identity changed between roots")
        search_identity = group["identity"]
        groups[tick] = group
        for owner, outcome in sorted(group["outcomes"].items()):
            roots.append(dict(tick=tick, owner=int(owner), signature=outcome["signature"], pending=group["pending"]))
            audit = outcome["audit"]
            if (audit["attempts"] != 128 + outcome["signature"]["result"]["invalid_attempts"]
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                        for value in [audit["seconds"], *audit["timings"].values()])):
                raise ValueError("search episode root audit differs")
    if (not roots or {root["owner"] for root in roots} != {0, 1} or roots != report["roots"]
            or report["total_decisions"] != len(roots) or report["total_valid_simulations"] != 128 * len(roots)):
        raise ValueError("search episode root coverage or totals differ")
    evidence = report["evidence"]
    if len(evidence) != len(roots) or sorted(entry["reservation"] for entry in evidence) != list(range(len(roots))):
        raise ValueError("search episode missing or duplicate reservation outcome")
    ordered_evidence = sorted(evidence, key=lambda entry: entry["reservation"])
    gameplay_end_tick = max(entry["tick"] for entry in ordered_evidence)
    outcomes_by_owner = {owner: [] for owner in (0, 1)}
    for index, entry in enumerate(ordered_evidence):
        root = roots[index]
        action = FullAction(**root["signature"]["selected"])
        due = root["tick"] + (60 if action.card_id is None else 2 * action.delay_slot)
        reason = entry["reason"]
        if (entry["owner"] != root["owner"] or reason not in ("executed", "wait_expired", "preempted", "terminal_expired")
                or not root["tick"] < entry["tick"] <= min(due, result["terminal_tick"])
                or (reason in ("executed", "wait_expired") and entry["tick"] != due)
                or (reason == "executed" and action.card_id is None)
                or (reason == "wait_expired" and action.card_id is not None)
                or (reason == "preempted" and entry["tick"] >= due)
                or (reason == "terminal_expired" and entry["tick"] != gameplay_end_tick)):
            raise ValueError("search episode reservation timing or action type differs")
        outcomes_by_owner[root["owner"]].append((root, entry, due))
    for decisions in outcomes_by_owner.values():
        if not decisions:
            raise ValueError("search episode omits one player's decision trajectory")
        for index, (root, outcome, due) in enumerate(decisions):
            next_root = decisions[index + 1][0] if index + 1 < len(decisions) else None
            if next_root is None:
                if outcome["tick"] != gameplay_end_tick:
                    raise ValueError("final player reservation does not reach the common gameplay boundary")
                if ((outcome["reason"] == "terminal_expired") != (due > gameplay_end_tick)
                        or outcome["reason"] == "preempted"):
                    raise ValueError("final reservation settlement differs from terminal timing")
                continue
            if (outcome["tick"] != next_root["tick"]
                    or outcome["reason"] == "terminal_expired"
                    or (outcome["reason"] == "preempted" and outcome["tick"] >= due)
                    or (outcome["reason"] in ("executed", "wait_expired") and outcome["tick"] != due)):
                raise ValueError("player decision trajectory skips or misclassifies a settled boundary")
    executions = {entry["tick"] for entry in evidence if entry["reason"] == "executed"}
    if any(entry["reason"] == "preempted" and entry["tick"] not in executions for entry in evidence):
        raise ValueError("search episode preemption has no public execution wake")
    first_tick = next(iter(groups))
    if set(groups[first_tick]["information"]) != {"0", "1"}:
        raise ValueError("search episode opening omits one player's frozen view")
    for tick, group in groups.items():
        pending = sorted(owner for owner, decisions in outcomes_by_owner.items()
            if any(root["tick"] < tick < outcome["tick"] for root, outcome, _ in decisions))
        if group["pending"] != pending:
            raise ValueError("search episode pending owners differ from live reservations")
        if tick in executions and set(group["information"]) != {"0", "1"}:
            raise ValueError("public execution did not freeze both player decisions")
    journal = episode / "private-audit"
    recovery_manifest = read(journal / "manifest.json")
    if (recovery_manifest["schema"] != SCHEMA or recovery_manifest["identity"] != identity
            or recovery_manifest["training_admissible"] is not False):
        raise ValueError("search episode recovery identity differs")
    receipt = read(episode / "restart-receipt.json")
    previous = sha(journal / "manifest.json")
    if receipt["manifest_sha"] != previous:
        raise ValueError("search episode recovery manifest receipt differs")
    records, commits = [], []
    for path in sorted(journal.glob("[0-9]*.json")):
        record = read(path)
        body = record["body"]
        if (path.name != f"{len(records):08d}.json" or body["sequence"] != len(records)
                or body["previous_sha"] != previous or hashlib.sha256(json_bytes(body)).hexdigest() != record["sha256"]):
            raise ValueError("search episode recovery hash chain differs")
        previous = record["sha256"]
        records.append(record)
        operation = body["operation"]
        if operation["kind"] == "commit":
            group = groups[operation["before"]["tick"]]
            if (operation["actions"] != {owner: value["signature"]["selected"] for owner, value in group["outcomes"].items()}
                    or operation["before"]["fair"] != {owner: value["fair_sha"] for owner, value in group["information"].items()}):
                raise ValueError("search episode recovered actions or FAIR frames differ")
            commits.append(group["tick"])
        elif operation["kind"] != "checkpoint":
            raise ValueError("unknown search episode recovery operation")
    if commits != list(groups):
        raise ValueError("search episode uncommitted or duplicate actor boundary")
    if (type(receipt["records"]) is not int or not 1 <= receipt["records"] < len(records)
            or records[receipt["records"] - 1]["sha256"] != receipt["record_sha"]):
        raise ValueError("search episode pinned restart head differs")
    prepared = read(episode / "prepare-report.json")
    recovery = read(episode / "recovery-search-report.json")
    before_restart = records[:receipt["records"]]
    count = sum(record["body"]["operation"]["kind"] == "commit" for record in before_restart)
    witness = before_restart[-1]["body"]["operation"]
    if (prepared != dict(status="checkpointed", tick=prepared["tick"], committed_groups=3, training_admissible=False)
            or recovery != dict(status="passed", tick=prepared["tick"], committed_groups=3,
                full128_roots=len(groups[prepared["tick"]]["outcomes"]), training_admissible=False)
            or count != 3 or commits[3] != prepared["tick"] or witness["kind"] != "checkpoint"
            or witness["proof"]["tick"] != prepared["tick"]):
        raise ValueError("search episode restart witness coverage differs")
    final = records[-1]["body"]["operation"]
    digest = hashlib.sha256(b"fair-final-reservation-evidence.v1").hexdigest()
    for entry in evidence:
        digest = hashlib.sha256(bytes.fromhex(digest) + json_bytes(entry)).hexdigest()
    if (final["kind"] != "checkpoint" or final["proof"]["ended"] is not True
            or final["proof"]["tick"] != result["terminal_tick"] or final["proof"]["fair"] != {}
            or final["proof"]["evidence_count"] != len(evidence) or final["proof"]["evidence_sha"] != digest):
        raise ValueError("search episode final recovery proof differs")
    return dict(status="passed", scope="complete Full128 engineering episode and journal-backed process restart",
        training_admissible=False, roots=len(roots), groups=len(groups), valid_simulations=128 * len(roots),
        result=result, lifecycle=dict(Counter(entry["reason"] for entry in evidence)),
        restart_tick=prepared["tick"], gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    launch = read(args.attempt / "launch.json")
    report = validate_episode(args.attempt / "evidence", launch=launch)
    atomic_json(args.attempt / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
