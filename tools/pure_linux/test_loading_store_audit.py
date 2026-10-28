"""Synthetic evidence fixtures test the verifier, not Android semantics."""

import json
from pathlib import Path

import pytest

import loading_store_audit as audit


def evidence(tmp_path):
    rows = []
    scope = {"scopeId": "fixture", "attribution": "matched_entry"}
    package = "nullsroyale.rel.free"

    def event(kind, details):
        rows.append({"target_pid": 42, "message": {"type": "send", "payload": {
            "pid": 42, "threadId": 43, "event": kind, "details": details}}})

    def call(method, args=(), result=None, body=None):
        event("hook_installed", {"className": method.rsplit(".", 1)[0],
                                 "methodName": method.rsplit(".", 1)[1]})
        details = {"method": method + "(fixture)", "args": list(args), "scope": scope}
        event("call_enter", details)
        if body:
            body()
        event("call_return", {**details, "result": result})

    def read(value, phase):
        call(audit.APP + ".getKeyValue", [audit.KEY], value)
        rows.append({"phase": phase, "result": {"key": audit.KEY, "value": value}})

    rows.append({"phase": "identity", "app": {"processName": package}})
    def snapshot(stage):
        for slot_index, slot in enumerate(audit.SLOTS):
            content = b"<map/>" if stage != "after_target_store" else (
                '<map><string name="private-key-%d">private-value-%d</string></map>' %
                (slot_index, slot_index)).encode()
            path = tmp_path / (stage + "-" + slot)
            path.write_bytes(content)
            rows.append({"phase": stage, "preference_file": {
                "remote_path": "/private/" + slot, "private_path": str(path),
                "state": "CAPTURED", "sha256": audit.digest(content), "bytes": len(content)}})
            rows.append({"phase": stage, "preference_file": {
                "remote_path": "/private/" + slot + ".bak", "state": "ABSENT"}})

    def store(value):
        def writers():
            for slot_index, slot in enumerate(audit.SLOTS):
                def writer(slot_index=slot_index, slot=slot):
                    encoded_key = "private-key-%d" % slot_index
                    edit = "putString" if value else "remove"
                    args = [encoded_key, "private-value-%d" % slot_index] if value else [encoded_key]
                    call(audit.EDITOR + "." + edit, args)

                    def commit():
                        call(audit.EDITOR + ".commitToMemory")
                        call(audit.PREF + ".enqueueDiskWrite", body=lambda:
                             call(audit.PREF + ".writeToFile", body=lambda:
                                  call(audit.PREF + ".createFileOutputStream",
                                       [{"path": "/private/" + slot}])))

                    call(audit.EDITOR + ".commit", result=True, body=commit)
                call("c.a.n", [audit.KEY, value], body=writer)
            call("com.supercell.titan.GameApp.requestBackup", body=lambda:
                 call("android.app.backup.IBackupManager$Stub$Proxy.dataChanged", [package]))
        call(audit.APP + ".storeKeyValue", [audit.KEY, value], body=writers)

    snapshot("before_attach")
    read("", "read_initial")
    snapshot("before_target_store")
    store("test")
    snapshot("after_target_store")
    read("test", "read_after_target_store")
    store("")
    read("", "read_after_restore")
    snapshot("after_restore_store")
    return rows


def check(tmp_path, rows, expected=None):
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return audit.audit(path, expected or audit.digest(path.read_bytes()))


def payload(row):
    return row.get("message", {}).get("payload", {})


def test_complete_fixture_restoration_and_public_output_limits(tmp_path):
    report = check(tmp_path, evidence(tmp_path))
    assert len(report["commits"]) == 4
    assert report["xml_bytes_restored"] and report["api_value_restored"]
    assert report["catalog_ready_pass"] is False
    assert report["linux_backup_binding"] == "UNRESOLVED"
    assert report["cross_process_recovery"] == "NOT_RUN"
    assert report["fsync"] == "UNCOVERED"
    assert "private-key" not in json.dumps(report)
    assert "private-value" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)


@pytest.mark.parametrize("event", ["observer_error", "hook_unavailable", "method_unavailable",
    "call_throw", "disk_commit_untracked", "unattributed_disk_write"])
