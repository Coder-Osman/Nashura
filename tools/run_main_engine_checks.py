"""Independent active firstlight_CR checks; frozen history-v2 remains a separate CI job."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "firstlight_CR"
TESTS = (
    "test_sspi_tower_shared.py",
    "test_sspi_tower_bootstrap.py",
    "test_sspi_random60.py",
    "test_sspi_binary_outcome.py",
    "test_sspi_value_only.py",
    "test_sspi_value_calibration.py",
    "test_sspi_mac_training.py",
    "test_sspi_temporal_augmentation.py",
    "test_sspi_search_performance.py",
    "test_sspi_json_delta.py",
    "test_sspi_event_history.py",
    "test_fair_final_protocol.py",
    "test_fair_final_learning.py",
    "test_fair_final_search.py",
    "test_fair_final_pending_search.py",
    "test_fair_final_runtime.py",
    "test_fair_final_visibility.py",
    "test_fair_migration.py",
    "test_fair_final_inheritance.py",
)


def main():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    paths = [str(PROJECT / "native_runner/tests" / name) for name in TESTS]
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
        cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
