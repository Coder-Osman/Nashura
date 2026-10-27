import hashlib
import struct

import pytest

import backup_trace_audit as audit


def trace(events=None, extra_rows="", overflow="false"):
    methods = "\n".join("\t".join((hex(4 * index), *method, "Source.java", "1"))
                        for index, method in enumerate(audit.METHODS, 1))
    if events is None:
        events = [(1, 4, 0), (1, 16, 0), (1, 60, 0), (1, 60, 1), (1, 16, 1), (1, 4, 1)]
    header = (f"*version\n3\ndata-file-overflow={overflow}\nclock=dual\n"
              f"num-method-calls={len(events)}\n*threads\n1\tfixture\n*methods\n{methods}"
              f"{extra_rows}\n*end\n").encode()
    binary = struct.pack("<4sHHQH", b"SLOW", 3, 32, 0, 14).ljust(32, b"\x00")
    return header + binary + b"".join(struct.pack("<HIII", thread, identity | action, index, index)
                                     for index, (thread, identity, action) in enumerate(events))


def test_nested_native_route_and_explicit_evidence_limits():
    report = audit.parse(trace())
    assert report["roots"][0]["completion"] == "returned"
    assert report["roots"][0]["maximum_depth"] == 3
    assert report["counts_scoped"][audit.label(audit.METHODS[14])] == {
        "entered": 1, "returned": 1, "unwound": 0}
    assert report["backup_service_delivery"] == "UNRESOLVED"
    assert report["arguments_and_return_values"] == "NOT_RECORDED"
    assert report["catalog_ready_pass"] is False
    assert "Source.java" not in str(report)


def test_zero_events_are_not_a_claim_of_absent_dependency():
    raw = trace()
    row = "\t".join(("0x40", *audit.METHODS[15], "Source.java", "1")) + "\n"
    report = audit.parse(raw.replace(row.encode(), b""))
    absent = report["method_coverage"][audit.label(audit.METHODS[15])]
    assert absent == {"metadata_present": False, "scope_events": 0}
    assert report["method_coverage"][audit.label(audit.METHODS[16])] == {
        "metadata_present": True, "scope_events": 0}


def test_interleaved_threads_and_opaque_records_stay_in_scope():
    events = [(9, 1000, 1), (9, 4, 0), (9, 1004, 0), (7, 4, 0),
              (9, 1004, 1), (7, 4, 1), (9, 4, 1), (9, 1000, 0)]
    report = audit.parse(trace(events))
    assert report["root_threads"] == 2
    assert report["unknown_method_count"] == 2
    assert report["scoped_unknown_method_count"] == 1
    assert report["scoped_unknown_records"] == 2
    assert report["roots"][0]["unknown_records"] == 2
    assert report["scope_stacks_balanced"]


def test_unwind_is_not_normal_return():
    report = audit.parse(trace([(1, 4, 0), (1, 16, 0), (1, 16, 2), (1, 4, 2)]))
    assert report["roots"][0]["completion"] == "unwound"
    assert report["counts_scoped"][audit.label(audit.METHODS[3])]["unwound"] == 1


def test_identical_duplicate_metadata_retained_but_conflict_rejected():
    extra = "\n" + "\t".join(("0x4", *audit.ROOT, "Source.java", "1"))
    assert audit.parse(trace(extra_rows=extra))["identical_duplicate_metadata_rows"] == 1
    with pytest.raises(ValueError, match="conflicting"):
        audit.parse(trace(extra_rows=extra.replace("Source.java", "Other.java")))


@pytest.mark.parametrize("events", [[], [(1, 4, 0)], [(1, 4, 0), (1, 16, 1)],
    [(1, 4, 0), (1, 16, 0), (1, 4, 1)], [(1, 4, 0), (1, 4, 0)], [(0, 4, 0)], [(1, 4, 3)]])
def test_invalid_or_incomplete_scopes_rejected(events):
    with pytest.raises(ValueError):
        audit.parse(trace(events))


@pytest.mark.parametrize("damage", ["boundary", "partial", "layout", "count", "overflow", "clock", "duplicate"])
def test_invalid_envelope_rejected(damage):
    raw = trace()
    if damage == "boundary": raw = raw.replace(b"*end\n", b"*other\n")
    elif damage == "partial": raw = raw[:-1]
    elif damage == "layout": raw = raw.replace(b"SLOW", b"FAST")
    elif damage == "count": raw = raw.replace(b"num-method-calls=6", b"num-method-calls=7")
    elif damage == "overflow": raw = trace(overflow="true")
    elif damage == "clock": raw = raw.replace(b"clock=dual", b"clock=wall")
    else: raw = raw.replace(b"clock=dual\n", b"clock=dual\nclock=dual\n")
    with pytest.raises(ValueError):
        audit.parse(raw)


def test_hash_bound_read_only_input_and_exclusive_output(tmp_path):
    source, output = tmp_path / "input.trace", tmp_path / "report.json"
    source.write_bytes(trace())
    with pytest.raises(ValueError, match="identity"):
        audit.audit(source, "wrong", output)
    assert not output.exists()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    audit.audit(source, digest, output)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    with pytest.raises(FileExistsError):
        audit.audit(source, digest, output)
