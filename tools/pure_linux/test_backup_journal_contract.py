import json

import pytest

import backup_journal_contract as contract
from platform_preference_audit import selected_methods
from preference_method_audit import method_evidence, sha256


def fixture_dump():
    return "".join("#0\naccess : PUBLIC\nregisters : 3\nins : 3\nouts : 0\n"
                   "insns size : 1 16-bit code units\n0000: |[0000] " + name +
                   "\n0000: 00 |0000: return-void\ncatches : (none)\npositions     :\n"
                   for name in contract.REVIEWED).encode()


def bind(monkeypatch, dump):
    methods = tuple(contract.REVIEWED)
    evidence = method_evidence(selected_methods(dump, methods), methods, b"dex\n")
    monkeypatch.setattr(contract, "REVIEWED", {name: evidence[name]["sha256"] for name in methods})


def test_real_pin_rejects_fixture():
    with pytest.raises(ValueError, match="manually reviewed"):
        contract.review(fixture_dump(), b"dex\n")


def test_binding_preserves_static_and_unresolved_boundaries(monkeypatch):
    dump = fixture_dump()
    bind(monkeypatch, dump)
    result = contract.review(dump, b"dex\n")
    assert len(result["methods"]) == 6
    assert result["status"] == "STATIC_FOUND"
    assert result["automatic_semantic_inference"] is False
    assert result["runtime_parity"] == "NOT_RUN"
    assert not result["catalog_ready_pass"]
    assert not result["journal"]["normal_return_proves_durability"]
    assert not result["scheduler"]["normal_return_proves_acceptance_or_delivery"]
    assert "return-void" not in json.dumps(result)


@pytest.mark.parametrize("damage", ["instruction", "metadata", "catch", "missing", "duplicate"])
def test_partial_or_changed_review_rejected(monkeypatch, damage):
    dump = fixture_dump()
    bind(monkeypatch, dump)
    broken = {
        "instruction": dump.replace(b"return-void", b"nop", 1),
        "metadata": dump.replace(b"registers : 3", b"registers : 4", 1),
        "catch": dump.replace(b"catches : (none)", b"catches : 0", 1),
        "missing": dump.replace(next(iter(contract.REVIEWED)).encode(), b"other", 1),
        "duplicate": dump + dump,
    }[damage]
    with pytest.raises(ValueError):
        contract.review(broken, b"dex\n")


@pytest.mark.parametrize("damage", [None, "dump_sha", "dex_sha", "magic", "method"])
def test_audit_identity_and_exclusive_output(tmp_path, monkeypatch, damage):
    dump, dex, output = tmp_path / "dump", tmp_path / "dex", tmp_path / "report"
    raw = fixture_dump()
    bind(monkeypatch, raw)
    dump.write_bytes(raw.replace(b"return-void", b"nop", 1) if damage == "method" else raw)
    dex.write_bytes(b"not dex" if damage == "magic" else b"dex\nfixture")
    args = (dump, "wrong" if damage == "dump_sha" else sha256(dump.read_bytes()),
            dex, "wrong" if damage == "dex_sha" else sha256(dex.read_bytes()), output)
    if damage:
        with pytest.raises(ValueError):
            contract.audit(*args)
        assert not output.exists()
    else:
        result = contract.audit(*args)
        assert result["inputs_unchanged"] is True
        assert json.loads(output.read_text()) == result
        assert output.stat().st_mode & 0o777 == 0o600
        original = output.read_bytes()
        with pytest.raises(FileExistsError):
            contract.audit(*args)
        assert output.read_bytes() == original


@pytest.mark.parametrize("changed", ["dump", "dex"])
def test_inputs_changed_during_review_rejected(tmp_path, monkeypatch, changed):
    dump, dex, output = tmp_path / "dump", tmp_path / "dex", tmp_path / "report"
    dump.write_bytes(fixture_dump())
    dex.write_bytes(b"dex\nfixture")
    bind(monkeypatch, dump.read_bytes())
    original = contract.review

    def review(raw, source):
        result = original(raw, source)
        (dump if changed == "dump" else dex).write_bytes(b"changed")
        return result

    monkeypatch.setattr(contract, "review", review)
    with pytest.raises(ValueError, match="input changed"):
        contract.audit(dump, sha256(dump.read_bytes()), dex, sha256(dex.read_bytes()), output)
    assert not output.exists()
