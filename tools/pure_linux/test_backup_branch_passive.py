import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import run_backup_branch_passive as runner


@pytest.mark.parametrize("scenario", ["first_duplicate", "unrelated_identity", "null_nonmember",
                                     "nested", "exception", "observer_failure", "record_failure", "bounded"])
def test_real_script_scope_in_mock_java(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for Java mock boundary test")
    directory = Path(__file__).parent
    result = subprocess.run([node, str(directory / "backup_branch_fixture.cjs"),
                             str(directory / "backup_branch_observer.js"), scenario],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MOCK_BRANCH_PASS " + scenario


def setup(tmp_path, monkeypatch, damage=None):
    source = tmp_path / "source.js"
    source.write_text("fixture only")
    calls = []
    current = {"boot_id": "fixture", "starttime": 100, "pid": 522, "cmdline": "system_server"}

    def identity(*args):
        calls.append("identity")
        if damage == "pid_reuse" and calls.count("identity") == 3:
            return {**current, "starttime": 101}
        return current.copy()

    header = {"schema": runner.SCHEMA, "pid": 522, "installed": True, "passive": True}
    readiness = {**header, "hooks": sorted(runner.REQUIRED_HOOKS)}
    observation = {**header, "events": [], "dropped": 0, "activeScopes": 0,
                   "observationErrors": 0, "naturalEntries": 0, "catalogReadyPass": False}
    if damage == "hook":
        readiness["hooks"].pop()
    if damage == "readiness":
        readiness["passive"] = False
    if damage == "snapshot":
        observation["pid"] = 523
    if damage == "events":
        observation["events"] = [{}]
    if damage == "counter":
        observation["dropped"] = True
    if damage == "count_mismatch":
        observation["naturalEntries"] = 1
    if damage in {"dropped", "activeScopes", "observationErrors"}:
        observation[damage] = 1
    if damage == "natural":
        observation["naturalEntries"] = 1
        observation["events"] = [{"event": "entry"}]

    def snapshot():
        calls.append("snapshot")
        if damage == "rpc":
            raise RuntimeError("snapshot failed")
        if damage == "source":
            source.write_text("changed")
        return observation

    def detach():
        calls.append("detach")
        if damage == "detach":
            raise RuntimeError("detach failed")

    script = SimpleNamespace(load=lambda: calls.append("load"), exports_sync=SimpleNamespace(
        install=lambda: calls.append("install") or readiness, snapshot=snapshot))
    session = SimpleNamespace(create_script=lambda text: script, detach=detach)
    device = SimpleNamespace(enumerate_processes=lambda: [SimpleNamespace(pid=522, name="system_server")],
                             attach=lambda pid: calls.append("attach") or session)
    frida = SimpleNamespace(get_device=lambda *args, **kwargs: device)
    monkeypatch.setattr(runner, "identity", identity)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append("sleep"))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return source, digest, frida, calls


def test_only_passive_rpcs_and_exclusive_private_output(tmp_path, monkeypatch):
    source, digest, frida, calls = setup(tmp_path, monkeypatch)
    output = tmp_path / "out.json"
    result = runner.run("adb", "fixture", 522, source, digest, 45, output, frida)
    assert result["status"] == "NOT_OBSERVED"
    assert result["requests_issued"] == result["stores_issued"] == 0
    assert result["catalog_ready_pass"] is False
    assert calls == ["identity", "attach", "load", "install", "identity", "sleep",
                     "snapshot", "identity", "detach"]
    assert json.loads(output.read_text()) == result
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        runner.run("adb", "fixture", 522, source, digest, 45, output, frida)
    assert output.read_bytes() == original


@pytest.mark.parametrize("damage", ["hook", "readiness", "snapshot", "events", "counter",
                                    "count_mismatch", "pid_reuse", "source", "rpc"])
def test_failures_saved_and_session_detached(tmp_path, monkeypatch, damage):
    source, digest, frida, calls = setup(tmp_path, monkeypatch, damage)
    output = tmp_path / "out.json"
    with pytest.raises((ValueError, RuntimeError)):
        runner.run("adb", "fixture", 522, source, digest, 45, output, frida)
    record = json.loads(output.read_text())
    assert record["error"]
    assert record["status"] == "FAILED"
    assert calls[-1] == "detach"


@pytest.mark.parametrize("damage", ["dropped", "activeScopes", "observationErrors", "detach"])
def test_coverage_errors_never_look_like_complete_zero(tmp_path, monkeypatch, damage):
    source, digest, frida, calls = setup(tmp_path, monkeypatch, damage)
    result = runner.run("adb", "fixture", 522, source, digest, 1, tmp_path / "out", frida)
    assert result["status"] == "INCOMPLETE_COVERAGE"


def test_natural_events_require_review(tmp_path, monkeypatch):
    source, digest, frida, calls = setup(tmp_path, monkeypatch, "natural")
    result = runner.run("adb", "fixture", 522, source, digest, 1, tmp_path / "out", frida)
    assert result["status"] == "RUNTIME_OBSERVED_REQUIRES_REVIEW"


@pytest.mark.parametrize("seconds", [0, -1, 61, float("nan")])
def test_invalid_window_rejected_before_attach(tmp_path, monkeypatch, seconds):
    source, digest, frida, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="window"):
        runner.run("adb", "fixture", 522, source, digest, seconds, tmp_path / "out", frida)
    assert calls == []


def test_wrong_script_rejected_before_attach(tmp_path, monkeypatch):
    source, digest, frida, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="SHA"):
        runner.run("adb", "fixture", 522, source, "0" * 64, 1, tmp_path / "out", frida)
    assert calls == []


def test_identity_reads_boot_pid_starttime(monkeypatch):
    fields = ["S"] + ["0"] * 18 + ["987"]
    values = [b"boot-id\n", b"system_server\x00", ("522 (system_server) " + " ".join(fields)).encode()]
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=values.pop(0)))
    assert runner.identity("adb", "fixture", 522)["starttime"] == 987
