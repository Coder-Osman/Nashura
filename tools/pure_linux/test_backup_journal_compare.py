import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import backup_journal_compare as comparison


RECORDS = ["00146e756c6c73726f79616c652e72656c2e66726565",
           "00146e756c6c73726f79616c652e72656c2e66726565", "0000",
           "000a7a65726fc0807461696c", "0006e4b8ade69687", "0006eda0bdedb880",
           "0003eda080", "0003edbfbf"]


def framework_rows(phase):
    identity = {"case": "identity", "class": "com.android.server.backup.DataChangedJournal",
                "pid": 123, "tid": 123}
    content = b""
    rows = [identity]
    for index, encoded in enumerate(RECORDS):
        content += bytes.fromhex(encoded)
        rows.append({"case": "append", "index": index, "hex": content.hex(), "length": len(content)})
    if phase == "reopen":
        content += bytes.fromhex("000872656f70656e6564")
        return [identity, {"case": "reopened", "records": 9, "hex": content.hex(),
                           "sha256": hashlib.sha256(content).hexdigest()}]
    rows.extend({"case": case, "exception": exception} for case, exception in (
        ("oversize", "java.io.UTFDataFormatException"), ("missing_parent", "java.io.IOException"),
        ("file_as_parent", "java.io.IOException"), ("directory_as_journal", "java.io.FileNotFoundException")))
    rows.append({"case": "prepared", "records": 8})
    return rows


def log_file(tmp_path, rows):
    path = tmp_path / "framework.log"
    path.write_text("\n".join("JOURNAL_EVENT\t" + json.dumps(row) for row in rows) + "\n")
    return path


@pytest.mark.parametrize("phase", ["prepare", "reopen"])
def test_complete_fixture_log(tmp_path, phase):
    result = comparison.observed(log_file(tmp_path, framework_rows(phase)), phase)
    assert len(result) == (8 if phase == "prepare" else 94)


@pytest.mark.parametrize("fault", ["class", "pid", "duplicate", "partial", "length", "index", "exceptions"])
def test_reject_incomplete_fixture_log(tmp_path, fault):
    rows = copy.deepcopy(framework_rows("prepare"))
    if fault == "class":
        rows[0]["class"] = "synthetic.Other"
    elif fault == "pid":
        rows[0]["pid"] = 0
    elif fault == "duplicate":
        rows.insert(-1, rows[-2])
    elif fault == "partial":
        rows.pop()
    elif fault == "length":
        rows[1]["length"] += 1
    elif fault == "index":
        rows[2]["index"] = 0
    else:
        rows[-2]["exception"] = "other"
    with pytest.raises(ValueError):
        comparison.observed(log_file(tmp_path, rows), "prepare")


def test_reopen_digest_and_phase_rejection(tmp_path):
    rows = framework_rows("reopen")
    rows[-1]["sha256"] = "0" * 64
    path = log_file(tmp_path, rows)
    with pytest.raises(ValueError, match="digest"):
        comparison.observed(path, "reopen")
    with pytest.raises(ValueError, match="phase"):
        comparison.observed(path, "other")


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    root = Path(__file__).parent
    output = tmp_path_factory.mktemp("journal-cli") / "probe"
    subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", str(root / "backup_journal.c"),
                    str(root / "backup_journal_probe.c"), "-o", str(output)], check=True)
    return output


def test_cli_prepare_and_separate_reopen(probe, tmp_path):
    directory = tmp_path / "store"
    reference = comparison.observed(log_file(tmp_path, framework_rows("prepare")), "prepare")
    assert comparison.compare(None, reference, directory, "prepare", probe)["journal_bytes"] == 84
    reference = comparison.observed(log_file(tmp_path, framework_rows("reopen")), "reopen")
    assert comparison.compare(None, reference, directory, "reopen", probe)["journal_bytes"] == 94


@pytest.mark.parametrize("payload", ["z000", "000", "-001", "+001", "0000g000"])
def test_cli_reject_invalid_units(probe, tmp_path, payload):
    result = subprocess.run([str(probe), str(tmp_path / "never"), payload], capture_output=True)
    assert result.returncode == 2
    assert not (tmp_path / "never").exists()


def test_existing_store_is_not_consumed(probe, tmp_path):
    reference = comparison.observed(log_file(tmp_path, framework_rows("prepare")), "prepare")
    with pytest.raises(FileExistsError):
        comparison.compare(None, reference, tmp_path, "prepare", probe)


@pytest.mark.parametrize("fault", ["exit", "stderr", "schema"])
def test_subprocess_rejection_and_absolute_paths(tmp_path, monkeypatch, fault):
    def run(command, **kwargs):
        assert Path(command[0]).is_absolute()
        assert Path(command[1]).is_absolute()
        return SimpleNamespace(returncode=1 if fault == "exit" else 0,
                               stdout=b'{}' if fault == "schema" else b'{"result":0,"errno":0}',
                               stderr=b"loader failed" if fault in {"exit", "stderr"} else b"")
    monkeypatch.setattr(comparison.subprocess, "run", run)
    with pytest.raises((RuntimeError, ValueError)):
        comparison.compare(None, [b"x"], tmp_path / "store", "prepare", Path("probe"), Path("linker"))


@pytest.mark.parametrize("fault", ["environment", "input_changed", "comparison"])
def test_main_failure_is_archived(tmp_path, monkeypatch, fault):
    source = tmp_path / "implementation"
    source.write_bytes(b"fixture")
    output = tmp_path / "result.json"
    log = log_file(tmp_path, framework_rows("prepare"))
    monkeypatch.setattr(sys, "argv", ["compare", "--probe", str(source), "--android-log", str(log),
                        "--directory", str(tmp_path / "store"), "--output", str(output), "--phase", "prepare"])
    monkeypatch.setattr(comparison.platform, "system", lambda: "Darwin" if fault == "environment" else "Linux")
    monkeypatch.setattr(comparison.platform, "machine", lambda: "aarch64")

    def compare(*args):
        if fault == "comparison":
            raise ValueError("deliberate comparison failure")
        source.write_bytes(b"changed")
        return {"checks": []}

    monkeypatch.setattr(comparison, "compare", compare)
    with pytest.raises(ValueError):
        comparison.main()
    record = json.loads(output.read_text())
    assert record["status"] == "BLOCKED"
    assert record["catalog_ready_pass"] is False
    assert record["backup_binding_pass"] is False
    assert record["error"]
    assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        comparison.main()
    assert output.read_bytes() == before
