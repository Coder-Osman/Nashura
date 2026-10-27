from types import SimpleNamespace

import pytest

import platform_preference_audit as audit


NAME = "android.app.SharedPreferencesImpl.commit:()Z"


def method(body=b"0001: return v0"):
    return (b"#0\naccess : PUBLIC\nregisters : 2\nins : 1\nouts : 1\ninsns size : 2 16-bit code units\n" +
            b"0000: |[0000] " + NAME.encode() + b"\n0000: 00 |" + body +
            b"\ncatches : (none)\npositions     :\n")


def test_invalid_unselected_utf8_does_not_corrupt_selected_method():
    data = b"unrelated literal \xc0\x80\n" + method() + b"unrelated \xff\n"
    summary = audit.summarize(data, b"dex\n", (NAME,))
    assert summary[NAME]["instructions"] == 1
    assert summary[NAME]["status"] == "STATIC_FOUND"
    assert len(summary[NAME]["sha256"]) == 64


@pytest.mark.parametrize("data", [method(b"0001: const-string v0, '\xff'"),
    method(b"0001: const-string v0, '\xef\xbf\xbd'"), method() + method(), b"", method().replace(b"catches", b"unknown")])
def test_incomplete_duplicate_or_damaged_selected_method_rejected(data):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        audit.summarize(data, b"dex\n", (NAME,))


def test_direct_call_operand_preserved_without_raw_instructions():
    data = method(b"0001: invoke-static {}, Landroid/os/ServiceManager;.getService:(Ljava/lang/String;)Landroid/os/IBinder; // method@0001")
    row = audit.summarize(data, b"dex\n", (NAME,))[NAME]
    assert row["direct_calls"] == ["Landroid/os/ServiceManager;.getService:(Ljava/lang/String;)Landroid/os/IBinder;"]
    assert "body" not in row


def test_wrong_identity_and_existing_output_rejected(tmp_path):
    dex, tool = tmp_path / "input.dex", tmp_path / "dexdump"
    dex.write_bytes(b"dex\n035\x00")
    tool.write_bytes(b"tool")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="identity"):
        audit.audit(dex, "wrong", tool, audit.sha256(tool.read_bytes()), output)
    assert not output.exists()
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_bytes(b"history")
    with pytest.raises(FileExistsError):
        audit.audit(dex, audit.sha256(dex.read_bytes()), tool, audit.sha256(tool.read_bytes()), output)
    assert sentinel.read_bytes() == b"history"


def test_tool_failure_preserved_without_report(tmp_path, monkeypatch):
    dex, tool = tmp_path / "input.dex", tmp_path / "dexdump"
    dex.write_bytes(b"dex\n035\x00")
    tool.write_bytes(b"tool")
    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=7, stdout=b"private", stderr=b"failure"))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="failed"):
        audit.audit(dex, audit.sha256(dex.read_bytes()), tool, audit.sha256(tool.read_bytes()), output)
    assert (output / "dexdump.stderr").read_bytes() == b"failure"
    assert not (output / "report.json").exists()
