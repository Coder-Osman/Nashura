import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "_spec_work/versions/fair-final-history-v2/project"
TEST_NAMES = (
    "protocol", "information", "search", "pending_search", "recovery", "persistence",
    "history_provider", "local_search_provider", "refill_provider",
)


def main():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    tests = [str(PROJECT / "native_runner/tests" / ("test_fair_final_" + name + ".py"))
        for name in TEST_NAMES]
    tests.append(str(ROOT / "_spec_work/versions/fair-final-general-fallback-v1/test_general_provider.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-general-fallback-v1/test_native_general.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-native-mode-audit-v1/test_mode.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-native-execution-flags-v1/test_flags.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-native-seed-audit-v1/test_seed.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-native-shuffle-audit-v1/test_shuffle.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-native-rng-alias-audit-v1/test_alias.py"))
    tests.extend(str(ROOT / "_spec_work/versions/fair-final-native-prior-sampling-v1" / name)
        for name in ("test_openings.py", "test_verification.py"))
    tests.append(str(ROOT / "_spec_work/versions/fair-final-active-migration-v1/test_selection_remote.py"))
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests,
        "--junitxml=" + str(ROOT / "publication/checks-public.xml")],
        cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
