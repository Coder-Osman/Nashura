from pathlib import Path
import subprocess


def test_real_observer_with_synthetic_concurrent_callbacks():
    result = subprocess.run(['node', str(Path(__file__).with_name('e0_observer_harness.js'))],
                            text=True, capture_output=True, check=True)
    assert 'engineering fixture passed' in result.stdout
