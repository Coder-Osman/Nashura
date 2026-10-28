import json
from types import SimpleNamespace

import pytest

import backup_queue_contract as contract
from platform_preference_audit import selected_methods
from preference_method_audit import method_evidence, sha256


def fixture_method(instruction="return-void"):
    return ("#0\naccess : PRIVATE\nregisters : 3\nins : 3\nouts : 0\n"
            "insns size : 1 16-bit code units\n0000: |[0000] " + contract.METHOD +
            "\n0000: 00 |0000: " + instruction +
            "\ncatches : (none)\npositions     :\n").encode()


def bind_fixture(monkeypatch, raw):
    digest = method_evidence(selected_methods(raw, (contract.METHOD,)),
                             (contract.METHOD,), b"dex\n")[contract.METHOD]["sha256"]
    monkeypatch.setattr(contract, "REVIEWED_METHOD_SHA256", digest)


def test_real_review_pin_rejects_synthetic_method():
    with pytest.raises(ValueError, match="manually reviewed"):
        contract.review(fixture_method(), b"dex\n")


def test_exact_identity_is_review_binding_not_automatic_semantics(monkeypatch):
    raw = fixture_method()
    bind_fixture(monkeypatch, raw)
    result = contract.review(raw, b"dex\n")
    assert result["status"] == "STATIC_FOUND"
    assert result["automatic_semantic_inference"] is False
    assert result["runtime_parity"] == "NOT_RUN"
    assert result["historical_branch"] == "UNRESOLVED"
    assert result["job_delivery"] == "UNRESOLVED"
    assert result["catalog_ready_pass"] is False
    assert "0000:" not in json.dumps(result)


@pytest.mark.parametrize("change", ["branch", "metadata", "catch", "duplicate", "missing"])
def test_changed_or_incomplete_method_is_not_approved(monkeypatch, change):
    raw = fixture_method()
    bind_fixture(monkeypatch, raw)
    damaged = {
        "branch": fixture_method("if-eqz v0, 0000"),
        "metadata": raw.replace(b"registers : 3", b"registers : 4"),
        "catch": raw.replace(b"catches : (none)", b"catches : 0"),
        "duplicate": raw + raw, "missing": b"no method",
    }[change]
    with pytest.raises(ValueError):
        contract.review(damaged, b"dex\n")


def test_queue_old_value_controls_journal_but_not_normal_schedule(monkeypatch):
    raw = fixture_method()
    bind_fixture(monkeypatch, raw)
    rows = contract.review(raw, b"dex\n")["normal_return_cases"]
    fresh, duplicate = rows[2:]
    assert fresh["queue_put"] and duplicate["queue_put"]
    assert fresh["journal_call"] and not duplicate["journal_call"]
    assert fresh["schedule_call"] and duplicate["schedule_call"]
    assert rows[1]["schedule_call"] and not rows[1]["queue_put"]
    assert not rows[0]["schedule_call"]


def inputs(tmp_path):
    dex, tool = tmp_path / "input.dex", tmp_path / "tool"
    dex.write_bytes(b"dex\nfixture")
    tool.write_bytes(b"fixture tool")
    return dex, sha256(dex.read_bytes()), tool, sha256(tool.read_bytes())


@pytest.mark.parametrize("damage", ["dex_hash", "tool_hash", "dex_changed", "tool_changed", "exit", "method"])
def test_failure_retention_and_identity_boundaries(tmp_path, monkeypatch, damage):
    dex, digest, tool, tool_digest = inputs(tmp_path)
    raw = fixture_method()
    bind_fixture(monkeypatch, raw)

    def run(*args, **kwargs):
        if damage == "dex_changed":
            dex.write_bytes(b"dex\nchanged")
        if damage == "tool_changed":
            tool.write_bytes(b"changed")
        return SimpleNamespace(returncode=7 if damage == "exit" else 0,
                               stdout=fixture_method("nop") if damage == "method" else raw,
                               stderr=b"private diagnostics")

    monkeypatch.setattr(contract.subprocess, "run", run)
    output = tmp_path / "audit"
    with pytest.raises(ValueError):
        contract.audit(dex, "wrong" if damage == "dex_hash" else digest, tool,
                       "wrong" if damage == "tool_hash" else tool_digest, output)
    assert not (output / "report.json").exists()
    if damage not in {"dex_hash", "tool_hash"}:
        assert (output / "dexdump.stderr").read_bytes() == b"private diagnostics"
        assert json.loads((output / "command.json").read_text())["exit_code"] == (7 if damage == "exit" else 0)
        with pytest.raises(FileExistsError):
            contract.audit(dex, sha256(dex.read_bytes()), tool, sha256(tool.read_bytes()), output)


def test_exclusive_success_and_no_instructions_published(tmp_path, monkeypatch):
    dex, digest, tool, tool_digest = inputs(tmp_path)
    raw = fixture_method()
    bind_fixture(monkeypatch, raw)
    monkeypatch.setattr(contract.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=raw, stderr=b""))
    output = tmp_path / "audit"
    report = contract.audit(dex, digest, tool, tool_digest, output)
    assert report["inputs_unchanged"] is True
    assert "return-void" not in (output / "report.json").read_text()
    assert (output.stat().st_mode & 0o777) == 0o700
    with pytest.raises(FileExistsError):
        contract.audit(dex, digest, tool, tool_digest, output)
