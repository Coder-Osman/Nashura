from pathlib import Path
import subprocess


def test_concurrent_rejection_ownership_timeline_and_coverage_gaps():
    result = subprocess.run(['node', str(Path(__file__).with_name('e2_1n_diagnostics_harness.js'))],
                            capture_output=True, text=True, check=True)
    assert 'no native concurrency equivalence claimed' in result.stdout
