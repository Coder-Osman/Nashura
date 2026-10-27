from pathlib import Path
import subprocess


def test_persistent_backend_routes_and_failure_cleanup():
    result = subprocess.run(['node', str(Path(__file__).with_name('e2_1m_native_backend_harness.js'))],
                            capture_output=True, text=True, check=True)
    assert 'Persistent PN backend engineering fixture passed; not native state-equivalence evidence' in result.stdout
