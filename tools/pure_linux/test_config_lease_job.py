import argparse
import json

import pytest

import config_lease_job as job


def arguments(tmp_path):
    probe = tmp_path / "probe"
    probe.write_bytes(b"synthetic candidate identity")
    linker = tmp_path / "linker"
    linker.write_text("#!/bin/sh\nprintf 'fixture failure\\n'\nexit 4\n")
    linker.chmod(0o700)
    libraries = tmp_path / "libraries"
    libraries.mkdir()
    return argparse.Namespace(probe=probe, linker=linker, library_path=libraries,
                              probe_sha256=job.digest(probe), linker_sha256=job.digest(linker),
                              output=tmp_path / "exclusive-output")


def test_platform_guard_precedes_output_and_execution(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(job.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="ARM64 Linux"):
        job.run(args)
    assert not args.output.exists()


def test_identity_guard_precedes_output(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(job.platform, "system", lambda: "Linux")
    monkeypatch.setattr(job.platform, "machine", lambda: "aarch64")
    args.probe_sha256 = "0" * 64
    with pytest.raises(ValueError, match="identity mismatch"):
        job.run(args)
    assert not args.output.exists()


def test_failure_artifacts_survive_and_output_is_exclusive(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(job.platform, "system", lambda: "Linux")
    monkeypatch.setattr(job.platform, "machine", lambda: "aarch64")
    assert job.run(args) == 1
    report = json.loads((args.output / "result.json").read_text())
    assert report["rows"][0]["exit_code"] == 4
    assert not report["rows"][0]["passed"]
    assert report["failure"]
    assert report["inputs_unchanged"]
    assert report["android_parity"] == "NOT_RUN"
    assert not report["catalog_ready_pass"]
    assert (args.output / "fresh.stdout").read_text() == "fixture failure\n"
    before = (args.output / "result.json").read_bytes()
    with pytest.raises(FileExistsError):
        job.run(args)
    assert (args.output / "result.json").read_bytes() == before