def test_observation_gaps_rejected(tmp_path, event):
    rows = evidence(tmp_path)
    rows.append({"message": {"type": "send", "payload": {"event": event}}})
    with pytest.raises(ValueError, match="coverage"):
        check(tmp_path, rows)


@pytest.mark.parametrize("damage", ["pid", "scope", "unfinished", "failed_commit", "wrong_slot",
    "readback", "extra_store", "missing_hook", "restore_error", "frida_error", "restore_key",
    "snapshot_phase"])
def test_incomplete_or_misattributed_transaction_rejected(tmp_path, damage):
    rows = evidence(tmp_path)
    enters = [row for row in rows if payload(row).get("event") == "call_enter"]
    if damage == "pid":
        enters[0]["target_pid"] += 1
    elif damage == "scope":
        enters[0]["message"]["payload"]["details"]["scope"] = None
    elif damage == "unfinished":
        rows.pop()
    elif damage == "failed_commit":
        returns = [row for row in rows if payload(row).get("event") == "call_return"
                   and payload(row)["details"]["method"].startswith(audit.EDITOR + ".commit(")]
        payload(returns[0])["details"]["result"] = False
    elif damage == "wrong_slot":
        create = next(row for row in enters if "createFileOutputStream" in payload(row)["details"]["method"])
        payload(create)["details"]["args"][0]["path"] = "/private/wrong.xml"
    elif damage == "readback":
        next(row for row in rows if row.get("phase") == "read_after_target_store")["result"]["value"] = "wrong"
    elif damage == "extra_store":
        roots = [row for row in enters if ".storeKeyValue(" in payload(row)["details"]["method"]]
        rows.extend([roots[0], rows[-1]])
    elif damage == "missing_hook":
        rows = [row for row in rows if not (payload(row).get("event") == "hook_installed"
                and payload(row)["details"]["methodName"] == "writeToFile")]
    elif damage == "restore_error":
        rows.append({"phase": "restore_failed"})
    elif damage == "frida_error":
        rows.append({"message": {"type": "error"}})
    elif damage == "restore_key":
        remove = next(row for row in enters if ".remove(" in payload(row)["details"]["method"])
        payload(remove)["details"]["args"][0] = "wrong-private-key"
    else:
        index = next(index for index, row in enumerate(rows)
                     if row.get("phase") == "after_target_store" and "preference_file" in row)
        rows.insert(0, rows.pop(index))
    with pytest.raises(ValueError):
        check(tmp_path, rows)


@pytest.mark.parametrize("damage", ["digest", "file_hash", "file_size", "backup", "symlink", "outside",
                                     "duplicate", "changed_restore", "unrelated_change"])
def test_identity_and_file_snapshots_rejected(tmp_path, damage):
    rows = evidence(tmp_path)
    records = [row["preference_file"] for row in rows if "preference_file" in row
               and row["preference_file"]["state"] == "CAPTURED"]
    record = records[-1]
    path = Path(record["private_path"])
    if damage == "digest":
        with pytest.raises(ValueError, match="identity"):
            check(tmp_path, rows, "0" * 64)
        return
    if damage == "file_hash":
        record["sha256"] = "0" * 64
    elif damage == "file_size":
        record["bytes"] += 1
    elif damage == "backup":
        next(row["preference_file"] for row in rows
             if row.get("preference_file", {}).get("state") == "ABSENT")["state"] = "CAPTURED"
    elif damage == "symlink":
        target = path.with_suffix(".target")
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif damage == "outside":
        directory = tmp_path / "other"
        directory.mkdir()
        target = directory / path.name
        target.write_bytes(path.read_bytes())
        record["private_path"] = str(target)
    elif damage == "duplicate":
        rows.append(next(row for row in rows if "preference_file" in row))
    else:
        if damage == "unrelated_change":
            record = records[4]
            path = Path(record["private_path"])
            content = path.read_bytes().replace(b"</map>", b'<string name="other">changed</string></map>')
        else:
            content = b'<map><string name="other">changed</string></map>'
        path.write_bytes(content)
        record.update(sha256=audit.digest(content), bytes=len(content))
    with pytest.raises(ValueError):
        check(tmp_path, rows)
