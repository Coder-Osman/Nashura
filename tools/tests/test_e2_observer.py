from pathlib import Path
import subprocess


def test_observer_memory_abi_results_and_thread_attribution():
    result = subprocess.run(['node', str(Path(__file__).with_name('e2_observer_harness.js'))],
                            text=True, capture_output=True, check=True)
    assert 'engineering fixture passed' in result.stdout
