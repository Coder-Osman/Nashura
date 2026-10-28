"""Review owned retry evidence without promoting it to a Linux engine gate."""

import argparse
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ElementTree

from backup_payload_audit import digest, installed_path, journal, journal_paths
from run_backup_retry import AVD, PACKAGE


def archive(path, expected_sha):
    source = path.read_bytes()
    if digest(source) != expected_sha:
        raise ValueError("input SHA mismatch")
    record = json.loads(source)
    commands = record if isinstance(record, list) else record["commands"]
    outputs = []
    for index, entry in enumerate(commands):
        if type(entry.get("exit_code")) is not int or entry["exit_code"] not in (0, 1):
            raise ValueError("failed or timed-out command")
        pair = []
        for extension in ("stdout", "stderr"):
            data = (path.parent / ("command-%03d.%s" % (index, extension))).read_bytes()
            if digest(data) != entry[extension + "_sha256"]:
                raise ValueError("archive SHA mismatch")
            pair.append(data)
        outputs.append(tuple(pair))
    if path.read_bytes() != source:
        raise ValueError("input changed during review")
    return record, commands, outputs


def pending(data):
    text = data.decode()
    match = re.search(r"^Pending key/value backup: (\d+)\n(.*?)^Full backup queue:",
                      text, re.MULTILINE | re.DOTALL)
    if not match or "\nNo backups running\n" not in text:
        raise ValueError("quiescent pending section missing")
    entries = re.findall(r"^    BackupRequest\{pkg=([^{}\s]+)\}$", match[2], re.MULTILINE)
    if len(entries) != int(match[1]) or len(entries) != len(set(entries)):
        raise ValueError("invalid pending entries")
    return PACKAGE in entries


def events(data, token):
    pattern = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(\d+)\s+(\d+) "
                         r"I NashuraBackupRetry: (STORE_RETURN|REQUEST_RETURN|AGENT_ENTER|AGENT_FAIL|AGENT_RETURN) "
                         r"token=([0-9a-f]{32})(?: new_state_bytes=(\d+))?$", re.MULTILINE)
    result = []
    for line in data.decode().splitlines():
        if "NashuraBackupRetry:" not in line:
            continue
        match = pattern.fullmatch(line)
        if not match or match[5] != token:
            raise ValueError("unattributed retry event")
        result.append({"time": match[1], "pid": int(match[2]), "tid": int(match[3]),
                       "event": match[4], "state_bytes": int(match[6]) if match[6] else None})
    if result != sorted(result, key=lambda entry: entry["time"]):
        raise ValueError("event order mismatch")
    return result


def workers(log, system_pid):
    parsed = []
    for line in log.decode().splitlines():
        match = re.fullmatch(r"(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(\d+)\s+(\d+) [A-Z] (.*)", line)
        if match and int(match[2]) == system_pid:
            parsed.append((match[1], int(match[3]), match[4]))
    failure = [entry for entry in parsed if entry[2] == "backup_agent_failure: [" + PACKAGE + ",result error]"]
    success = [entry for entry in parsed if re.fullmatch(r"backup_package: \[" + re.escape(PACKAGE) + r",[1-9][0-9]*\]", entry[2])]
    if len(failure) != 1 or len(success) != 1 or failure[0][0] >= success[0][0]:
        raise ValueError("one real agent failure and one success required")
    windows = []
    for event in (failure[0], success[0]):
        invokes = [entry for entry in parsed if entry[1] == event[1] and entry[0] <= event[0]
                   and entry[2] == "KeyValueBackupTask: Invoking agent on " + PACKAGE]
        finishes = [entry for entry in parsed if entry[1] == event[1] and entry[0] >= event[0]
                    and entry[2] == "KeyValueBackupTask: K/V backup pass finished"]
        if len(invokes) != 1 or len(finishes) != 1:
            raise ValueError("worker invoke/finish boundary missing")
        windows.append({"pid": system_pid, "tid": event[1], "start": invokes[0][0], "end": finishes[0][0]})
    return windows, int(success[0][2].rsplit(",", 1)[1][:-1])


