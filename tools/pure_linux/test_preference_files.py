import json
from pathlib import Path
import shutil
import subprocess

import pytest

from preference_files_job import CASES


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    directory = tmp_path_factory.mktemp("preference-files-build")
    source = Path(__file__).parent
    executable = directory / "probe"
    subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", "-pthread",
                    str(source / "preference_files.c"), str(source / "preference_files_probe.c"),
                    "-o", str(executable)], check=True)
    return executable


def test_real_files_faults_exec_and_crash_recovery(probe, tmp_path):
    result = subprocess.run([str(probe), str(tmp_path / "cases"), "-"],
                            capture_output=True, timeout=30, check=True)
    summary = json.loads(result.stdout)
    assert summary["status"] == "PASS"
    assert summary["cases"] == list(CASES)
    assert summary["case_count"] == len(CASES)
    assert summary["assertions"] >= 300
    assert summary["libg_loaded"] is False
    assert summary["scope"] == "synthetic_file_commit_and_process_crash_only"
    assert "assertion_failed" not in result.stderr.decode()
    assert (tmp_path / "cases/failure-5/store-0.xml").read_bytes().endswith(b"old</string></map>")
    assert (tmp_path / "cases/failure-3/store-0.xml").read_bytes().endswith(b"new</string></map>")


def test_existing_experiment_directory_is_not_reused(probe, tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"preserve-existing")
    result = subprocess.run([str(probe), str(tmp_path), "-"], capture_output=True, timeout=5)
    assert result.returncode != 0
    assert sentinel.read_bytes() == b"preserve-existing"
    assert list(tmp_path.iterdir()) == [sentinel]
