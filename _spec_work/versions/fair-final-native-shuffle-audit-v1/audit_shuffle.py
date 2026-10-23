import argparse
import json
from pathlib import Path

from shuffle_audit import build_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = build_report(arguments.library)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({name: report[name] for name in ("status", "libg_sha256", "gates_closed")}))


if __name__ == "__main__":
    main()
