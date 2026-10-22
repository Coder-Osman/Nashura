"""Paired FAIR search versus direct decoding of the same frozen policy."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time
import traceback

import torch

from .belief import IncrementalBeliefProvider
from .config import SearchConfig
from .evaluate_policy import file_sha256, wait_for_engine
from .inference import LocalInference, action_key
from .model import load_checkpoint
from .native import NativeMatchRuntime, NativeReplayFactory, install_puct
from .search import InformationSetSearch
from .storage import atomic_write


def sample_visit_action(visits, *, seed):
    if (not visits or any(type(count) is not int or count < 0 for count in visits.values())
            or sum(visits.values()) != 32):
        raise ValueError("teacher sampling requires 32 nonnegative integer visits")
    actions = sorted(action for action, count in visits.items() if count)
    return random.Random(seed).choices(actions, weights=[visits[action] for action in actions])[0]


def select_frozen_actions(frozen, teacher_owner, search, direct, *, decision_index, deadline,
                          teacher_selection="argmax", root_noise=False, sampling_seed=None):
    if teacher_selection not in ("argmax", "sample-visits"):
        raise ValueError("unknown teacher selection")
    if teacher_selection == "sample-visits" and sampling_seed is None:
        raise ValueError("teacher sampling requires an independent action seed")
    selected = {}
    result = None
    for owner, information in sorted(frozen.items()):
        if owner == teacher_owner:
            payload = f"fair-teacher-eval.v1:{information.sha256}:{decision_index}"
            seed = int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")
            result = search.run(information, seed=seed, training=root_noise, deadline=deadline)
            if sum(result.visits.values()) != 32:
                raise ValueError("teacher root did not complete 32 simulations")
            selected[owner] = (sample_visit_action(result.visits, seed=sampling_seed)
                               if teacher_selection == "sample-visits" else
                               min(result.visits, key=lambda action: (-result.visits[action], action)))
        else:
            selected[owner] = direct(information)
        if not information.legal(selected[owner]):
            raise ValueError("evaluation selected an illegal action")
    return selected, result


def play_game(args, model, identity, config, rollout_type, pair, teacher_owner, attempt):
    started = time.monotonic()
    seed = args.seed_base + pair
    policies = {0: model, 1: model}
    identities = {0: identity, 1: identity}
    runtime = NativeMatchRuntime(args.port, policies, identities, rollout_type=rollout_type,
                                 seed=seed, match_id=f"teacher-{seed}-{teacher_owner}-{attempt}",
                                 transport="compact-delta")
    provider = None
    try:
        factory = NativeReplayFactory(range(args.port + 1, args.port + 1 + config.particles),
                                      policies, identities, rollout_type, search_only=True,
                                      transport="compact-delta", conditional_draws=True)
        provider = IncrementalBeliefProvider(factory, attempts=32)
        inference = LocalInference(model, identity, config=config)
        search = InformationSetSearch(provider, inference, config=config)
        device = next(model.parameters()).device

        def direct(information):
            context, _ = model.encode(information.frame)
            masks = {name: mask.to(device) for name, mask in information.masks.items()}
            return action_key(model.policy.select_action(context, **masks, sample=False,
                                                          validate=True).action)

        roots = decisions = 0
        teacher_selection = getattr(args, "teacher_selection", "argmax")
        root_noise = getattr(args, "root_noise", False)
        search_seconds = 0.
        with (args.output / f"roots-{pair}-{teacher_owner}-{attempt}.jsonl").open("a", buffering=1) as output:
            with torch.inference_mode():
                while not runtime.ended:
                    if time.monotonic() >= started + args.deadline:
                        raise TimeoutError("teacher evaluation exceeded game deadline")
                    frozen = runtime.freeze_due_information()
                    if not frozen:
                        runtime.advance()
                        continue
                    action_seed = int.from_bytes(hashlib.sha256(
                        f"teacher-visit-sampling.v1:{seed}:{teacher_owner}:{roots}".encode()).digest()[:8], "big")
                    selected, result = select_frozen_actions(
                        frozen, teacher_owner, search, direct, decision_index=decisions,
                        deadline=started + args.deadline, teacher_selection=teacher_selection,
                        root_noise=root_noise, sampling_seed=action_seed)
                    runtime.commit_simultaneous(selected, frozen)
                    decisions += len(frozen)
                    if result is not None:
                        roots += 1
                        search_seconds += result.seconds
                        record = dict(pair=pair, teacher_owner=teacher_owner, attempt=attempt,
                                      tick=frozen[teacher_owner].tick, roots=roots,
                                      visits=sum(result.visits.values()), selected=selected[teacher_owner],
                                      visit_counts=result.visits, sampling_seed=action_seed,
                                      teacher_selection=teacher_selection, root_noise=root_noise,
                                      argmax=min(result.visits, key=lambda action: (-result.visits[action], action)),
                                      root_seconds=result.seconds, timings=result.timings,
                                      elapsed=time.monotonic() - started)
                        output.write(json.dumps(record, allow_nan=False) + "\n")
                        atomic_write(args.output / "progress.json", json.dumps(record).encode(), replace=True)
        evidence = runtime.terminal_evidence()
        if (not evidence["ended"] or evidence["truncated"] or evidence["rejected"]
                or evidence["unconfirmed"]):
            raise ValueError("evaluation requires a clean native complete terminal")
        winner = evidence["winner"]
        return dict(pair=pair, seed=seed, teacher_owner=teacher_owner, attempt=attempt,
                    score=.5 if winner is None else float(winner == teacher_owner),
                    winner=winner, terminal_tick=evidence["terminal_tick"], reason=evidence["reason"],
                    terminal=evidence, teacher_selection=teacher_selection, root_noise=root_noise,
                    roots=roots, search_seconds=search_seconds, wall_seconds=time.monotonic() - started)
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--engine-script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--start-pair", type=int, required=True)
    parser.add_argument("--pairs", type=int, required=True)
    parser.add_argument("--seed-base", type=int, default=610000)
    parser.add_argument("--deadline", type=float, default=43200.)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--teacher-selection", choices=("argmax", "sample-visits"), default="argmax")
    parser.add_argument("--root-noise", action="store_true")
    parser.add_argument("--teacher-owner", type=int, choices=(0, 1))
    args = parser.parse_args(argv)
    if args.pairs < 1 or args.start_pair < 0 or args.deadline <= 0:
        raise ValueError("invalid evaluation range or deadline")
    torch.set_num_threads(1)
    manifest = json.loads(args.manifest.read_text())
    config = SearchConfig(**manifest["config"])
    if manifest["checkpoint_sha256"] != args.expected_sha256:
        raise ValueError("teacher differs from the producing checkpoint")
    model, _, identity = load_checkpoint(args.checkpoint, device=args.device,
                                         expected_sha256=args.expected_sha256)
    model.eval().requires_grad_(False)
    args.output.mkdir(parents=True, exist_ok=True)
    contract = dict(schema="fair-teacher-evaluation.v1", checkpoint_sha256=identity,
                    config_sha256=config.sha256, particles=config.particles, simulations=32,
                    teacher="FAIR-no-noise-max-visits", opponent="same-policy-greedy",
                    start_pair=args.start_pair, pairs=args.pairs, seed_base=args.seed_base,
                    code_sha256=file_sha256(Path(__file__)))
    owners = (0, 1) if args.teacher_owner is None else (args.teacher_owner,)
    contract.update(teacher="FAIR-" + ("training-noise-" if args.root_noise else "no-noise-")
                    + args.teacher_selection, teacher_selection=args.teacher_selection,
                    root_noise=args.root_noise, owners=list(owners),
                    sampling_temperature=1. if args.teacher_selection == "sample-visits" else None)
    identity_path = args.output / "identity.json"
    if identity_path.exists():
        if json.loads(identity_path.read_text()) != contract:
            raise ValueError("evaluation identity changed")
    else:
        atomic_write(identity_path, json.dumps(contract, sort_keys=True).encode())
    matches = args.output / "matches.jsonl"
    completed = set()
    if matches.exists():
        for line in matches.read_text().splitlines():
            row = json.loads(line)
            key = row["pair"], row["teacher_owner"]
            if (key in completed or row["seed"] != args.seed_base + row["pair"]
                    or not args.start_pair <= row["pair"] < args.start_pair + args.pairs
                    or row["teacher_owner"] not in owners
                    or row.get("evaluation_identity") != contract):
                raise ValueError("invalid existing evaluation record")
            completed.add(key)
    rollout_type = install_puct(args.project / "research/puct-optimized")
    engines = []
    log = (args.output / "engine.log").open("a")

    def stop(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for offset in range(config.particles + 1):
            instance = args.output / f"engine-{offset}"
            if instance.exists():
                instance = args.output / f"engine-{offset}-{time.time_ns()}"
            process = subprocess.Popen([sys.executable, str(args.engine_script), "--project",
                                        str(args.project), "--port", str(args.port + offset),
                                        "--instance", str(instance)], stdout=log, stderr=subprocess.STDOUT)
            engines.append(process)
            wait_for_engine(process, instance)
        with matches.open("a", buffering=1) as output:
            for pair in range(args.start_pair, args.start_pair + args.pairs):
                for owner in owners:
                    if (pair, owner) in completed:
                        continue
                    attempt = len(list(args.output.glob(f"failure-{pair}-{owner}-*.json")))
                    try:
                        row = play_game(args, model, identity, config, rollout_type, pair, owner, attempt)
                    except Exception:
                        failure = dict(pair=pair, teacher_owner=owner, attempt=attempt,
                                       traceback=traceback.format_exc())
                        atomic_write(args.output / f"failure-{pair}-{owner}-{attempt}.json",
                                     json.dumps(failure).encode())
                        raise
                    row["evaluation_identity"] = contract
                    output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                    print(json.dumps(row), flush=True)
    finally:
        for process in engines:
            process.terminate()
        for process in engines:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()
    atomic_write(args.output / "done.json", json.dumps(dict(games=args.pairs * len(owners))).encode(), replace=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
