import argparse
import json
from pathlib import Path
import random

import torch

from .config import PARENT_SHA256, SearchConfig
from .learner import SearchLearner
from .model import bootstrap, load_checkpoint, save_checkpoint
from .replay import ReplayPool


def initialize_checkpoint(parent_path, destination, *, config=None, device="cpu",
                          expected_parent_sha256=PARENT_SHA256):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("initial search checkpoint already exists")
    config = config or SearchConfig()
    model, parent_sha256 = bootstrap(parent_path, device=device, expected_sha256=expected_parent_sha256)
    replay = ReplayPool(max_generations=config.replay_generations, max_rows=config.replay_rows)
    learner = SearchLearner(model, replay, config=config)
    identity = save_checkpoint(destination, model, learner.optimizer, generation=0,
                               parent_sha256=parent_sha256, config_sha256=config.sha256,
                               replay_state=replay.state_dict(), sample_credit=0, optimizer_steps=0)
    return model, identity


def load_selfplay_checkpoint(path, *, initialization_path, config=None, device="cpu",
                            expected_parent_sha256=PARENT_SHA256):
    config = config or SearchConfig()
    payload = torch.load(path, map_location="cpu", weights_only=True)
    is_search = isinstance(payload, dict) and "schema" in payload
    del payload
    if is_search:
        model, state, identity = load_checkpoint(path, device=device)
        if state["config_sha256"] != config.sha256:
            raise ValueError("self-play checkpoint search configuration differs")
        return model, identity
    return initialize_checkpoint(path, initialization_path, config=config, device=device,
                                 expected_parent_sha256=expected_parent_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Initialize a FAIR WDL checkpoint from legacy value weights.")
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-parent-sha256", default=PARENT_SHA256)
    parser.add_argument("--particles", type=int, default=8)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=1755)
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    config = SearchConfig(particles=args.particles)
    model, identity = initialize_checkpoint(args.parent, args.output, config=config, device=args.device,
                                            expected_parent_sha256=args.expected_parent_sha256)
    print(json.dumps(dict(checkpoint_path=str(args.output), checkpoint_sha256=identity,
                          parent_sha256=args.expected_parent_sha256, config_sha256=config.sha256,
                          value_head=model.value_head_architecture, calibrated=False), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
