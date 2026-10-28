import hashlib
import json

import pytest

from native_backup_binding_audit import audit
from test_native_backup_evidence import logs


def save(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def experiment(tmp_path):
    records = []
    for ordinal in range(3):
        path = tmp_path / f"process-{ordinal}"
        path.mkdir()
        records.append({"exit_code": 1, "consumer_exit": 0})
        if ordinal:
            stdout = logs() + "\nPARSED_TABLE_SUMMARY\tbefore\t0\t0\n"
            for slot in range(162):
                count = 150 if slot < 161 else 203
                stdout += f"PARSED_TABLE\tafter\t{slot}\t{count}\t{count}\t{count}\t-1\t-1\n"
            stdout += ("PARSED_TABLE_SUMMARY\tafter\t162\t24353\nLOADER_CANARY\t1\n"
                       "CATALOG_READY_AFTER_LOADER\t1\nNATIVE_ASSET_FINISH_RESULT\t0\nPHASE\tafter_close\n")
            stderr = ""
            store = path / "preference-store"
            store.mkdir()
            archive = store / "backup-fixture"
            archive.mkdir()
            identities = {}
            for slot in range(2):
                name = f"store-{slot}.xml"
                data = f"synthetic slot {slot}".encode()
                (store / name).write_bytes(data)
                (archive / name).write_bytes(data)
                identities[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            manifest = {"android_backup_parity": "NOT_VERIFIED", "files": identities}
            save(archive / "manifest.json", manifest)
            save(path / "consumer.json", {"status": "NATIVE_ARCHIVE_CONSUMED", **manifest})
        else:
            summary = {"result": -1, "deleted": False, "commits": 2, "disk_writes": 2,
                       "false_commits": 0, "backup_requests": 1, "backup_failures": 1,
                       "decode_exceptions": 0, "entries": [12, 12]}
            stdout = ("PREFERENCE_BACKEND_HOOK\thistorical_identity_ascii_xml_only_backup_unresolved\n"
                      "PREFERENCE_BACKEND_WRITE\t" + json.dumps(summary))
            stderr = ("PREFERENCE_BACKUP\tUNRESOLVED\tTID\t1\n"
                      "PREFERENCE_BACKEND_FATAL\tbackup service unresolved after persistence")
        (path / "stdout.log").write_text(stdout)
        (path / "stderr.log").write_text(stderr)
        result = {"status": "BLOCKED", "catalog_ready_pass": False, "exit_code": 0 if ordinal else 96,
                  "timed_out": False, "probe_sha256": "synthetic", "elapsed_seconds": 1,
                  "inputs_before": {}, "inputs_after": {}, "inputs_unchanged": True,
                  "probe_unchanged": True, "resources_unchanged": True, "auxiliary_unchanged": True,
                  "postcheck_errors": [], "catalog": {"loader_destroyed": True},
                  "outputs": {name: hashlib.sha256((path / name).read_bytes()).hexdigest()
                              for name in ("stdout.log", "stderr.log")}}
        save(path / "result.json", result)
    save(tmp_path / "batch-result.json", {"machine": "aarch64", "kernel": "fixture",
                                         "probe_sha256": "synthetic", "records": records})
    return tmp_path


def test_fixture_audit_keeps_strict_gate_closed(experiment):
    result = audit(experiment)
    assert result["audit"] == "PASS"
    assert result["catalog_ready_pass"] is False
    assert result["android_backup_parity"] == "NOT_VERIFIED"


@pytest.mark.parametrize("field,value", [("catalog_ready_pass", True), ("resources_unchanged", False),
                                        ("exit_code", 96), ("postcheck_errors", ["changed"]),
                                        ("probe_sha256", "wrong"), ("timed_out", True)])
def test_rejects_inconsistent_child(experiment, field, value):
    path = experiment / "process-1/result.json"
    result = json.loads(path.read_text())
    result[field] = value
    save(path, result)
    with pytest.raises(ValueError):
        audit(experiment)


def test_rejects_log_tampering(experiment):
    with (experiment / "process-1/stdout.log").open("a") as stream:
        stream.write("tampered")
    with pytest.raises(ValueError, match="output hash"):
        audit(experiment)


def test_rejects_archive_corruption(experiment):
    (experiment / "process-2/preference-store/backup-fixture/store-0.xml").write_bytes(b"changed")
    with pytest.raises(ValueError, match="archive bytes"):
        audit(experiment)


def test_rejects_pending_not_consumed(experiment):
    (experiment / "process-1/preference-store/.backup-pending").touch()
    with pytest.raises(ValueError, match="pending"):
        audit(experiment)
