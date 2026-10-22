"""Safe local controller for the independent Hog tree-search experiment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def check() -> None:
    subprocess.run([sys.executable, str(ROOT / "plan.py")], check=True)
    subprocess.run([sys.executable, str(ROOT / "validate.py")], check=True)
    print(json.dumps({"task": ROOT.name, "status": "checked", "remote_started": False}, indent=2))


def prototype() -> None:
    """Run the task-owned native acceptance before any remote collection."""
    check()
    acceptance = ROOT / "native_acceptance.py"
    if not acceptance.exists():
        raise RuntimeError("native acceptance runner is missing")
    subprocess.run([sys.executable, str(ROOT / "lifecycle.py"), "status"], check=True)
    subprocess.run([sys.executable, str(ROOT / "native_acceptance.py")], check=True)
    print(json.dumps({"task": ROOT.name, "status": "prototype_native_acceptance_passed",
                      "remote_started": False}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "run-prototype", "start-formal"))
    parser.add_argument("training_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "start-formal":
        if any(argument == "--mode" or argument.startswith("--mode=") for argument in args.training_args):
            parser.error("start-formal always uses formal mode")
        subprocess.run([sys.executable, str(ROOT / "train_with_teacher.py"), "--mode", "formal",
                        *args.training_args], check=True)
    else:
        if args.training_args:
            parser.error("training arguments apply only to start-formal")
        (prototype if args.command == "run-prototype" else check)()


if __name__ == "__main__":
    main()
