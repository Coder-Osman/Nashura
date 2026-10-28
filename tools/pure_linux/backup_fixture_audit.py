"""Audit scoped observations of the dedicated test application's Backup requests."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from run_backup_fixture import PACKAGE, validate


def review(record):
    if (record.get("schema") != "nashura.backup-fixture-run.v1" or
            record.get("status") != "RUNTIME_OBSERVED_REQUIRES_REVIEW" or
            record.get("avd") != "nashura-e2-backup-contract-api30" or
            record.get("requests_issued") != 2 or record.get("catalog_ready_pass") is not False):
        raise ValueError("complete dedicated fixture run required")
    before, after = record["identity_before"], record["identity_after"]
    if before != after or before.get("cmdline") != "system_server":
        raise ValueError("system_server identity changed")
    pid = before["pid"]
    validate(record["readiness"], pid, readiness=True)
    observation = record["observation"]
    validate(observation, pid)
    if any(observation[name] for name in ("dropped", "observationErrors", "activeScopes")):
        raise ValueError("incomplete observer coverage")
    if not record.get("commands") or any(command.get("exit_code") != 0 for command in record["commands"]):
        raise ValueError("fixture command failure")
    events = observation["events"]
    platforms = [event for event in events if event["event"] == "platform_scope"]
    if (observation.get("platformScopes") != len(platforms) or
            len({event["details"]["scope"] for event in platforms}) != len(platforms)):
        raise ValueError("platform scope count mismatch")
    requests = [event for event in events if event["event"] == "binder_request"]
    if len(requests) != 2 or any(event["details"].get("packageName") != PACKAGE or
            type(event["details"].get("callingUid")) is not int or
            event["details"]["callingUid"] < 10000 for event in requests):
        raise ValueError("two application UID Binder requests required")
    if len({event["details"]["callingUid"] for event in requests}) != 1:
        raise ValueError("fixture UID changed")
    if any(event["event"].endswith("throw") or event["event"] == "observer_error" for event in events):
        raise ValueError("exception in observed Backup path")
    entries = [event for event in events if event["event"] == "entry"]
    if len(entries) != 2 or len({event["details"]["scope"] for event in entries}) != 2:
        raise ValueError("two distinct fixture entry scopes required")
    branches = []
    for entry in entries:
        details = entry["details"]
        scope = details["scope"]
        rows = [event for event in events if event.get("details", {}).get("scope") == scope]
        if any(event["thread"] != entry["thread"] for event in rows):
            raise ValueError("scope crosses threads")
        if rows[0]["event"] != "entry" or rows[-1]["event"] != "return":
            raise ValueError("incomplete fixture entry")
        if details.get("targetsNull") is not False or details.get("targetMember") is not True:
            raise ValueError("fixture is not a backup participant")
        puts = [event for event in rows if event["event"] == "queue_put_return"]
        if len(puts) != 1 or type(puts[0]["details"].get("previousNull")) is not bool:
            raise ValueError("queue result missing")
        first = puts[0]["details"]["previousNull"]
        if details["state"]["targetPending"] is not (not first):
            raise ValueError("queue result contradicts pending state")
        if rows[-1]["details"]["state"]["targetPending"] is not True:
            raise ValueError("target not retained in queue")
        journal_calls = [event for event in rows if event["event"] == "callee_enter" and
                         ".DataChangedJournal.addPackage(" in event["details"]["method"]]
        writes = [event for event in rows if event["event"] == "callee_enter" and
                  ".writeToJournalLocked(" in event["details"]["method"]]
        if len(journal_calls) != int(first) or len(writes) != int(first):
            raise ValueError("journal branch disagrees with queue result")
        opened = []
        schedules = 0
        for event in rows:
            if event["event"] == "callee_enter":
                opened.append(event["details"]["method"])
                schedules += ".KeyValueBackupJob.schedule(" in opened[-1]
            elif event["event"] == "callee_return":
                if not opened or opened.pop() != event["details"]["method"]:
                    raise ValueError("callee nesting mismatch")
        if opened or schedules == 0:
            raise ValueError("missing return or schedule")
        if any(event["event"] == "job_submit" for event in rows):
            raise ValueError("fixture branch is not scheduler deduplication")
        schedule_states = [event["details"]["state"]["scheduled"] for event in rows
            if event["event"] in {"callee_enter", "callee_return"} and
            ".KeyValueBackupJob.schedule(" in event["details"]["method"]]
        if not schedule_states or any(state is not True for state in schedule_states):
            raise ValueError("scheduler deduplication state missing")
        branches.append("first_enqueue_journal" if first else "duplicate_no_journal")
    if branches != ["first_enqueue_journal", "duplicate_no_journal"]:
        raise ValueError("first and duplicate request sequence required")
    accepted = []
    for event in events:
        if event["event"] != "job_submit":
            continue
        scope = event["details"]["scope"]
        rows = [row for row in events if row.get("details", {}).get("scope") == scope]
        if (rows[0]["event"] != "platform_scope" or
                rows[0]["details"].get("origin") != "platform_schedule_without_fixture_scope"):
            raise ValueError("platform submission origin missing")
        if any(row["thread"] != event["thread"] for row in rows):
            raise ValueError("platform scope crosses threads")
        if sum(row["event"] == "job_submit" for row in rows) != 1:
            raise ValueError("ambiguous platform submission")
        returns = [row for row in rows if row["event"] == "job_submit_return"]
        if len(returns) != 1 or type(returns[0]["details"].get("result")) is not int:
            raise ValueError("job submission return missing")
        if events.index(returns[0]) <= events.index(event):
            raise ValueError("job submission return out of order")
        if returns[0]["details"]["result"] != 1:
            raise ValueError("job was not accepted")
        enters = [row for row in rows if row["event"] == "callee_enter"]
        exits = [row for row in rows if row["event"] == "callee_return"]
        if (not enters or len(enters) != len(exits) or
                enters[0]["details"]["state"]["scheduled"] is not False or
                exits[-1]["details"]["state"]["scheduled"] is not True):
            raise ValueError("scheduler transition missing")
        opened = []
        for row in rows:
            if row["event"] == "callee_enter":
                opened.append(row["details"]["method"])
            elif row["event"] == "callee_return":
                if not opened or opened.pop() != row["details"]["method"]:
                    raise ValueError("platform callee nesting mismatch")
            elif row["event"] in {"job_submit", "job_submit_return"} and not opened:
                raise ValueError("submission outside scheduler call")
        if opened or rows[-1]["event"] != "callee_return":
            raise ValueError("incomplete platform scheduler call")
        accepted.append({key: event["details"][key] for key in
            ("id", "minLatency", "maxDelay", "charging")})
    if not accepted:
        raise ValueError("no independently attributed platform submission")
    return {"schema": "nashura.backup-fixture-audit.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "source": "owned test application; not historical game transaction",
            "branches": branches, "binder_requests": len(requests),
            "platform_accepted_jobs": accepted, "job_delivery": "UNKNOWN",
            "linux_backup_binding": "UNRESOLVED", "catalog_ready_pass": False}


def audit(path, expected_sha, output):
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != expected_sha:
        raise ValueError("fixture run SHA mismatch")
    result = review(json.loads(source))
    result["input_sha256"] = expected_sha
    if path.read_bytes() != source:
        raise ValueError("fixture run changed during audit")
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.input, args.input_sha256, args.output)))


if __name__ == "__main__":
    main()
