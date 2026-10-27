from pathlib import Path
import subprocess


def test_native_backend_route_failures_and_retirement():
    result = subprocess.run(['node', str(Path(__file__).with_name('e2_1k_native_backend_harness.js'))],
                            capture_output=True, text=True, check=True)
    assert 'engineering fixture passed; not native evidence' in result.stdout
