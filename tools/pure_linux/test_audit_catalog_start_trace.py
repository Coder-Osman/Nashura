import json

import pytest

import audit_catalog_start_trace as module


def fixture(root, monkeypatch):
    names = ("android", "android_before", "android_after", "historical_update", "linux_batch", "linux_gdb",
             "linux_resource_result", "linux_trace", "linux_stderr")
    paths = {name: root / name for name in names}
    inventory = json.dumps(["a" * 64 + "  ./csv_logic/a"])
    for name in ("android_before", "android_after", "historical_update"):
        paths[name].write_text(inventory)
    events = [{"kind": "engine_hooks_ready"}]
    events += [{"kind": "table_batch", "args": row, "listCount": 158} for row in module.BATCHES]
    events += [{"kind": "filesystem_open", "path": "csv_logic/a"}]
    trace = {"events": events, "errors": [], "lost": 0, "coverage": [
        {"label": label, "installed": True} for label in ("table_batch", "filesystem_open", "asset_open")]}
    paths["android"].write_text(json.dumps({"status": "RUNTIME_OBSERVED_REQUIRES_AUDIT",
        "engine_sha256": module.ENGINE, "catalog_ready_pass": False, "update_unchanged": True,
        "spawn_thread_alive": False, "update_before": {"sha256": module.sha(paths["android_before"])},
        "update_after": {"sha256": module.sha(paths["android_after"])}, "trace": trace}))
    gdb = {"exit_code": 0, "targets": [{"armed": True, "site_only": True, "calls": 5}],
           "events": [{"name": "loader_batch_call_site", "arguments": [0, 0, *row]} for row in module.BATCHES]}
    paths["linux_gdb"].write_text(json.dumps(gdb))
    paths["linux_batch"].write_text(json.dumps({"exit_code": 0, "status": "BLOCKED", "catalog_ready_pass": False,
        "inputs_before": {"libg.so": module.ENGINE}, "inputs_unchanged": True, "resources_unchanged": True,
        "auxiliary_unchanged": True, "probe_unchanged": True, "postcheck_errors": [],
        "outputs": {"observation.json": module.sha(paths["linux_gdb"])}}))
    paths["linux_resource_result"].write_text("{}")
    paths["linux_trace"].write_text("trace")
    paths["linux_stderr"].write_text("NATIVE_ASSET_OPEN\tcsv_logic/a\t1\n")
    monkeypatch.setattr(module, "audit_loader", lambda *args: None)
    monkeypatch.setattr(module, "analyze", lambda *args, **kwargs: {"records": []})
    return paths


def test_checks_batch_identity_and_path_set(tmp_path, monkeypatch):
    paths = fixture(tmp_path, monkeypatch)
    identities = {name: module.sha(path) for name, path in paths.items()}
    result = module.audit(paths, identities, {"assets": "/a", "update": "/u"}, "/work")
    assert result["batch_arguments_match"] and result["android_only_paths"] == result["linux_only_paths"] == 0
    assert result["catalog_ready_pass"] is False
    paths["linux_gdb"].write_text("tampered")
    with pytest.raises(ValueError, match="identity"):
        module.audit(paths, identities, {"assets": "/a", "update": "/u"}, "/work")


def test_rejects_wrong_batch_with_rebound_file_hashes(tmp_path, monkeypatch):
    paths = fixture(tmp_path, monkeypatch)
    gdb = json.loads(paths["linux_gdb"].read_text())
    gdb["events"][0]["arguments"][3] = 1
    paths["linux_gdb"].write_text(json.dumps(gdb))
    linux = json.loads(paths["linux_batch"].read_text())
    linux["outputs"]["observation.json"] = module.sha(paths["linux_gdb"])
    paths["linux_batch"].write_text(json.dumps(linux))
    identities = {name: module.sha(path) for name, path in paths.items()}
    with pytest.raises(ValueError, match="batch"):
        module.audit(paths, identities, {"assets": "/a", "update": "/u"}, "/work")
