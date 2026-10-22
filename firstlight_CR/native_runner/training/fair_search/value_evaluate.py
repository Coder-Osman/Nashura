import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import torch

from .belief import IncrementalBeliefProvider
from .config import SearchConfig
from .evaluation import paired_confidence
from .evaluate_policy import wait_for_engine
from .inference import LocalInference, action_key
from .model import load_checkpoint
from .native import NativeMatchRuntime, NativeReplayFactory, install_puct
from .search import InformationSetSearch
from .storage import atomic_write
from .value_data import BASE_SHA
from .value_train import policy_digest


def play_game(root, port, rollout_type, candidate, baseline, candidate_sha, *, group, pair, owner, progress):
    seed = (13610000 if group == "search" else 14610000) + pair
    models = {owner: candidate, 1 - owner: baseline}
    identities = {owner: candidate_sha, 1 - owner: BASE_SHA}
    runtime = NativeMatchRuntime(port, models, identities, rollout_type=rollout_type, seed=seed,
                                 match_id=f"value-eval-{group}-{pair}-{owner}", transport="compact-delta")
    providers = {}
    started = time.monotonic()
    config = SearchConfig(particles=1)
    roots = 0
    counts = {0: 0, 1: 0}
    try:
        searches = {}
        for player in ((0, 1) if group == "search" else (owner,)):
            belief_port = port + 1 if group == "direct" else port + 1 + player
            factory = NativeReplayFactory([belief_port], models, identities, rollout_type,
                                          search_only=True, transport="compact-delta", conditional_draws=True)
            providers[player] = IncrementalBeliefProvider(factory, attempts=32)
            searches[player] = InformationSetSearch(providers[player], LocalInference(models[player], identities[player],
                                                    config=config), config=config)
        with torch.inference_mode():
            while not runtime.ended:
                if time.monotonic() - started > 43200:
                    raise TimeoutError("search evaluation deadline")
                frozen = runtime.freeze_due_information()
                if not frozen:
                    runtime.advance()
                    continue
                selected = {}
                for player, information in sorted(frozen.items()):
                    if player in searches:
                        payload = f"value-head-ab.v1:{information.sha256}:{counts[player]}"
                        search_seed = int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")
                        result = searches[player].run(information, seed=search_seed, training=False,
                                                       deadline=started + 43200)
                        if sum(result.visits.values()) != 32:
                            raise ValueError("incomplete evaluation root")
                        selected[player] = result.selected
                        roots += 1
                    else:
                        context, _ = baseline.encode(information.frame)
                        device = next(baseline.parameters()).device
                        masks = {name: mask.to(device) for name, mask in information.masks.items()}
                        selected[player] = action_key(baseline.policy.select_action(
                            context, **masks, sample=False, validate=True).action)
                    counts[player] += 1
                runtime.commit_simultaneous(selected, frozen)
                progress(dict(group=group, pair=pair, candidate_owner=owner, roots=roots,
                              tick=max(info.tick for info in frozen.values()), elapsed=time.monotonic() - started))
        terminal = runtime.terminal_evidence()
        if not terminal["ended"] or terminal["truncated"] or terminal["rejected"] or terminal["unconfirmed"]:
            raise ValueError("invalid evaluation terminal")
        return dict(group=group, pair=pair, candidate_owner=owner, seed=seed,
                    score=.5 if terminal["winner"] is None else float(terminal["winner"] == owner),
                    terminal=terminal, roots=roots, seconds=time.monotonic() - started)
    finally:
        try:
            for provider in providers.values():
                provider.close()
        finally:
            runtime.close()


