import pytest
import json

from audit_catalog_resource_selection import audit, classify, sha


def test_classifies_real_object_kinds_and_update_priority():
    android = [{"kind": "filesystem_open", "path": "csv_logic/a", "nonnull": True, "vtable": "0x18c35b0"},
               {"kind": "filesystem_open", "path": "csv_logic/b", "nonnull": True, "vtable": "0x18c3130"},
               {"kind": "filesystem_open", "path": "csv_logic/c", "nonnull": False, "vtable": None}]
    linux = [{"root": "update", "path": "csv_logic/b"}]
    stderr = "NATIVE_ASSET_OPEN\tcsv_logic/a\t4\nNATIVE_ASSET_ABSENT_FROM_APK\tcsv_logic/b\nNATIVE_ASSET_ABSENT_FROM_APK\tcsv_logic/c\n"
    rows, pairs, extra = classify(android, linux, stderr)
    assert len(rows) == 3 and extra == 0
    assert pairs == {("apk", "apk"): 1, ("update", "update"): 1, ("null", "absent"): 1}


def test_unknown_and_duplicate_are_rejected():
    row = {"kind": "filesystem_open", "path": "csv_logic/a", "nonnull": True, "vtable": "unknown"}
    with pytest.raises(ValueError, match="unclassified"):
        classify([row], [], "")
    row["vtable"] = "0x18c35b0"
    with pytest.raises(ValueError, match="repeated"):
        classify([row, row], [], "")


@pytest.mark.parametrize("error_count", [None, 1])
def test_rejects_unknown_or_failed_script_callbacks(tmp_path, error_count):
    names = ("android", "android_before", "android_after", "linux_result", "linux_trace", "linux_stderr")
    paths = {name: tmp_path / name for name in names}
    for path in paths.values():
        path.write_text("{}")
    capture = {"status": "RUNTIME_OBSERVED_REQUIRES_AUDIT", "catalog_ready_pass": False,
               "spawn_gate": {"disabled": True}, "trace": {"errors": [], "lost": 0,
               "activeScopes": 0, "activeBatchScopes": 0}}
    if error_count is not None:
        capture["script_error_count"] = error_count
    paths["android"].write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="incomplete"):
        audit(paths, {name: sha(path) for name, path in paths.items()}, {}, "/")
