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
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests,
        "--junitxml=" + str(ROOT / "publication/checks-public.xml")],
        cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
