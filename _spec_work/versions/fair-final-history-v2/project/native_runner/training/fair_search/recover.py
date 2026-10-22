import argparse
from pathlib import Path

from .storage import publish_terminal_episode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish a retained complete FAIR episode without running a match.")
    parser.add_argument("--terminal-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(publish_terminal_episode(args.terminal_file, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
