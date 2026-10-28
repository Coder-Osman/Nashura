import copy
import json
from pathlib import Path
import subprocess

import pytest

import audit_catalog_entry_sequences as module


def fixture():
    android_events = [{"kind": "engine_hooks_ready"}]
    linux_events = []
    values = []
    for index in range(158):
        values.append(("entry_type", index))
        if index < 155:
            values.append(("entry_id", index))
    offset = 0
    for number, (args, size) in enumerate(zip(module.BATCHES, (76, 78, 78, 78, 3))):
        call = number + 1
        android_events.append({"kind": "table_batch", "call": call, "args": args,
                               "listCount": 158, "thread": 42})
        linux_events.append({"name": "table_batch", "thread": [1, 2, 3], "return_x0": 0})
        for name, value in values[offset:offset + size]:
            android_events.append({"kind": name, "batch": call, "thread": 42, "value": value})
            linux_events.append({"name": name, "thread": [1, 2, 3], "return_x0": value})
        offset += size
    for index, event in enumerate(android_events):
        event["sequence"] = index
    android = {"status": "RUNTIME_OBSERVED_REQUIRES_AUDIT", "engine_sha256": module.ENGINE,
               "catalog_ready_pass": False, "update_unchanged": True, "resume": "returned",
               "script_error_count": 0, "owned_pid": 123,
               "spawn_gate": {"enabled": True, "disabled": True,
                              "events": [{"pid": 123, "identifier": "nullsroyale.rel.free:engine0"}]},
               "launch": {"exit": 0}, "trace": {"events": android_events, "errors": [], "lost": 0,
               "activeScopes": 0, "activeBatchScopes": 0,
               "coverage": [{"label": name, "installed": True} for name in ("table_batch", *module.NAMES)]}}
    targets = [{"name": name, "armed": True, "calls": count, "scope": scope,
                "scope_start": start, "site_only": False} for name, count, scope, start in
               (("table_batch", 5, None, "table_batch"), ("entry_type", 158, "table_batch", None),
                ("entry_id", 155, "table_batch", None))]
    linux = {"exit_code": 0, "active_scopes": 0, "targets": targets, "events": linux_events}
    return android, linux


def test_ordered_sequences_and_incomplete_evidence():
    android, linux = fixture()
    assert module.sequences(android, linux) == [76, 78, 78, 78, 3]
    changed = copy.deepcopy(android)
    next(row for row in changed["trace"]["events"] if row["kind"] == "entry_id")["value"] += 1
    with pytest.raises(ValueError, match="sequences differ"):
        module.sequences(changed, linux)
    changed = copy.deepcopy(linux)
    del changed["events"][1]["return_x0"]
    with pytest.raises(ValueError, match="return missing"):
        module.sequences(android, changed)
    changed = copy.deepcopy(android)
    changed["script_error_count"] = 1
    with pytest.raises(ValueError, match="capture incomplete"):
        module.sequences(changed, linux)


def test_read_only_invocation_context():
    root = Path(__file__).parent
    subprocess.run(["node", str(root / "catalog_trace_fixture.cjs"),
                    str(root / "android_catalog_start_trace.js")], check=True, timeout=10)


def test_audit_binds_files_and_rejects_changed_input(tmp_path):
    names = ("android", "android_script", "android_before", "android_after", "historical_update",
             "linux_result", "linux_observation")
    paths = {name: tmp_path / name for name in names}
    android, linux = fixture()
    paths["android_script"].write_text("fixture")
    inventory = json.dumps(["a" * 64 + "  ./example"])
    for name in ("android_before", "android_after", "historical_update"):
        paths[name].write_text(inventory)
    android["script_sha256"] = module.sha(paths["android_script"])
    for label in ("before", "after"):
        android["update_" + label] = {"sha256": module.sha(paths["android_" + label])}
    paths["android"].write_text(json.dumps(android))
    paths["linux_observation"].write_text(json.dumps(linux))
    paths["linux_result"].write_text(json.dumps({"status": "BLOCKED", "exit_code": 0, "timed_out": False,
        "catalog_ready_pass": False, "inputs_before": {"libg.so": module.ENGINE}, "postcheck_errors": [],
        "outputs": {"observation.json": module.sha(paths["linux_observation"])},
        "inputs_unchanged": True, "resources_unchanged": True, "auxiliary_unchanged": True,
        "probe_unchanged": True}))
    identities = {name: module.sha(path) for name, path in paths.items()}
    assert module.audit(paths, identities)["ordered_getter_values_match"] is True
    paths["linux_observation"].write_text("changed")
    with pytest.raises(ValueError, match="identity"):
        module.audit(paths, identities)
