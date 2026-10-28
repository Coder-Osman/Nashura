import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_backup_queue_readonly as reader


def setup(tmp_path, monkeypatch, damage=None):
    source = tmp_path / "observer.js"
    source.write_text("fixture observer")
    calls = []
    identities = iter(["system_server", "system_server"])

    def identity(*args):
        calls.append("identity")
        if damage == "identity":
            raise ValueError("system_server PID identity mismatch")
        return next(identities)

    def inspect():
        calls.append("inspect")
        if damage == "rpc":
            raise RuntimeError("private RPC failure")
        if damage == "source":
            source.write_text("changed")
        return {"read_only": True, "historical_branch": "UNRESOLVED", "catalog_ready_pass": False}

    script = SimpleNamespace(load=lambda: calls.append("load"),
                             exports_sync=SimpleNamespace(inspect=inspect))

    def detach():
        calls.append("detach")
        if damage == "detach":
            raise RuntimeError("private detach failure")

    session = SimpleNamespace(create_script=lambda text: script, detach=detach)
    device = SimpleNamespace(enumerate_processes=lambda: [SimpleNamespace(pid=42,
        name="other" if damage == "name" else "system_server")],
        attach=lambda pid: calls.append("attach") or session)
    frida = SimpleNamespace(get_device=lambda *args, **kwargs: device)
    monkeypatch.setattr(reader, "identity", identity)
    return source, frida, calls


def test_only_inspect_rpc_is_called_and_private_output_is_exclusive(tmp_path, monkeypatch):
    source, frida, calls = setup(tmp_path, monkeypatch)
    output = tmp_path / "result.json"
    record = reader.inspect(Path("adb"), "fixture", 42, source, output, frida)
    assert calls == ["identity", "attach", "load", "inspect", "identity", "detach"]
    assert json.loads(output.read_text()) == record
    assert record["inspection"]["catalog_ready_pass"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        reader.inspect(Path("adb"), "fixture", 42, source, output, frida)
    assert output.read_bytes() == original


@pytest.mark.parametrize("damage", ["identity", "name", "rpc", "source"])
def test_failure_saved_and_attached_session_detached(tmp_path, monkeypatch, damage):
    source, frida, calls = setup(tmp_path, monkeypatch, damage)
    output = tmp_path / "result.json"
    with pytest.raises((ValueError, RuntimeError)):
        reader.inspect(Path("adb"), "fixture", 42, source, output, frida)
    record = json.loads(output.read_text())
    assert record["error"]
    if damage in {"identity", "name"}:
        assert "attach" not in calls
    else:
        assert calls[-1] == "detach"


def test_detach_error_is_retained(tmp_path, monkeypatch):
    source, frida, calls = setup(tmp_path, monkeypatch, "detach")
    record = reader.inspect(Path("adb"), "fixture", 42, source, tmp_path / "out", frida)
    assert record["detach_error"] == "private detach failure"


@pytest.mark.parametrize("cmdline", [b"other\x00", b"system_server extra\x00", b"system_server\x00arg"])
def test_real_identity_rejects_other_processes(monkeypatch, cmdline):
    monkeypatch.setattr(reader.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=cmdline))
    with pytest.raises(ValueError, match="PID identity"):
        reader.identity("adb", "fixture", 42)


def test_real_identity_accepts_exact_nul_terminated_name(monkeypatch):
    monkeypatch.setattr(reader.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=b"system_server\x00\r\n"))
    assert reader.identity("adb", "fixture", 42) == "system_server"
