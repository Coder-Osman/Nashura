"""Create a separate skill-event checkpoint; never replace the source file."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.training.event_checkpoint import load_event_checkpoint, save_event_checkpoint
from native_runner.training.event_ability_migration import migrate_ability_checkpoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.source.resolve() == args.output.resolve():
        parser.error("output must be a new, separate checkpoint path")
    loaded = load_event_checkpoint(args.source)
    model, extra = migrate_ability_checkpoint(loaded)
    digest = save_event_checkpoint(args.output, model, extra=extra)
    verified = load_event_checkpoint(args.output, initialization_only=True)
    if verified.checkpoint_sha256 != digest:
        raise ValueError("written ability checkpoint SHA does not match the migration result")
    print(json.dumps(dict(path=str(args.output.resolve()), sha256=digest,
                         readback_verified=True,
                         **extra["architecture_migration"]), indent=2))


if __name__ == "__main__":
    main()
