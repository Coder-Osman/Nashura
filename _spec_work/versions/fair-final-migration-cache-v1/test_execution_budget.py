import argparse
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from execution_budget import phase_timeout, training_timeout


@pytest.mark.parametrize("parse,maximum", [(training_timeout, 21600), (phase_timeout, 43200)])
def test_budget_accepts_bounds_and_rejects_invalid_values(parse, maximum):
    assert parse("1") == 1
    assert parse(str(maximum)) == maximum
    for value in (0, -1, maximum + 1, True, "1.5", "unbounded"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse(value)


@pytest.mark.parametrize("entry,option,invalid", [
    ("start_cached.py", "--timeout-seconds", "0"),
    ("start_cached.py", "--timeout-seconds", "21601"),
    ("start_cached.py", "--timeout-seconds", "1.5"),
    ("start_resume.py", "--phase-timeout-seconds", "0"),
    ("start_resume.py", "--phase-timeout-seconds", "43201"),
    ("start_resume.py", "--phase-timeout-seconds", "1.5"),
])
def test_invalid_cli_budget_fails_before_parent_read_or_ssh(tmp_path, entry, option, invalid):
    result = subprocess.run([sys.executable, str(ROOT / entry), str(tmp_path / "missing-parent"),
        option, invalid], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "timeout" in result.stderr
    assert "Traceback" not in result.stderr
    assert list(tmp_path.iterdir()) == []