def worker(args):
    torch.set_num_threads(1)
    root = args.root
    report = json.loads((root / "training/done.json").read_text())
    if not report["acceptance"]["passed"]:
        raise ValueError("offline acceptance not passed")
    candidate_sha = report["candidate_sha256"]
    device = args.device or f"cuda:{args.slot}"
    candidate, _, _ = load_checkpoint(root / "training/candidate.pt", device=device, expected_sha256=candidate_sha)
    baseline, _, _ = load_checkpoint(root / "initial.pt", device=device, expected_sha256=BASE_SHA)
    for model in (candidate, baseline):
        model.eval().requires_grad_(False)
    if policy_digest(candidate) != policy_digest(baseline):
        raise ValueError("value comparison changed the policy")
    output = args.output or root / "value-evaluation" / f"slot-{args.slot}"
    output.mkdir(parents=True, exist_ok=True)
    groups = ("search", "direct") if args.group == "both" else (args.group,)
    start_pair = args.slot * 16 if args.start_pair is None else args.start_pair
    pairs = args.pairs
    candidate_owner = getattr(args, "candidate_owner", None)
    owners = (0, 1) if candidate_owner is None else (candidate_owner,)
    if start_pair < 0 or pairs < 1 or start_pair + pairs > 64:
        raise ValueError("evaluation pairs outside frozen 64-pair range")
    identity = dict(candidate_sha256=candidate_sha, baseline_sha256=BASE_SHA,
                    config_sha256=SearchConfig(particles=1).sha256, slot=args.slot,
                    groups=groups, start_pair=start_pair, pairs=pairs,
                    owners=list(owners),
                    code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    identity["groups"] = list(groups)
    identity_path = output / "identity.json"
    if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
        raise ValueError("evaluation resume identity changed")
    atomic_write(identity_path, json.dumps(identity).encode(), replace=True)
    completed = set()
    matches = output / "matches.jsonl"
    if matches.exists():
        for line in matches.read_text().splitlines():
            row = json.loads(line)
            key = row["group"], row["pair"], row["candidate_owner"]
            if (key in completed or row["group"] not in groups
                    or not start_pair <= row["pair"] < start_pair + pairs
                    or row["candidate_owner"] not in owners):
                raise ValueError("duplicate or out-of-shard evaluation result")
            completed.add(key)
    rollout_type = install_puct(root / "project/research/puct-optimized")
    port = args.port or 29400 + 4 * args.slot
    engines = []

    def stop(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with (output / "engine.log").open("a") as log:
            for offset in range(2 if groups == ("direct",) else 3):
                engine_root = getattr(args, "engine_root", None) or output
                instance = engine_root / f"slot-{args.slot}-engine-{offset}-{time.time_ns()}"
                process = subprocess.Popen([sys.executable, str(root / "isolated_engine.py"), "--project",
                                            str(root / "project"), "--port", str(port + offset),
                                            "--instance", str(instance)], stdout=log, stderr=subprocess.STDOUT)
                engines.append(process)
                wait_for_engine(process, instance)
            with matches.open("a", buffering=1) as stream:
                for group in groups:
                    for pair in range(start_pair, start_pair + pairs):
                        for owner in owners:
                            if (group, pair, owner) in completed:
                                continue
                            row = play_game(root, port, rollout_type, candidate, baseline, candidate_sha,
                                            group=group, pair=pair, owner=owner, progress=lambda value:
                                            atomic_write(output / "progress.json", json.dumps(value).encode(), replace=True))
                            row.update(candidate_sha256=candidate_sha, baseline_sha256=BASE_SHA,
                                       config_sha256=SearchConfig(particles=1).sha256,
                                       code_sha256=identity["code_sha256"])
                            stream.write(json.dumps(row) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
            atomic_write(output / "done.json", json.dumps(dict(games=len(groups) * pairs * len(owners),
                         identity=identity, time=time.time())).encode(), replace=True)
    finally:
        for process in engines:
            process.terminate()
        for process in engines:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--slot", type=int)
    parser.add_argument("--group", choices=("both", "search", "direct"), default="both")
    parser.add_argument("--start-pair", type=int)
    parser.add_argument("--pairs", type=int, default=16)
    parser.add_argument("--device")
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--candidate-owner", type=int, choices=(0, 1))
    args = parser.parse_args(argv)
    if args.slot is not None:
        worker(args)
        return
    output = args.root / "value-evaluation"
    output.mkdir(exist_ok=True)
    processes = []
    for slot in range(4):
        with (output / f"worker-{slot}.log").open("a") as log:
            process = subprocess.Popen([sys.executable, "-u", "-m", __package__ + ".value_evaluate",
                                        "--root", str(args.root), "--slot", str(slot)],
                                       stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
    codes = [process.wait() for process in processes]
    results = {}
    for path in output.glob("slot-*/matches.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            key = row["group"], row["pair"], row["candidate_owner"]
            if key in results:
                raise ValueError("duplicate paired result")
            results[key] = row["score"]
    reports = {}
    for group in ("search", "direct"):
        pairs = [[results[group, pair, owner] for owner in (0, 1)] for pair in range(64)
                 if all((group, pair, owner) in results for owner in (0, 1))]
        reports[group] = paired_confidence(pairs) if pairs else dict(games=0)
    atomic_write(output / "summary.json", json.dumps(dict(groups=reports, exit_codes=codes,
                 complete=len(results) == 256 and not any(codes), promoted=False)).encode(), replace=True)
    if any(codes):
        raise RuntimeError("search evaluation has failed workers; preserved partial evidence")


if __name__ == "__main__":
    main()
