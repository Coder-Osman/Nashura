import hashlib
import json

import pytest

from catalog_resource_audit import audit


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path):
    trace = tmp_path / "syscalls.1"
    stderr = tmp_path / "stderr.log"
    result = tmp_path / "result.json"
    trace.write_text('''newfstatat(AT_FDCWD, "/fixture/update/a", {st_size=2}, 0) = 0
openat(AT_FDCWD, "/fixture/update/a", O_RDONLY) = 5
fstat(5, {st_size=2}) = 0
mmap(NULL, 2, PROT_READ, MAP_SHARED, 5, 0) = 0xffff0000
close(5) = 0
openat(AT_FDCWD, "/fixture/assets/b", O_RDONLY) = 5
fstat(5, {st_size=1}) = 0
read(0x5, 0xff, 0x1) = 0x1
close(5) = 0
+++ exited with 0 +++
''')
    stderr.write_text("NATIVE_ASSET_OPEN\tb\t1\nNATIVE_ASSET_READ\tb\t1\n"
                      "NATIVE_ASSET_CLOSE\tb\nNATIVE_ASSET_ABSENT_FROM_APK\tc\n")
    resources = {"assets": {"a": "apk-hash", "b": "b-hash"}, "update": {"a": "update-hash"}}
    result.write_text(json.dumps({"exit_code": 0, "timed_out": False, "inputs_unchanged": True,
                                  "resources_unchanged": True, "auxiliary_unchanged": True,
                                  "postcheck_errors": [], "resources_before": resources,
                                  "resources_after": resources,
                                  "outputs": {trace.name: sha(trace), stderr.name: sha(stderr)}}))
    return result, trace, stderr


def run(paths, expected=None):
    result, trace, stderr = paths
    return audit(result, expected or sha(result), trace, stderr,
                 {"assets": "/fixture/assets", "update": "/fixture/update"}, "/fixture")


def test_conflicting_inventory_and_actual_opens_are_separately_reported(tmp_path):
    report = run(fixture(tmp_path))
    assert report["staged_same_name"] == report["update_open_also_staged_as_asset"] == 1
    assert report["update_open_with_different_staged_asset_sha"] == 1
    assert report["update_paths_also_opened_as_asset"] == 0
    assert report["asset_open_calls"] == report["asset_close_calls"] == 1
    assert report["asset_absent_calls"] == 1
    assert report["update_fully_mapped"] == 1
    assert report["android_resource_parity"] == "NOT_RUN" and not report["catalog_ready_pass"]


def test_result_and_trace_tampering_fail_closed(tmp_path):
    paths = fixture(tmp_path)
    with pytest.raises(ValueError, match="result identity"):
        run(paths, "0" * 64)
    paths[1].write_text(paths[1].read_text() + "tampered\n")
    with pytest.raises(ValueError, match="trace identity"):
        run(paths)


def test_ambiguous_roots_are_rejected(tmp_path):
    result, trace, stderr = fixture(tmp_path)
    with pytest.raises(ValueError, match="resource roots"):
        audit(result, sha(result), trace, stderr,
              {"assets": "/fixture", "update": "/fixture/update"}, "/fixture")


@pytest.mark.parametrize("damage", ["missing_close", "incomplete_trace", "backend_failure", "missing_stat"])
def test_incomplete_runtime_evidence_is_rejected(tmp_path, damage):
    result, trace, stderr = fixture(tmp_path)
    if damage == "missing_close":
        stderr.write_text(stderr.read_text().replace("NATIVE_ASSET_CLOSE\tb\n", ""))
    elif damage == "backend_failure":
        stderr.write_text(stderr.read_text() + "NATIVE_ASSET_FATAL\tfixture\n")
    elif damage == "incomplete_trace":
        trace.write_text(trace.read_text().replace("+++ exited with 0 +++", ""))
    else:
        trace.write_text(trace.read_text().split("\n", 1)[1])
    record = json.loads(result.read_text())
    record["outputs"] = {trace.name: sha(trace), stderr.name: sha(stderr)}
    result.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        run((result, trace, stderr))
