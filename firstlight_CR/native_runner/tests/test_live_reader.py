from pathlib import Path
import shutil
import subprocess

import pytest


def test_native_reader_health_timers_and_type_checks():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Frida JavaScript reader")
    tests = Path(__file__).parent
    subprocess.run([node, str(tests / "live_reader_health.cjs"),
                    str(tests.parent / "live/reader.js")], check=True, timeout=20)
