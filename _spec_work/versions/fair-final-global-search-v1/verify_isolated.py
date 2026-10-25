import importlib
from pathlib import Path
import runpy
import sys


def main():
    root = Path(__file__).resolve().parent
    project = root.parent / "fair-final-history-v2/project"
    sys.path.insert(0, str(project))
    package = importlib.import_module("native_runner")
    if Path(package.__file__).resolve() != project / "native_runner/__init__.py":
        raise ValueError("fresh process with frozen history-v2 package required")
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / "verify_search.py"), run_name="__main__")


if __name__ == "__main__":
    main()
