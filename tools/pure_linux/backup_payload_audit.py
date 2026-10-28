"""Audit private nonempty Backup evidence; keep Linux binding and coverage unknown."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ElementTree

from run_backup_payload import AVD, PACKAGE


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(path, expected_sha, prefix):
    source = path.read_bytes()
    if digest(source) != expected_sha:
        raise ValueError("input SHA mismatch")
    record = json.loads(source)
    commands = record if isinstance(record, list) else record["commands"]
    outputs = []
    for index, command in enumerate(commands):
        if type(command.get("exit_code")) is not int or command["exit_code"] != 0:
            raise ValueError("failed command in evidence")
        for extension in ("stdout", "stderr"):
            data = (path.parent / (prefix % index + "." + extension)).read_bytes()
            if digest(data) != command[extension + "_sha256"]:
                raise ValueError("command archive SHA mismatch")
            if extension == "stdout":
                outputs.append(data)
    if path.read_bytes() != source:
        raise ValueError("evidence changed during audit")
    return record, commands, outputs


def one(commands, outputs, arguments):
    matches = [output for command, output in zip(commands, outputs)
               if command["arguments"] == arguments]
    if len(matches) != 1:
        raise ValueError("one exact command required: " + " ".join(arguments))
    return matches[0]


def sequence(commands, expected):
    if [entry["arguments"] for entry in commands] != expected:
        raise ValueError("command order or experiment boundary mismatch")


def indexed(record, key, commands, arguments):
    index = record[key]
    if type(index) is not int or not 0 <= index < len(commands) or commands[index]["arguments"] != arguments:
        raise ValueError("evidence command index mismatch: " + key)


def installed_path(data):
    path = data.decode().strip()
    if not re.fullmatch(r"package:/data/app/[A-Za-z0-9_+~/=.-]+/base\.apk", path) or "/../" in path:
        raise ValueError("invalid installed APK path")
    return path[len("package:"):]


def pending(data):
    text = data.decode()
    match = re.search(r"^Pending key/value backup: (\d+)\n(.*?)^Full backup queue:",
                      text, re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError("pending section missing")
    entries = re.findall(r"^    BackupRequest\{pkg=([^{}\s]+)\}$", match[2], re.MULTILINE)
    if len(entries) != int(match[1]) or len(set(entries)) != len(entries):
        raise ValueError("incomplete or duplicate pending entries")
    return PACKAGE in entries


def journal(data):
    offset, entries = 0, []
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("truncated journal length")
        length = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        if offset + length > len(data):
            raise ValueError("truncated journal payload")
        entry = data[offset:offset + length].decode("ascii")
        if not re.fullmatch(r"(?:[A-Za-z0-9_]+\.)*[A-Za-z0-9_]+", entry):
            raise ValueError("unsupported journal package encoding")
        entries.append(entry)
        offset += length
    return entries


def journal_paths(data):
    paths = data.decode().splitlines()
    if (len(paths) != len(set(paths)) or
            any(not re.fullmatch(r"/data/backup/pending/journal\d+\.tmp", path) for path in paths)):
        raise ValueError("invalid journal paths")
    return set(paths)


def token_events(data, token):
    pattern = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(\d+)\s+(\d+) "
                         r"I NashuraBackupPayload: (STORE_RETURN|REQUEST_RETURN|AGENT_ENTER|AGENT_RETURN) "
                         r"token=([0-9a-f]{32}) pid=(\d+) tid=(\d+)(?: new_state_bytes=(\d+))?$",
                         re.MULTILINE)
    lines = [line for line in data.decode().splitlines() if "NashuraBackupPayload:" in line]
    events = []
    for line in lines:
        match = pattern.fullmatch(line)
        if (not match or match[5] != token or match[2] != match[6] or match[3] != match[7]):
            raise ValueError("unattributed payload log event")
        events.append({"time": match[1], "pid": int(match[2]), "tid": int(match[3]),
                       "event": match[4], "state_bytes": int(match[8]) if match[8] else None})
    if events != sorted(events, key=lambda event: event["time"]):
        raise ValueError("payload log order mismatch")
    return events


def worker_window(data, start, end, system_pid, expected_bytes):
    lines = []
    pattern = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(\d+)\s+(\d+) [A-Z] (.*)$")
    for line in data.decode().splitlines():
        match = pattern.fullmatch(line)
        if match and start <= match[1] <= end:
            lines.append((match[1], int(match[2]), int(match[3]), match[4]))
    package_event = "backup_package: [" + PACKAGE + "," + str(expected_bytes) + "]"
    packages = [line for line in lines if line[3] == package_event and line[1] == system_pid]
    if len(packages) != 1:
        raise ValueError("target worker byte count missing or ambiguous")
    event = packages[0]
    invokes = [line for line in lines if line[1:3] == event[1:3] and
               line[3] == "KeyValueBackupTask: Invoking agent on " + PACKAGE and line[0] <= event[0]]
    finishes = [line for line in lines if line[1:3] == event[1:3] and
                line[3] == "KeyValueBackupTask: K/V backup pass finished" and line[0] >= event[0]]
    if len(invokes) != 1 or len(finishes) != 1:
        raise ValueError("target worker invoke/finish missing or ambiguous")
    return {"pid": event[1], "tid": event[2], "stream_bytes": expected_bytes,
            "coverage": "LOG_ONLY_NOT_METHOD_COUNTS"}


def review(run, commands, outputs, drain_commands, drain_outputs, follow, follow_commands, follow_outputs):
    if (run.get("schema") != "nashura.backup-payload-run.v1" or
            run.get("status") != "RUNTIME_OBSERVED_REQUIRES_REVIEW" or run.get("avd") != AVD or
            run.get("trigger") != "owned_payload_then_bmgr_backupnow_nonincremental" or
            run.get("catalog_ready_pass") is not False):
        raise ValueError("isolated payload evidence required")
    for name, expected in (("requests_issued", 1), ("stores_issued", 1), ("backupnow_issued", 1),
                           ("game_stores_issued", 0), ("system_server_hooks", 0)):
        if type(run.get(name)) is not int or run[name] != expected:
            raise ValueError("one-shot experiment boundary mismatch")
    token = run["token"]
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid experiment token")
    identity = run["identity_before"]
    if (identity != run["identity_after"] or identity.get("cmdline") != "system_server" or
            type(identity.get("pid")) is not int or identity["pid"] <= 0 or
            type(identity.get("starttime")) is not int or identity["starttime"] <= 0 or
            not identity.get("boot_id")):
        raise ValueError("system_server identity missing or changed")
    for selected_commands, selected_outputs in ((commands, outputs), (drain_commands, drain_outputs),
                                               (follow_commands, follow_outputs)):
        if one(selected_commands, selected_outputs, ["emu", "avd", "name"]).decode().splitlines()[0] != AVD:
            raise ValueError("exclusive fixture AVD required")
        if one(selected_commands, selected_outputs,
               ["shell", "pm", "list", "packages", "nullsroyale.rel.free"]).strip():
            raise ValueError("game present on fixture AVD")
    requested = [entry["arguments"] for entry in commands
                 if entry["arguments"][:3] == ["shell", "am", "broadcast"]]
    expected_request = ["shell", "am", "broadcast", "--include-stopped-packages", "-n",
                        PACKAGE + "/.PayloadReceiver", "-a", PACKAGE + ".STORE_AND_REQUEST",
                        "--es", "token", token]
    apk_path = installed_path(one(commands, outputs, ["shell", "pm", "path", PACKAGE]))
    installation = [entry["arguments"] for entry in commands if entry["arguments"][:1] == ["install"]]
    if len(installation) != 1 or len(installation[0]) != 2:
        raise ValueError("one fixture installation required")
    dump_command = ["shell", "dumpsys", "backup"]
    sequence(commands, [
        ["emu", "avd", "name"], ["shell", "pm", "list", "packages", "nullsroyale.rel.free"],
        ["shell", "pm", "list", "packages", PACKAGE], ["shell", "getprop", "ro.build.fingerprint"],
        ["exec-out", "cat", "/system/framework/services.jar"], ["shell", "pidof", "system_server"],
        installation[0], ["shell", "pm", "path", PACKAGE], ["exec-out", "cat", apk_path],
        ["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"],
        ["shell", "bmgr", "enable", "true"], ["shell", "bmgr", "run"], dump_command,
        ["logcat", "-d", "-b", "all", "-t", "2000"], expected_request, dump_command,
        ["shell", "bmgr", "backupnow", "--non-incremental", PACKAGE], dump_command,
        ["logcat", "-d", "-b", "all", "-t", "5000"],
        ["shell", "find", "/data/backup", "/data/user_de/0/com.android.localtransport", "-type", "f"],
        ["exec-out", "cat", apk_path], ["exec-out", "cat", "/system/framework/services.jar"]])
    expected_indices = {"before_dump_index": 12, "prelude_log_index": 13,
                        "queued_dump_index": 15, "backupnow_index": 16, "after_dump_index": 17,
                        "final_log_index": 18, "backup_files_index": 19}
    if any(type(run[key]) is not int or run[key] != value for key, value in expected_indices.items()):
        raise ValueError("run command indices differ")
    if outputs[2].strip() or b"Success" not in outputs[6]:
        raise ValueError("fresh fixture installation evidence missing")
    if int(outputs[5].strip()) != identity["pid"]:
        raise ValueError("run system_server PID differs")
    if outputs[3].decode().strip() != run.get("fingerprint") or not run.get("fingerprint"):
        raise ValueError("run fingerprint differs")
    for index, expected_sha in ((4, run["platform_sha256"]), (21, run["platform_sha256"]),
                                (8, run["apk_sha256"]), (20, run["apk_sha256"])):
        if digest(outputs[index]) != expected_sha:
            raise ValueError("run platform or APK identity changed")
    if requested != [expected_request]:
        raise ValueError("exactly one payload store/request required")
    if ("PAYLOAD_REQUEST_RETURNED:" + token).encode() not in one(commands, outputs, expected_request):
        raise ValueError("payload store/request confirmation missing")
    direct = ["shell", "bmgr", "backupnow", "--non-incremental", PACKAGE]
    if [entry["arguments"] for entry in commands if entry["arguments"][:3] == direct[:3]] != [direct]:
        raise ValueError("one direct nonincremental backup required")
    if b"Backup finished with result: Success" not in one(commands, outputs, direct):
        raise ValueError("direct backup did not complete successfully")
    if not pending(outputs[run["queued_dump_index"]]) or not pending(outputs[run["after_dump_index"]]):
        raise ValueError("direct backup must not be mistaken for queue drain")
    if token_events(outputs[run["prelude_log_index"]], token):
        raise ValueError("token events in preparation window")
    events = token_events(outputs[run["final_log_index"]], token)
    kinds = ["STORE_RETURN", "REQUEST_RETURN", "AGENT_ENTER", "AGENT_RETURN"]
    if [event["event"] for event in events] != kinds or len({event["pid"] for event in events}) != 1:
        raise ValueError("one original agent return required after store/request")
    if events[0]["tid"] != events[1]["tid"] or events[2]["tid"] != events[3]["tid"]:
        raise ValueError("source/agent thread identity mismatch")
    drain_args = [entry["arguments"] for entry in drain_commands]
    if drain_args.count(["shell", "bmgr", "run"]) != 1 or any(
            args[:3] == ["shell", "am", "broadcast"] or args[:3] == direct[:3] for args in drain_args):
        raise ValueError("queue drain must not issue another store or backupnow")
    dumps = [data for entry, data in zip(drain_commands, drain_outputs)
             if entry["arguments"] == ["shell", "dumpsys", "backup"]]
    if len(dumps) != 2 or not pending(dumps[0]) or pending(dumps[1]):
        raise ValueError("queue drain pending transition missing")
    drain_log = one(drain_commands, drain_outputs, ["logcat", "-d", "-b", "all", "-t", "3000"])
    drained_events = token_events(drain_log, token)
    if (drained_events[:4] != events or [event["event"] for event in drained_events] !=
            kinds + ["AGENT_ENTER", "AGENT_RETURN"] or
            drained_events[4]["pid"] != events[0]["pid"] or
            drained_events[4]["pid"] != drained_events[5]["pid"] or
            drained_events[4]["tid"] != drained_events[5]["tid"]):
        raise ValueError("queue drain agent evidence missing or duplicated")
    direct_worker = worker_window(outputs[run["final_log_index"]], events[1]["time"],
                                  "99-99 99:99:99.999", identity["pid"], 261)
    drain_worker = worker_window(drain_log, drained_events[3]["time"],
                                 "99-99 99:99:99.999", identity["pid"], 0)
    transport_log = ("LocalTransport: Performing non-incremental backup for " + PACKAGE).encode()
    if outputs[run["final_log_index"]].count(transport_log) != 1:
        raise ValueError("nonempty transport log missing or ambiguous")
    file_paths = {"transport": "/data/user/0/com.android.localtransport/files/1/_delta/" + PACKAGE +
                  "/Zml4dHVyZTpmaXh0dXJl", "xml": "/data/user/0/" + PACKAGE + "/shared_prefs/fixture.xml",
                  "state": "/data/backup/com.android.localtransport.LocalTransport/" + PACKAGE}
    files, content = {}, {}
    for name, path in file_paths.items():
        copies = [data for entry, data in zip(drain_commands, drain_outputs)
                  if entry["arguments"] == ["exec-out", "cat", path]]
        if len(copies) != 2 or copies[0] != copies[1] or not copies[0]:
            raise ValueError("payload file missing or changed across queue drain")
        content[name] = copies[0]
        files[name] = {"sha256": digest(copies[0]), "bytes": len(copies[0]), "unchanged": True}
    tree = ElementTree.fromstring(content["xml"])
    if (tree.tag != "map" or len(tree) != 2 or any(child.tag != "string" for child in tree) or
            {child.attrib.get("name"): child.text for child in tree} !=
            {"token": token, "payload": "payload-v1:" + token}):
        raise ValueError("stored XML differs from actual payload")
    if (content["transport"][16:] != content["xml"] or
            any(event["state_bytes"] != len(content["state"]) for event in (events[3], drained_events[5]))):
        raise ValueError("transport XML or agent state length mismatch")
    listings = [data for entry, data in zip(drain_commands, drain_outputs)
                if entry["arguments"] == ["shell", "find", "/data/backup/pending", "-type", "f"]]
    if len(listings) != 2:
        raise ValueError("journal before/after listing required")
    before, after = map(journal_paths, listings)
    read_files = [["exec-out", "cat", path] for path in file_paths.values()]
    listing_command = ["shell", "find", "/data/backup/pending", "-type", "f"]
    sequence(drain_commands, [
        ["emu", "avd", "name"], ["shell", "pm", "list", "packages", "nullsroyale.rel.free"],
        *read_files, listing_command,
        *[["exec-out", "cat", path] for path in listings[0].decode().splitlines()],
        dump_command, ["shell", "bmgr", "run"], dump_command,
        ["logcat", "-d", "-b", "all", "-t", "3000"], listing_command, *read_files])
    target_paths = set()
    for path in before:
        data = one(drain_commands, drain_outputs, ["exec-out", "cat", path])
        count = journal(data).count(PACKAGE)
        if count > 1:
            raise ValueError("duplicate target journal entry")
        if count:
            target_paths.add(path)
    if len(target_paths) != 1 or target_paths & after:
        raise ValueError("target journal did not disappear from listing")
    if (follow.get("schema") != "nashura.backup-payload-journal-followup.v1" or
            follow.get("status") != "RUNTIME_OBSERVED_REQUIRES_REVIEW" or
            follow.get("read_only") is not True or follow.get("catalog_ready_pass") is not False):
        raise ValueError("read-only journal followup required")
    indexed(follow, "listing_before_index", follow_commands, listing_command)
    indexed(follow, "listing_after_index", follow_commands, listing_command)
    indexed(follow, "dump_index", follow_commands, dump_command)
    indexed(follow, "platform_index", follow_commands, ["exec-out", "cat", "/system/framework/services.jar"])
    indexed(follow, "installed_apk_index", follow_commands, ["exec-out", "cat", apk_path])
    sequence(follow_commands, [
        ["emu", "avd", "name"], ["shell", "pm", "list", "packages", "nullsroyale.rel.free"],
        ["shell", "getprop", "ro.build.fingerprint"], ["shell", "pidof", "system_server"],
        ["shell", "cat", "/proc/%d/stat" % identity["pid"]],
        ["shell", "cat", "/proc/sys/kernel/random/boot_id"], listing_command,
        *[["exec-out", "cat", path] for path in follow_outputs[follow["listing_before_index"]].decode().splitlines()],
        dump_command, listing_command, ["shell", "pm", "path", PACKAGE],
        ["exec-out", "cat", "/system/framework/services.jar"], ["exec-out", "cat", apk_path]])
    if (one(follow_commands, follow_outputs, ["shell", "getprop", "ro.build.fingerprint"]).decode().strip()
            != run["fingerprint"] or installed_path(one(follow_commands, follow_outputs,
                ["shell", "pm", "path", PACKAGE])) != apk_path):
        raise ValueError("followup fingerprint or installed path differs")
    if (int(one(follow_commands, follow_outputs, ["shell", "pidof", "system_server"]).strip()) != identity["pid"] or
            one(follow_commands, follow_outputs, ["shell", "cat", "/proc/sys/kernel/random/boot_id"]).decode().strip() != identity["boot_id"]):
        raise ValueError("followup system_server identity differs")
    stat = one(follow_commands, follow_outputs, ["shell", "cat", "/proc/%d/stat" % identity["pid"]]).decode()
    if int(stat.rsplit(") ", 1)[1].split()[19]) != identity["starttime"]:
        raise ValueError("followup system_server starttime differs")
    if (pending(follow_outputs[follow["dump_index"]]) or
            journal_paths(follow_outputs[follow["listing_before_index"]]) != after or
            journal_paths(follow_outputs[follow["listing_after_index"]]) != after):
        raise ValueError("journal followup window changed")
    for path in after:
        data = one(follow_commands, follow_outputs, ["exec-out", "cat", path])
        if PACKAGE in journal(data):
            raise ValueError("target requeued in surviving journal")
        if path in before and data != one(drain_commands, drain_outputs, ["exec-out", "cat", path]):
            raise ValueError("retained journal changed")
    for index_key, expected in (("platform_index", run["platform_sha256"]),
                                ("installed_apk_index", run["apk_sha256"])):
        if digest(follow_outputs[follow[index_key]]) != expected:
            raise ValueError("followup platform or APK identity changed")
    return {"schema": "nashura.backup-payload-audit.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "source": "owned Android fixture, not game or Linux runtime",
            "token_sha256": digest(token.encode()), "direct_worker": direct_worker,
            "drain_worker": drain_worker, "token_events": len(drained_events), "files": files,
            "direct_backup_left_pending": True, "after_queue_drain_pending": False,
            "target_journal_disappeared": True, "surviving_journals": len(after),
            "surviving_target_entries": 0, "transport_xml_offset_observed": 16,
            "transport_header_semantics": "UNKNOWN", "journal_delete_method_count": "UNKNOWN",
            "natural_job_expiry": "NOT_RUN", "failure_paths": "NOT_RUN",
            "linux_backup_binding": "UNRESOLVED", "catalog_ready_pass": False}


def audit(run_path, run_sha, drain_path, drain_sha, follow_path, follow_sha, output):
    run, commands, outputs = archive(run_path, run_sha, "command-%02d")
    _, drain_commands, drain_outputs = archive(drain_path, drain_sha, "%02d")
    follow, follow_commands, follow_outputs = archive(follow_path, follow_sha, "%02d")
    result = review(run, commands, outputs, drain_commands, drain_outputs,
                    follow, follow_commands, follow_outputs)
    result["input_sha256"] = {"run": run_sha, "drain_commands": drain_sha, "followup": follow_sha}
    result["command_archives_verified"] = sum(map(len, (commands, drain_commands, follow_commands)))
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "drain", "followup", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("run-sha256", "drain-sha256", "followup-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.run, args.run_sha256, args.drain, args.drain_sha256,
                           args.followup, args.followup_sha256, args.output)))


if __name__ == "__main__":
    main()
