import argparse
import json

from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import MatchConfig, tournament_level_kwargs
from .config import HOG_DECK


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    native = NativeClashEnv(port=args.port, timeout=90)
    native._persistent_transport_requested = True
    try:
        records = []
        for seed in (770001, 770002):
            for deck in (HOG_DECK, tuple(reversed(HOG_DECK))):
                observation = native.create_match(MatchConfig(deck0=deck, deck1=HOG_DECK,
                                                              seed=seed, **tournament_level_kwargs()))
                records.append(dict(seed=seed, deck=list(deck), tick=observation["tick"],
                                    players=observation["players"]))
        print(json.dumps(records), flush=True)
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
