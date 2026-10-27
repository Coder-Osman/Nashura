import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import preference_contract_job as job


EXPECTED = {"status": "PASS", "scope": "synthetic_backend_only", "failure_boundaries": 15,
            "scenario_groups": 8, "libg_loaded": False}


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    probe.write_bytes(b"fixture")
    output = tmp_path / "result"
    monkeypatch.setattr(sys, "argv", ["job", "--payload", str(tmp_path / "payload"),
                                      "--probe", str(probe), "--probe-sha256", job.digest(probe),
                                      "--output", str(output)])
    monkeypatch.setattr(job.platform, "system", lambda: "Linux")
    monkeypatch.setattr(job.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(job, "check_inputs", lambda path: {"fixture": "identity"})
    return output


def runner(summary=EXPECTED, code=0, timeout=False):
    def execute(command, output, environment, limit):
        assert "LD_PRELOAD" not in environment and "NASHURA_TEST" not in environment
        assert limit == 20
        (output / "stdout.log").write_text(json.dumps(summary))
        (output / "stderr.log").write_bytes(b"")
        return {"command": command, "exit_code": code, "timed_out": timeout}
    return execute


def test_two_processes_preserve_scope_and_identities(invocation, monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "fixture")
    monkeypatch.setenv("NASHURA_TEST", "fixture")
    monkeypatch.setattr(job, "run", runner())
    assert job.main() == 0
    evidence = json.loads((invocation / "result.json").read_text())
    assert len(evidence["records"]) == 2
    assert all(row["passed"] for row in evidence["records"])
    assert evidence["inputs_unchanged"] and evidence["probe_unchanged"]
    assert evidence["android_runtime_parity"] == "NOT_RUN"
    assert evidence["catalog_ready_pass"] is False
    assert evidence["records"][0]["stderr_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("summary,code,timeout", [
    ({**EXPECTED, "libg_loaded": True}, 0, False), (EXPECTED, 2, False),
    (EXPECTED, 0, True), ("invalid summary", 0, False)])
def test_failed_children_leave_evidence_and_run_independently(invocation, monkeypatch, summary, code, timeout):
    monkeypatch.setattr(job, "run", runner(summary, code, timeout))
    assert job.main() == 1
    evidence = json.loads((invocation / "result.json").read_text())
    assert len(evidence["records"]) == 2
    assert not any(row["passed"] for row in evidence["records"])


def test_wrong_environment_and_identity_are_rejected(invocation, monkeypatch):
    monkeypatch.setattr(job, "run", lambda *args: pytest.fail("child must not start"))
    monkeypatch.setattr(job.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="ARM64 Linux"):
        job.main()
    monkeypatch.setattr(job.platform, "system", lambda: "Linux")
    monkeypatch.setattr(job, "digest", lambda path: "wrong")
    with pytest.raises(ValueError, match="identity"):
        job.main()


def test_changed_input_is_a_failed_run(invocation, monkeypatch):
    observations = iter(({"fixture": "before"}, {"fixture": "after"}))
    monkeypatch.setattr(job, "check_inputs", lambda path: next(observations))
    monkeypatch.setattr(job, "run", runner())
    assert job.main() == 1
    assert not json.loads((invocation / "result.json").read_text())["inputs_unchanged"]


def test_existing_output_is_not_overwritten(invocation, monkeypatch):
    invocation.mkdir()
    marker = invocation / "marker"
    marker.write_bytes(b"historical")
    monkeypatch.setattr(job, "run", lambda *args: pytest.fail("child must not start"))
    with pytest.raises(FileExistsError):
        job.main()
    assert marker.read_bytes() == b"historical"


def test_c_probe_builds_and_asserts_contract(tmp_path):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    root = Path(__file__).parent
    output = tmp_path / "probe"
    subprocess.run([compiler, "-Wall", "-Wextra", "-Werror",
                    str(root / "preference_contract.c"), str(root / "preference_contract_probe.c"),
                    "-o", str(output)], check=True, capture_output=True)
    result = subprocess.run([str(output)], capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout) == EXPECTED
    assert not result.stderr