def review(record, commands, outputs, follow_commands, follow_outputs):
    if len(commands) != len(outputs) or len(follow_commands) != len(follow_outputs):
        raise ValueError("command/output coverage mismatch")
    if (record.get("schema") not in {"nashura.backup-retry-run.v1", "nashura.backup-retry-run.v2"}
            or record.get("status") != "RUNTIME_OBSERVED_REQUIRES_REVIEW"
            or record.get("avd") != AVD or record.get("serial") != "emulator-5582"
            or record.get("catalog_ready_pass") is not False):
        raise ValueError("exclusive retry evidence required")
    legacy = record["schema"].endswith(".v1")
    if not legacy and record.get("file_capture_mode") != "shell_T_remote_exit_status":
        raise ValueError("remote file exit status required")
    for key, expected in (("stores_issued", 1), ("requests_issued", 1), ("queue_runs_issued", 3),
                          ("game_stores_issued", 0), ("system_server_hooks", 0), ("backupnow_issued", 0)):
        if type(record.get(key)) is not int or record[key] != expected:
            raise ValueError("one-shot boundary mismatch")
    token = record["token"]
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid token")
    identity = record["identity_before"]
    if (identity != record["identity_after"] or identity.get("cmdline") != "system_server"
            or type(identity.get("pid")) is not int or identity["pid"] <= 0
            or type(identity.get("starttime")) is not int or identity["starttime"] <= 0
            or not identity.get("boot_id")):
        raise ValueError("process identity missing or changed")
    cursor = 0

    def take(arguments, missing=False):
        nonlocal cursor
        index = cursor
        cursor += 1
        if index >= len(commands) or commands[index]["arguments"] != arguments:
            raise ValueError("command order or boundary differs")
        data, error = outputs[index]
        code = commands[index]["exit_code"]
        if missing:
            diagnostic = ("cat: " + arguments[-1] + ": No such file or directory\n").encode()
            if legacy and code == 0 and data == diagnostic and not error:
                return None
            if not legacy and code == 1 and not data and error == diagnostic:
                return None
        if type(code) is not int or code != 0:
            raise ValueError("unexpected command failure")
        if missing and (error or data.startswith(b"cat:")):
            raise ValueError("unclassified file failure")
        return data

    if take(["emu", "avd", "name"]).decode().splitlines()[0] != AVD:
        raise ValueError("wrong AVD")
    if take(["shell", "pm", "list", "packages", "nullsroyale.rel.free"]).strip():
        raise ValueError("game present on fixture")
    if take(["shell", "pm", "list", "packages", PACKAGE]).strip():
        raise ValueError("fresh fixture required")
    if take(["shell", "getprop", "ro.build.fingerprint"]).decode().strip() != record["fingerprint"] or not record["fingerprint"]:
        raise ValueError("fingerprint differs")
    if digest(take(["exec-out", "cat", "/system/framework/services.jar"])) != record["platform_sha256"]:
        raise ValueError("platform SHA differs")
    if take(["shell", "pidof", "system_server"]).strip() != str(identity["pid"]).encode():
        raise ValueError("system PID differs")
    installation = commands[cursor]["arguments"]
    if len(installation) != 2 or installation[0] != "install" or b"Success" not in take(installation):
        raise ValueError("one fixture installation required")
    apk_path = installed_path(take(["shell", "pm", "path", PACKAGE]))
    if digest(take(["exec-out", "cat", apk_path])) != record["apk_sha256"]:
        raise ValueError("APK SHA differs")
    take(["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"])
    take(["shell", "bmgr", "enable", "true"])
    take(["shell", "bmgr", "run"])
    paths = ["/data/user/0/" + PACKAGE + "/shared_prefs/fixture.xml",
             "/data/user/0/" + PACKAGE + "/no_backup/fail-once",
             "/data/user/0/com.android.localtransport/files/1/_delta/" + PACKAGE + "/Zml4dHVyZTpmaXh0dXJl",
             "/data/backup/com.android.localtransport.LocalTransport/" + PACKAGE]
    stages = []
    expected_events = ["STORE_RETURN", "REQUEST_RETURN", "AGENT_ENTER", "AGENT_FAIL", "AGENT_ENTER", "AGENT_RETURN"]
    for stage_index, name in enumerate(("before", "queued", "after_failure", "after_retry")):
        if stage_index == 1:
            response = take(["shell", "am", "broadcast", "--include-stopped-packages", "-n", PACKAGE + "/.RetryReceiver",
                             "-a", PACKAGE + ".STORE_AND_REQUEST", "--es", "token", token])
            if ("RETRY_REQUEST_RETURNED:" + token).encode() not in response:
                raise ValueError("store/request not confirmed")
        elif stage_index > 1:
            take(["shell", "bmgr", "run"])
        indices = {"dump": cursor}
        queued = pending(take(["shell", "dumpsys", "backup"]))
        indices["journal_listing"] = cursor
        journals = {}
        listed = journal_paths(take(["shell", "find", "/data/backup/pending", "-type", "f"]))
        indices["journals"] = []
        for path in sorted(listed):
            indices["journals"].append(cursor)
            journals[path] = take(["exec-out", "cat", path])
        target = {path for path, data in journals.items() if PACKAGE in journal(data)}
        indices["files"] = []
        files = []
        for path in paths:
            indices["files"].append(cursor)
            files.append(take((["exec-out", "cat"] if legacy else ["shell", "-T", "cat"]) + [path], missing=True))
        indices["log"] = cursor
        log = take(["logcat", "-d", "-v", "threadtime", "-b", "all", "-t", "6000"])
        observed = events(log, token)
        if ([entry["event"] for entry in observed] != expected_events[:stage_index * 2]
                or record["snapshots"].get(name) != indices):
            raise ValueError("event coverage or snapshot index differs")
        stages.append({"pending": queued, "journals": journals, "target": target,
                       "files": files, "events": observed, "log": log})
    for path, expected_sha in ((apk_path, record["apk_sha256"]), ("/system/framework/services.jar", record["platform_sha256"])):
        if digest(take(["exec-out", "cat", path])) != expected_sha:
            raise ValueError("input changed")
    if cursor != len(commands) or set(record["snapshots"]) != {"before", "queued", "after_failure", "after_retry"}:
        raise ValueError("extra command or snapshot")
    before, queued, failed, retried = stages
    if [stage["pending"] for stage in stages] != [False, True, True, False]:
        raise ValueError("failure requeue and success consumption not observed")
    if [len(stage["target"]) for stage in stages] != [0, 1, 1, 0]:
        raise ValueError("journal target transition differs")
    if queued["target"] & set(failed["journals"]) or failed["target"] & set(retried["journals"]):
        raise ValueError("consumed journal still present")
    if set(before["journals"]) != set(queued["journals"]):
        raise ValueError("queue request changed journal ownership unexpectedly")
    for stage_index, (earlier, later) in enumerate(zip(stages, stages[1:])):
        retained = set(earlier["journals"]) & set(later["journals"])
        if stage_index == 0:
            path = next(iter(queued["target"]))
            previous, current = earlier["journals"][path], later["journals"][path]
            if not current.startswith(previous) or journal(current) != journal(previous) + [PACKAGE]:
                raise ValueError("request journal is not an exact target append")
            retained.remove(path)
        if any(earlier["journals"][path] != later["journals"][path]
               for path in retained):
            raise ValueError("retained journal changed unexpectedly")
    if before["files"] != [None] * 4 or queued["files"][1] != token.encode():
        raise ValueError("fresh files or failure marker differ")
    xml = queued["files"][0]
    try:
        root = ElementTree.fromstring(xml)
    except (ElementTree.ParseError, TypeError) as error:
        raise ValueError("invalid payload XML") from error
    if (root.tag != "map" or len(root) != 2
            or {child.attrib.get("name"): child.text for child in root} != {"token": token, "payload": "retry-v1:" + token}
            or any(child.tag != "string" for child in root)):
        raise ValueError("payload identity differs")
    if (queued["files"][2:] != [None, None] or failed["files"] != [xml, None, None, b""]
            or retried["files"][0] != xml or retried["files"][1] is not None):
        raise ValueError("failure modified payload or published transport")
    transport, state = retried["files"][2:]
    if transport is None or transport[16:] != xml or not state:
        raise ValueError("successful transport or state missing")
    windows, stream_bytes = workers(retried["log"], identity["pid"])
    final_events = retried["events"]
    if any(stage["events"] != final_events[:len(stage["events"])] for stage in stages):
        raise ValueError("cumulative log continuity differs")
    for indices, window in zip(((2, 3), (4, 5)), windows):
        enter, leave = [final_events[index] for index in indices]
        if (enter["pid"] != leave["pid"] or enter["tid"] != leave["tid"]
                or not window["start"] <= enter["time"] <= leave["time"] <= window["end"]):
            raise ValueError("agent/worker window differs")
    if final_events[-1]["state_bytes"] != len(state):
        raise ValueError("agent state size differs")
    if b"NASHURA_CONTROLLED_AGENT_FAILURE" not in failed["log"]:
        raise ValueError("real controlled exception diagnostic missing")
    expected_follow = [["emu", "avd", "name"], ["shell", "pm", "list", "packages", "nullsroyale.rel.free"],
                       ["shell", "pidof", "system_server"], *[["shell", "-T", "cat", path] for path in paths],
                       ["shell", "dumpsys", "backup"]]
    if [entry["arguments"] for entry in follow_commands] != expected_follow:
        raise ValueError("followup boundary differs")
    if ([entry["exit_code"] for entry in follow_commands] != [0, 0, 0, 0, 1, 0, 0, 0]
            or follow_outputs[0][0].decode().splitlines()[0] != AVD or follow_outputs[1][0].strip()
            or follow_outputs[2][0].strip() != str(identity["pid"]).encode()
            or any(follow_outputs[index][0] != data for index, data in ((3, xml), (5, transport), (6, state)))
            or follow_outputs[4] != (b"", ("cat: " + paths[1] + ": No such file or directory\n").encode())
            or pending(follow_outputs[7][0])):
        raise ValueError("remote-status followup differs")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "pending": [stage["pending"] for stage in stages],
            "journal_target_counts": [len(stage["target"]) for stage in stages],
            "agent_events": final_events, "worker_windows": windows, "stream_bytes": stream_bytes,
            "xml_bytes": len(xml), "transport_bytes": len(transport), "state_bytes": len(state),
            "file_capture_mode": "legacy_exec_out_diagnostics_with_shell_T_followup" if legacy else "shell_T_remote_exit_status",
            "transport_header_semantics": "UNKNOWN", "journal_delete_method_count": "UNKNOWN",
            "failure_scope": "owned_agent_IOException_only", "natural_job_expiry": "NOT_RUN",
            "task_exception_and_crash_recovery": "NOT_RUN", "linux_backup_binding": "UNRESOLVED",
            "catalog_ready_pass": False}


def audit(run_path, run_sha, follow_path, follow_sha, output):
    record, commands, outputs = archive(run_path, run_sha)
    _, follow_commands, follow_outputs = archive(follow_path, follow_sha)
    report = {"schema": "nashura.backup-retry-audit.v1", **review(record, commands, outputs, follow_commands, follow_outputs),
              "run_sha256": run_sha, "followup_sha256": follow_sha,
              "command_archives_verified": len(commands) + len(follow_commands),
              "apk_sha256": record["apk_sha256"], "platform_sha256": record["platform_sha256"],
              "fingerprint": record["fingerprint"]}
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "followup", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("run-sha256", "followup-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = audit(args.run, args.run_sha256, args.followup, args.followup_sha256, args.output)
    print(json.dumps({"status": result["status"], "catalog_ready_pass": False}))


if __name__ == "__main__":
    main()
