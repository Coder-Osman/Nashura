"""Verify a recorded loading transaction without repeating device writes."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from preference_backend_job import entries


APP = "com.supercell.titan.ApplicationUtilBase"
PREF = "android.app.SharedPreferencesImpl"
EDITOR = PREF + "$EditorImpl"
KEY = "LOADING_SCREEN_STATE_LABEL"
SLOTS = ("storage.xml", "storage_new.xml")
STAGES = ("before_attach", "before_target_store", "after_target_store", "after_restore_store")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def calls_from(rows):
    stacks, calls, installed, counts = {}, [], set(), Counter()
    forbidden = {"observer_error", "hook_unavailable", "method_unavailable", "call_throw",
                 "disk_commit_untracked", "unattributed_disk_write"}
    for sequence, row in enumerate(rows):
        message = row.get("message", {})
        require(message.get("type") != "error", "Frida script error")
        payload = message.get("payload", {})
        event, details = payload.get("event"), payload.get("details", {})
        require(event not in forbidden, "observer error or incomplete coverage")
        if event == "hook_installed":
            installed.add(details["className"] + "." + details["methodName"])
        if event not in ("call_enter", "call_return"):
            continue
        require(row.get("target_pid") == payload.get("pid"), "event PID mismatch")
        identity = (payload["pid"], payload["threadId"])
        stack = stacks.setdefault(identity, [])
        if event == "call_enter":
            call = {"method": details["method"].split("(", 1)[0],
                    "signature": details["method"], "args": details["args"],
                    "scope": details.get("scope"), "begin": sequence,
                    "pid": identity[0], "thread": identity[1]}
            calls.append(call)
            stack.append(call)
            counts[call["method"]] += 1
        else:
            require(stack and stack[-1]["signature"] == details["method"], "unbalanced call scope")
            call = stack.pop()
            require(call["scope"] == details.get("scope"), "return scope differs")
            call.update(end=sequence, result=details.get("result"))
    require(all(not stack for stack in stacks.values()), "unfinished calls")
    return calls, installed, counts


def within(calls, root, method):
    return [call for call in calls if call["method"] == method and call["pid"] == root["pid"]
            and call["thread"] == root["thread"]
            and root["begin"] < call["begin"] < call["end"] < root["end"]]


def audit(path, expected_sha256):
    raw = path.read_bytes()
    require(digest(raw) == expected_sha256, "trace identity mismatch")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    calls, installed, counts = calls_from(rows)
    roots = [call for call in calls if call["method"] == APP + ".storeKeyValue"]
    reads = [call for call in calls if call["method"] == APP + ".getKeyValue"]
    require(len(roots) == 2 and len(reads) == 3, "expected one target store and one restore")
    require(all(call["args"] == [KEY] for call in reads), "unexpected read key")
    require(all(call["args"][0] == KEY and len(call["args"]) == 2 for call in roots),
            "unexpected store key")
    original, target = reads[0]["result"], roots[0]["args"][1]
    require(isinstance(original, str) and isinstance(target, str) and target != original,
            "invalid target or original value")
    require(roots[1]["args"][1] == original and reads[1]["result"] == target
            and reads[2]["result"] == original, "readback or restore differs")
    ordered = [reads[0], roots[0], reads[1], roots[1], reads[2]]
    require(all(left["end"] < right["begin"] for left, right in zip(ordered, ordered[1:])),
            "transaction order differs")
    require(len({call["pid"] for call in ordered}) == 1, "transaction spans app processes")
    identities = [row for row in rows if row.get("phase") == "identity"]
    require(len(identities) == 1 and identities[0]["app"]["processName"] == "nullsroyale.rel.free",
            "application identity missing")
    for phase, expected in (("read_initial", original), ("read_after_target_store", target),
                            ("read_after_restore", original)):
        records = [row for row in rows if row.get("phase") == phase]
        require(len(records) == 1 and records[0]["result"] == {"key": KEY, "value": expected},
                "runner readback missing or inconsistent")
    require(not any(row.get("phase") in ("experiment_error", "target_store_error", "restore_failed")
                    or "detach_error" in row for row in rows), "runner failure recorded")

    snapshots, file_evidence, snapshot_ranges = {}, [], {}
    for stage in STAGES:
        positions = [index for index, row in enumerate(rows)
                     if row.get("phase") == stage and "preference_file" in row]
        require(len(positions) == 4, "incomplete file and backup snapshots")
        snapshot_ranges[stage] = min(positions), max(positions)
        records = [row["preference_file"] for row in rows
                   if row.get("phase") == stage and "preference_file" in row]
        by_name = {Path(record["remote_path"]).name: record for record in records}
        require(set(by_name) == set(SLOTS) | {slot + ".bak" for slot in SLOTS},
                "unexpected preference snapshot names")
        for slot in SLOTS:
            record = by_name[slot]
            require(record["state"] == "CAPTURED" and by_name[slot + ".bak"]["state"] == "ABSENT",
                    "missing XML or unexpected backup file")
            captured = Path(record["private_path"])
            require(not captured.is_symlink() and captured.is_file(), "snapshot not a regular file")
            require(captured.resolve().parent == path.resolve().parent, "snapshot outside trace directory")
            content = captured.read_bytes()
            require(len(content) == record["bytes"] and digest(content) == record["sha256"],
                    "snapshot identity mismatch")
            snapshots[stage, slot] = (content, entries(captured))
            file_evidence.append({"stage": stage, "slot": slot, "bytes": len(content),
                                  "sha256": digest(content), "backup": "ABSENT"})

    require(snapshot_ranges["before_attach"][1] < reads[0]["begin"]
            and reads[0]["end"] < snapshot_ranges["before_target_store"][0]
            and snapshot_ranges["before_target_store"][1] < roots[0]["begin"]
            and roots[0]["end"] < snapshot_ranges["after_target_store"][0]
            and snapshot_ranges["after_target_store"][1] < reads[1]["begin"]
            and reads[2]["end"] < snapshot_ranges["after_restore_store"][0],
            "file snapshot phase attribution differs")

    commits_evidence, target_operations = [], []
    required = (EDITOR + ".commitToMemory", PREF + ".enqueueDiskWrite", PREF + ".writeToFile",
                PREF + ".createFileOutputStream")
    for root_index, root in enumerate(roots):
        require(root["scope"] and root["scope"]["attribution"] == "matched_entry",
                "store root lacks scope")
        writers = within(calls, root, "c.a.n")
        require(len(writers) == 2, "expected two slot writers")
        operations = []
        for slot_index, writer in enumerate(writers):
            require(writer["args"] == root["args"], "slot plaintext differs from store request")
            commits = within(calls, writer, EDITOR + ".commit")
            edits = within(calls, writer, EDITOR + ".putString") + within(calls, writer, EDITOR + ".remove")
            require(len(commits) == len(edits) == 1 and commits[0]["result"] is True,
                    "missing edit or unsuccessful commit")
            commit, edit = commits[0], edits[0]
            require(edit["end"] < commit["begin"], "commit precedes edit")
            nested = [within(calls, commit, method) for method in required]
            require(all(len(group) == 1 for group in nested), "incomplete disk commit chain")
            memory, enqueue, write, create = [group[0] for group in nested]
            require(memory["end"] < enqueue["begin"] < write["begin"] < create["begin"]
                    < create["end"] < write["end"] < enqueue["end"], "disk commit order differs")
            slot = SLOTS[slot_index]
            require(Path(create["args"][0]["path"]).name == slot, "slot commit order differs")
            stage = "after_target_store" if root_index == 0 else "after_restore_store"
            content, parsed = snapshots[stage, slot]
            if edit["method"] == EDITOR + ".putString":
                require(len(edit["args"]) == 2 and parsed.get(edit["args"][0]) == edit["args"][1],
                        "XML bytes differ from observed encoded edit")
            else:
                require(len(edit["args"]) == 1 and edit["args"][0] not in parsed,
                        "removed encoded key remains on disk")
            operations.append((edit["method"].rsplit(".", 1)[1], edit["args"][0]))
            if root_index == 0:
                target_operations.append(operations[-1])
            else:
                require(edit["args"][0] == target_operations[slot_index][1],
                        "restore edits a different encoded key")
            commits_evidence.append({"transaction": "target" if root_index == 0 else "restore",
                                     "slot": slot, "operation": operations[-1][0],
                                     "commit_return": True, "write_to_file_returned": True,
                                     "xml_sha256": digest(content)})
        require(writers[0]["end"] < writers[1]["begin"], "slot writers overlap")
        backup = within(calls, root, "com.supercell.titan.GameApp.requestBackup")
        proxy = within(calls, root, "android.app.backup.IBackupManager$Stub$Proxy.dataChanged")
        require(len(backup) == len(proxy) == 1 and writers[-1]["end"] < backup[0]["begin"]
                and proxy[0]["args"] == ["nullsroyale.rel.free"], "backup request chain missing")
        for slot_index, slot in enumerate(SLOTS):
            before, initial_entries = snapshots["before_target_store", slot]
            require(snapshots["before_attach", slot][0] == before, "pre-transaction XML changed")
            require(snapshots["after_restore_store", slot][0] == before, "XML bytes not restored")
            changed = snapshots["after_target_store", slot][1]
            require(changed != initial_entries, "target XML did not change")
            if root_index == 0:
                edited_key = operations[slot_index][1]
                require({key: value for key, value in changed.items() if key != edited_key} ==
                        {key: value for key, value in initial_entries.items() if key != edited_key},
                        "unrelated entries changed")
    require(set(required) | {EDITOR + ".commit", "c.a.n"} <= installed, "required hooks missing")
    require(path.read_bytes() == raw, "trace changed during audit")
    return {"schema": "nashura.loading-store-disk-audit.v1", "status": "RUNTIME_OBSERVED",
            "scope": "one recorded Android store/restore; no Linux equivalence gate",
            "trace_sha256": digest(raw), "records": len(rows),
            "counts": dict(sorted(counts.items())), "commits": commits_evidence,
            "snapshots": file_evidence, "api_value_restored": True, "xml_bytes_restored": True,
            "two_slot_order": list(SLOTS), "backup_order": "after both synchronous commits",
            "cross_process_attribution": "temporal correlation only; no Binder transaction IDs",
            "journal": "NOT_OBSERVED_IN_THIS_WINDOW", "fsync": "UNCOVERED",
            "cross_process_recovery": "NOT_RUN", "async_disk_attribution": "NOT_RUN",
            "engine_identity_pre_post": "NOT_CAPTURED_THIS_TRANSACTION",
            "linux_backup_binding": "UNRESOLVED", "catalog_ready_pass": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--trace-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.trace, args.trace_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
