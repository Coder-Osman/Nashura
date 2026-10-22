import argparse
import json
import math
from pathlib import Path
import random
import statistics
import subprocess
import sys

import torch

from .config import SearchConfig
from .initialize import load_selfplay_checkpoint
from .storage import atomic_write


def summarize(records):
    if not records or len(records) % 2:
        raise ValueError("benchmark requires complete alternating pairs")
    reference = records[0]
    stable = ("root_sha256", "checkpoint_sha256", "config_sha256", "runtime_sha256",
              "visits", "selected", "particles", "trace_sha256")
    for index, record in enumerate(records):
        if any(record[field] != reference[field] for field in stable):
            raise ValueError("optimization changed search identity, visits, action or branch information")
        if sum(record["visits"].values()) != 32 or not record["trace_sha256"]:
            raise ValueError("benchmark did not complete an audited 32-simulation root")
        if record["search_only"] != bool(index % 2):
            raise ValueError("benchmark trials are not alternating baseline and optimized runs")
        if not math.isfinite(record["seconds"]) or record["seconds"] <= 0:
            raise ValueError("invalid benchmark duration")
    baseline = [record["seconds"] for record in records[::2]]
    optimized = [record["seconds"] for record in records[1::2]]
    return dict(schema="fair-root-benchmark.v1", pairs=len(baseline), simulations=32,
                root_sha256=reference["root_sha256"], checkpoint_sha256=reference["checkpoint_sha256"],
                config_sha256=reference["config_sha256"], runtime_sha256=reference["runtime_sha256"],
                baseline_seconds=baseline, optimized_seconds=optimized,
                baseline_median=statistics.median(baseline), optimized_median=statistics.median(optimized),
                root_speedup=statistics.median(baseline) / statistics.median(optimized),
                branch_information_equal=True, visits_equal=True, selected_equal=True,
                full_episode_throughput_measured=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Alternate audited FAIR root baseline and search-only trials.")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--replay-root", type=Path, required=True)
    parser.add_argument("--main-port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1755)
    parser.add_argument("--timeout-seconds", type=float, default=600.)
    args = parser.parse_args(argv)
    if args.pairs < 1 or args.timeout_seconds <= 0:
        raise ValueError("benchmark pairs and timeout must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    initialization_path = args.output / "initial-checkpoint.pt"
    if (any(args.output.glob("trial-*.json")) or (args.output / "summary.json").exists()
            or initialization_path.exists()):
        raise ValueError("benchmark output already contains trials; use a fresh directory")
    torch.set_num_threads(1)
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    model, _ = load_selfplay_checkpoint(args.checkpoint, initialization_path=initialization_path,
                                       config=SearchConfig(particles=1))
    del model
    checkpoint_path = initialization_path if initialization_path.exists() else args.checkpoint
    command = [sys.executable, "-m", "native_runner.training.fair_search.smoke",
               "--checkpoint", str(checkpoint_path), "--puct-root", str(args.puct_root),
               "--replay-root", str(args.replay_root), "--main-port", str(args.main_port),
               "--belief-port", str(args.belief_port), "--output", str(args.output),
               "--particles", "1", "--search-root", "--seed", str(args.seed)]
    records = []
    for index in range(args.pairs * 2):
        optimized = bool(index % 2)
        completed = subprocess.run(command + (["--search-only"] if optimized else []),
                                   capture_output=True, text=True, timeout=args.timeout_seconds, check=True)
        record = json.loads(completed.stdout)
        records.append(record)
        atomic_write(args.output / f"trial-{index:02d}.json", json.dumps(record, sort_keys=True).encode())
        if optimized:
            summarize(records)
        print(json.dumps(dict(trial=index, search_only=optimized, seconds=record["seconds"])), flush=True)
    summary = summarize(records)
    atomic_write(args.output / "summary.json", json.dumps(summary, sort_keys=True).encode())
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
