import json
from types import SimpleNamespace

import pytest

import backup_method_audit as audit


def instructions():
    return [
        (0, "invoke-static {}, " + audit.OBTAIN),
        (3, "move-result-object v0"),
        (4, "invoke-static {}, " + audit.OBTAIN),
        (7, "move-result-object v1"),
        (8, "const-string v2, 'fixture'"),
        (10, "invoke-virtual {v0, v2}, Landroid/os/Parcel;.writeInterfaceToken:(Ljava/lang/String;)V"),
        (13, "invoke-virtual {v0, v6}, Landroid/os/Parcel;.writeString:(Ljava/lang/String;)V"),
        (16, "iget-object v2, v5, Landroid/app/backup/IBackupManager$Stub$Proxy;.mRemote:Landroid/os/IBinder;"),
        (18, "const/4 v3, #int 2 // #2"),
        (19, "const/4 v4, #int 0 // #0"),
        (20, "invoke-interface {v2, v3, v0, v1, v4}, " + audit.TRANSACT),
        (23, "return-void"),
    ]


def method(name, rows=None, catches="catches : (none)"):
    rows = rows if rows is not None else [(0, "return-void")]
    header = ("#0\naccess : PUBLIC\nregisters : 7\nins : 2\nouts : 5\n"
              "insns size : 24 16-bit code units\n0000: |[0000] " + name + "\n")
    return (header + "\n".join(f"0000: 00 |{location:04x}: {value}"
                                 for location, value in rows) +
            "\n" + catches + "\npositions     :\n").encode()


def all_methods():
    return b"\n".join(method(name, instructions() if name in
                             {audit.BACKUP_PROXY, audit.SERVICE_PROXY} else None)
                       for name in audit.METHODS)


def test_synchronous_distinct_parcel_provenance_is_not_runtime_delivery():
    result = audit.transaction(instructions())
    assert result["code"] == 2
    assert result["flags"] == 0
    assert result["oneway"] is False
    assert result["request_reply_distinct"] is True
    assert result["result_and_fallback_execution"] == "UNRESOLVED"


@pytest.mark.parametrize("old,new", [
    ("#int 2 // #2", "#int 3 // #3"),
    ("#int 0 // #0", "#int 1 // #1"),
    ("{v2, v3, v0, v1, v4}", "{v2, v3, v0, v0, v4}"),
    ("{v2, v3, v0, v1, v4}", "{v6, v3, v0, v1, v4}"),
    ("{v2, v3, v0, v1, v4}", "{v2, v3, v1, v0, v4}"),
    ("{v2, v3, v0, v1, v4}", "{v2, v3, v0, v1}"),
    ("const/4 v3, #int 2 // #2", "if-eqz v3, 0014"),
    ("const/4 v3, #int 2 // #2", "add-int/lit8 v3, v3, #int 2"),
    (audit.OBTAIN, "Lfixture/Unknown;.call:()Landroid/os/Parcel;"),
])
def test_unproven_or_changed_transaction_is_rejected(old, new):
    rows = [(location, text.replace(old, new)) for location, text in instructions()]
    with pytest.raises(ValueError):
        audit.transaction(rows)


def test_register_move_preserves_provenance_but_redefinition_invalidates():
    rows = instructions()
    rows.insert(10, (19, "move-object v7, v0"))
    rows[11] = (20, rows[11][1].replace("v0, v1", "v7, v1"))
    assert audit.transaction(rows)["request_reply_distinct"]
    rows.insert(11, (19, "move-object v7, v6"))
    with pytest.raises(ValueError, match="provenance"):
        audit.transaction(rows)


def test_missing_duplicate_transact_and_orphan_result_rejected():
    for rows in (instructions()[:-2], instructions() + instructions(),
                 [(0, "move-result-object v0")] + instructions()):
        with pytest.raises(ValueError):
            audit.transaction(rows)


@pytest.mark.parametrize("instruction", ["invoke-static/range {v0 .. v4}, " + audit.TRANSACT,
    "invoke-interface {p0, v0}, " + audit.TRANSACT, "not an invocation"])
def test_unsupported_invocation_encoding_rejected(instruction):
    with pytest.raises(ValueError):
        audit.invocation(instruction)


