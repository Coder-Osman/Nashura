from pathlib import Path
import subprocess


def test_cold_install_order_reentrancy_and_failure_remain_explicit():
    result = subprocess.run(['node', str(Path(__file__).with_name('e2_1_cold_observer_harness.js'))],
                            capture_output=True, text=True, check=True)
    assert 'engineering fixture passed; not native evidence' in result.stdout
