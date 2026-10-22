"""Guard the immutable host probe binding used by task acceptances."""
from __future__ import annotations

import hashlib

import pytest

import runtime_contract


def test_install_binds_verified_probe(monkeypatch, tmp_path):
    probe = tmp_path / "probe.so"
    probe.write_bytes(b"verified probe")
    monkeypatch.setattr(runtime_contract, "PROBE", probe)
    monkeypatch.setattr(runtime_contract, "PROBE_SHA256", hashlib.sha256(probe.read_bytes()).hexdigest())
    monkeypatch.delenv("CR_PROBE", raising=False)

    assert runtime_contract.install() == probe
    assert runtime_contract.os.environ["CR_PROBE"] == str(probe)

    monkeypatch.setenv("CR_PROBE", str(tmp_path / "other.so"))
    with pytest.raises(RuntimeError, match="conflicts"):
        runtime_contract.install()

    monkeypatch.delenv("CR_PROBE", raising=False)
    monkeypatch.setattr(runtime_contract, "PROBE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="identity changed"):
        runtime_contract.install()


def test_experimental_probe_requires_explicit_opt_in_and_exact_hash(monkeypatch, tmp_path):
    probe = tmp_path / "libcrprobe.so"
    probe.write_bytes(b"experimental resident")
    monkeypatch.setattr(runtime_contract, "EXPERIMENTAL_PROBE", probe)
    monkeypatch.setattr(
        runtime_contract, "EXPERIMENTAL_PROBE_SHA256",
        hashlib.sha256(probe.read_bytes()).hexdigest(),
    )
    monkeypatch.delenv("CR_PROBE", raising=False)
    monkeypatch.setenv("PUCT_EXPERIMENTAL_PROBE", "1")

    assert runtime_contract.install() == probe
    assert runtime_contract.os.environ["CR_PROBE"] == str(probe)

    monkeypatch.setattr(runtime_contract, "EXPERIMENTAL_PROBE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="identity changed"):
        runtime_contract.install()

    monkeypatch.setenv("PUCT_EXPERIMENTAL_PROBE", "invalid")
    with pytest.raises(RuntimeError, match="must be 0 or 1"):
        runtime_contract.install()