def test_catch_start_inclusive_end_exclusive_and_uncaught_call():
    name = audit.METHODS[1]
    rows = [(0, "invoke-static {}, Lfixture/Call;.before:()V"),
            (3, "invoke-static {}, Lfixture/Call;.inside:()V"),
            (6, "invoke-static {}, Lfixture/Call;.after:()V"),
            (9, "return-void")]
    catches = "catches : 1\n  0x0003 - 0x0006\n    Landroid/os/RemoteException; -> 0x0009"
    result = audit.analyze(method(name, rows, catches), b"dex\n", (name,))[name]
    assert [row["caught_by"] for row in result["call_exception_coverage"]] == [
        [], ["Landroid/os/RemoteException;"], []]
    assert result["status"] == "STATIC_FOUND"
    assert "0003:" not in json.dumps(result)


@pytest.mark.parametrize("catches", [
    "catches : 1\n  Landroid/os/RemoteException; -> 0x0009",
    "catches : 1\n  0x0003 - 0x0003\n    <any> -> 0x0009",
    "catches : 1\n  0x0001 - 0x0006\n    <any> -> 0x0009",
    "catches : 1\n  0x0003 - 0x0006\n    <any> -> 0x0008",
])
def test_invalid_catch_boundaries_rejected(catches):
    rows = [(0, "nop"), (3, "nop"), (6, "nop"), (9, "return-void")]
    with pytest.raises(ValueError):
        audit.body(method(audit.METHODS[0], rows, catches).decode())


def setup_inputs(tmp_path):
    dex, tool = tmp_path / "input.dex", tmp_path / "dexdump"
    dex.write_bytes(b"dex\n035\x00")
    tool.write_bytes(b"fixture tool")
    return dex, tool, [(dex, audit.sha256(dex.read_bytes()))], audit.sha256(tool.read_bytes())


def test_hash_binding_exclusive_output_and_private_failure_retention(tmp_path, monkeypatch):
    dex, tool, inputs, digest = setup_inputs(tmp_path)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="identity"):
        audit.audit(inputs, tool, "wrong", output)
    with pytest.raises(ValueError, match="identity"):
        audit.audit([(dex, "wrong")], tool, digest, output)
    assert not output.exists()
    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=7, stdout=b"private", stderr=b"failure"))
    with pytest.raises(ValueError, match="failed"):
        audit.audit(inputs, tool, digest, output)
    assert (output / "dex-0.stderr").read_bytes() == b"failure"
    assert json.loads((output / "dex-0.command.json").read_text())["exit_code"] == 7
    assert not (output / "report.json").exists()
    with pytest.raises(FileExistsError):
        audit.audit(inputs, tool, digest, output)


def test_realistic_dump_retains_unknowns_and_ignores_unselected_unicode(tmp_path, monkeypatch):
    dex, tool, inputs, digest = setup_inputs(tmp_path)
    raw = b"0000: |[0000] nonascii\xff\n" + all_methods()
    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=raw, stderr=b""))
    report = audit.audit(inputs, tool, digest, tmp_path / "output")
    assert report["inputs_unchanged"] is True
    assert report["runtime_parity"] == "NOT_RUN"
    assert report["backup_service_delivery"] == "UNRESOLVED"
    assert report["catalog_ready_pass"] is False
    assert set(report["methods"]) == set(audit.METHODS)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "input_mutation", "tool_mutation"])
def test_incomplete_or_changed_evidence_is_rejected(tmp_path, monkeypatch, damage):
    dex, tool, inputs, digest = setup_inputs(tmp_path)
    raw = all_methods()
    if damage == "missing":
        raw = method(audit.METHODS[0])
    elif damage == "duplicate":
        raw += method(audit.METHODS[0])

    def run(*args, **kwargs):
        if damage == "input_mutation":
            dex.write_bytes(b"dex\nchanged")
        elif damage == "tool_mutation":
            tool.write_bytes(b"changed")
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    monkeypatch.setattr(audit.subprocess, "run", run)
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        audit.audit(inputs, tool, digest, output)
    assert not (output / "report.json").exists()
