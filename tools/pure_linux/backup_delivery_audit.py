"""Audit limited JobScheduler callback evidence, without claiming transport parity."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from run_backup_delivery import HOOKS, JOB, PACKAGE, validate


WORKER_COVERAGE = "UNINSTRUMENTED_AFTER_OBSERVER_CRASH"
CALLBACK = JOB + "onStartJob(android.app.job.JobParameters)"
BACKUP = "com.android.server.backup.UserBackupManagerService.backupNow()"
CANCEL = JOB + "cancel(int,android.content.Context)"
CLEAR = JOB + "clearScheduledForUserId(int)"
TRIGGER = "controlled_forced_JobScheduler_not_natural_expiry"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def review(record):
    if (record.get("schema") != "nashura.backup-delivery-run.v1" or
            record.get("status") != "RUNTIME_OBSERVED_REQUIRES_REVIEW" or
            record.get("avd") != "nashura-e2-backup-contract-api30" or
            record.get("trigger") != TRIGGER or
            record.get("catalog_ready_pass") is not False or
            any(type(record.get(key)) is not int or record[key] != expected
                for key, expected in (("requests_issued", 1), ("forced_jobs_issued", 1),
                                      ("stores_issued", 0)))):
        raise ValueError("complete isolated controlled delivery required")
    before, after = record["identity_before"], record["identity_after"]
    if (before != after or before.get("cmdline") != "system_server" or
            type(before.get("pid")) is not int or before["pid"] <= 0 or
            type(before.get("starttime")) is not int or before["starttime"] <= 0 or
            not isinstance(before.get("boot_id"), str) or not before["boot_id"]):
        raise ValueError("system_server identity missing or changed")
    pid = before["pid"]
    validate(record["readiness"], pid, readiness=True)
    if record["readiness"].get("workerCoverage") != WORKER_COVERAGE:
        raise ValueError("worker coverage limit must remain explicit")
    observation, prelude = record["observation"], record["prelude"]
    validate(observation, pid)
    validate(prelude, pid)
    start = record["experiment_start_sequence"]
    events = observation["events"]
    if (type(start) is not int or start != len(prelude["events"]) or
            events[:start] != prelude["events"]):
        raise ValueError("preparation and experiment boundary mismatch")
    stacks, calls, completed = {}, {}, {}
    previous_time = -1
    for sequence, event in enumerate(events):
        thread, moment = event.get("thread"), event.get("monotonicMs")
        if (type(event.get("sequence")) is not int or event["sequence"] != sequence or
                type(thread) is not int or thread <= 0 or
                type(moment) is not int or moment < previous_time):
            raise ValueError("invalid event order, thread or monotonic timestamp")
        previous_time = moment
        details = event["details"]
        call, method = details.get("call"), details.get("method")
        if not isinstance(call, str) or not call.isdecimal() or method not in HOOKS:
            raise ValueError("unknown call or method")
        stack = stacks.setdefault(thread, [])
        if event["event"] == "enter":
            if call in calls or details.get("parent") != (stack[-1] if stack else None):
                raise ValueError("duplicate call or incorrect same-thread parent")
            calls[call] = event
            stack.append(call)
        elif event["event"] == "return":
            if (not stack or stack.pop() != call or call in completed or
                    calls[call]["details"]["method"] != method):
                raise ValueError("unbalanced original method return")
            completed[call] = event
        else:
            raise ValueError("exception or unknown event in callback observation")
        if sequence == start - 1 and any(stacks.values()):
            raise ValueError("preparation ends inside a call")
    if any(stacks.values()) or len(completed) != len(calls):
        raise ValueError("missing original method return")
    experimental = events[start:]
    entries = [event for event in experimental if event["event"] == "enter"]
    selected = {}
    for method in (CALLBACK, BACKUP, CANCEL):
        matching = [event for event in entries if event["details"]["method"] == method]
        if len(matching) != 1:
            raise ValueError("one callback, backupNow and cancel required")
        selected[method] = matching[0]
    callback, backup, cancel = (selected[method] for method in (CALLBACK, BACKUP, CANCEL))
    if (callback is not experimental[0] or
            completed[callback["details"]["call"]] is not experimental[-1] or
            callback["details"]["parent"] is not None or
            backup["details"]["parent"] != callback["details"]["call"] or
            cancel["details"]["parent"] != backup["details"]["call"]):
        raise ValueError("callback to backupNow to cancel nesting mismatch")
    if any(event["thread"] != callback["thread"] for event in experimental):
        raise ValueError("experimental callback crosses threads")
    queued = record["queued"]
    if (record["after_prepare"].get("pending") is not False or
            queued.get("pending") is not True or queued.get("scheduled") is not True or
            len(queued["jobs"]) != 1 or record["after_delivery"].get("pending") is not False):
        raise ValueError("target pending transitions incomplete")
    job = queued["jobs"][0]
    if type(job.get("id")) is not int or job.get("userId") != 0:
        raise ValueError("queued job identity mismatch")
    callback_before = callback["details"]["value"]
    callback_after = completed[callback["details"]["call"]]["details"]["value"]
    if any(value.get("id") != job["id"] or value.get("userId") != 0
           for value in (callback_before, callback_after)):
        raise ValueError("callback job differs from queued job")
    if (callback_before.get("scheduled") is not True or
            callback_after.get("scheduled") is not False or
            callback_after.get("result") is not False or
            backup["details"]["value"].get("scheduled") is not False or
            backup["details"]["value"].get("pending") is not True):
        raise ValueError("callback state transition or return mismatch")
    for event in entries:
        if event["details"]["method"] in (CANCEL, CLEAR) and event["details"]["value"].get("userId") != 0:
            raise ValueError("unexpected backup user")
    commands = record["commands"]
    if not commands or any(command.get("exit_code") != 0 for command in commands):
        raise ValueError("failed or missing commands")
    forced = [command["arguments"] for command in commands
              if command["arguments"][:3] == ["shell", "cmd", "jobscheduler"]]
    requested = [command["arguments"] for command in commands
                 if command["arguments"][:3] == ["shell", "am", "broadcast"]]
    if forced != [["shell", "cmd", "jobscheduler", "run", "-f", "--user", "0", "android", str(job["id"])]]:
        raise ValueError("exactly one observed job must be forced")
    if requested != [["shell", "am", "broadcast", "--include-stopped-packages",
                     "-n", PACKAGE + "/.ProbeReceiver", "-a", PACKAGE + ".REQUEST"]]:
        raise ValueError("exactly one owned fixture request required")
    arguments = [command["arguments"] for command in commands]
    if arguments.index(requested[0]) >= arguments.index(forced[0]):
        raise ValueError("request must precede forced delivery")
    return {"schema": "nashura.backup-delivery-audit.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "trigger": TRIGGER, "source": "owned fixture, not a game store transaction",
            "events": len(events), "experimental_events": len(experimental),
            "job_id": job["id"], "callback_result": False,
            "callback_result_meaning": "JobService returned; asynchronous backup completion not implied",
            "clear_hook_entries": sum(event["details"]["method"] == CLEAR for event in entries),
            "clear_direct_call_coverage": "UNKNOWN", "worker_coverage": WORKER_COVERAGE,
            "target_pending_after": False, "transport_payload_parity": "UNKNOWN",
            "journal_deletion": "UNKNOWN", "scheduler_failure": "UNKNOWN",
            "linux_backup_binding": "UNRESOLVED", "catalog_ready_pass": False}


def audit(path, expected_sha, output):
    source = path.read_bytes()
    if digest(source) != expected_sha:
        raise ValueError("run SHA mismatch")
    record = json.loads(source)
    result = review(record)
    for index, command in enumerate(record["commands"]):
        for extension in ("stdout", "stderr"):
            data = (path.parent / ("command-%02d.%s" % (index, extension))).read_bytes()
            if digest(data) != command[extension + "_sha256"]:
                raise ValueError("command archive SHA mismatch")
        if command["arguments"][:3] == ["shell", "am", "broadcast"]:
            data = (path.parent / ("command-%02d.stdout" % index)).read_bytes()
            if b'data="REQUEST_RETURNED"' not in data:
                raise ValueError("request return evidence missing")
    if digest((path.parent / "observer.js").read_bytes()) != record["script_sha256"]:
        raise ValueError("observer archive SHA mismatch")
    if path.read_bytes() != source:
        raise ValueError("run changed during audit")
    result["input_sha256"] = expected_sha
    result["command_archives_verified"] = len(record["commands"])
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
