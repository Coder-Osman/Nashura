import argparse
from pathlib import Path

from prepare_collection import encoded, verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    result = verify(**vars(parser.parse_args()))
    print(encoded(result).decode(), flush=True)


if __name__ == "__main__":
    main()
