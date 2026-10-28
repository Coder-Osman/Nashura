import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["nested", "exception", "interleaved", "asynchronous",
                                    "disk_exception", "read_scope", "association_limit",
                                    "observer_failure"])
def test_actual_hook_wrappers_with_mock_java_runtime(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for observer control-flow fixture")
    root = Path(__file__).parent
    completed = subprocess.run(
        [node, str(root / "backup_observer_scope_fixture.cjs"),
         str(root / "backup_runtime_observer.js"), scenario],
        text=True, capture_output=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "MOCK_SCOPE_PASS " + scenario + "\n"
