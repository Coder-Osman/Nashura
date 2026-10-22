"""Evaluate two frozen event policies in owner-swapped native Hog matches."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import torch

from ..event_checkpoint import load_event_checkpoint
from .native import NativeMatchRuntime, install_puct


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def play_game(port, policies, identities, rollout_type, *, seed, candidate_owner, deadline):
    owners = {candidate_owner: SimpleNamespace(policy=policies[0]),
              1 - candidate_owner: SimpleNamespace(policy=policies[1])}
    shas = {candidate_owner: identities[0], 1 - candidate_owner: identities[1]}
    runtime = NativeMatchRuntime(port, owners, shas, rollout_type=rollout_type, seed=seed,
                                 match_id=f"fair-policy-eval-{seed}-{candidate_owner}",
                                 transport="compact-delta")
    started = time.monotonic()
    try:
        with torch.inference_mode():
            while not runtime.ended:
                if time.monotonic() - started > deadline:
                    raise TimeoutError("evaluation game exceeded wall deadline")
                frozen = runtime.freeze_due_information()
                if not frozen:
                    runtime.advance()
                    continue
                from .inference import action_key
                selected = {}
                for owner, information in sorted(frozen.items()):
                    policy = owners[owner].policy
                    device = next(policy.parameters()).device
                    frame = information.frame.to_model_input(device, float_dtype=torch.float32)
                    context = policy.encode(frame, policy.initial_state(frame.batch_size, device=device),
                                episode_start=torch.ones(frame.batch_size, dtype=torch.bool, device=device))
                    masks = {name: value.to(device) for name, value in information.masks.items()}
                    selected[owner] = action_key(policy.select_action(context, **masks, sample=False, validate=True).action)
                runtime.commit_simultaneous(selected, frozen)
        evidence = runtime.terminal_evidence()
        if not evidence["ended"] or evidence["truncated"]:
            raise ValueError("evaluation requires a native complete terminal")
        if evidence["rejected"] or evidence["unconfirmed"]:
            raise ValueError("evaluation game contains rejected or unconfirmed actions")
        winner = evidence["winner"]
        return dict(seed=seed, candidate_owner=candidate_owner, winner=winner,
                    score=.5 if winner is None else float(winner == candidate_owner),
                    terminal_tick=evidence["terminal_tick"],
                    terminal=evidence,
                    wall_seconds=time.monotonic() - started)
    finally:
        runtime.close()


def wait_for_engine(process, instance, timeout=240):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("native engine exited during startup")
        state = instance / "process.json"
        if state.exists() and json.loads(state.read_text()).get("ready"):
            return
        time.sleep(.5)
    raise TimeoutError("native engine startup exceeded deadline")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--expected-baseline-sha256", required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--engine-script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--start-pair", type=int, required=True)
    parser.add_argument("--candidate-owner", type=int, choices=(0, 1))
    parser.add_argument("--pairs", type=int, required=True)
    parser.add_argument("--seed-base", type=int, default=440000)
    parser.add_argument("--deadline", type=float, default=600.)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--candidate-decoder", choices=("greedy", "joint-beam"), default="greedy")
    parser.add_argument("--beam-width", type=int, default=32)
    args = parser.parse_args(argv)
    if args.pairs < 1 or args.start_pair < 0:
        raise ValueError("pair range must be nonempty and nonnegative")
    torch.set_num_threads(1)
    args.output.mkdir(parents=True, exist_ok=True)
    candidate_sha = file_sha256(args.candidate)
    baseline_sha = file_sha256(args.baseline)
    if (candidate_sha != args.expected_candidate_sha256
            or baseline_sha != args.expected_baseline_sha256):
        raise ValueError("frozen checkpoint identity differs")
    identity = dict(candidate_sha256=candidate_sha, baseline_sha256=baseline_sha,
                    start_pair=args.start_pair, pairs=args.pairs, seed_base=args.seed_base,
                    mode="policy-direct", deck="fixed-hog", transport="compact-delta")
    if args.candidate_decoder == "joint-beam":
        if args.beam_width < 1:
            raise ValueError("beam width must be positive")
        identity.update(candidate_decoder=args.candidate_decoder, beam_width=args.beam_width,
                        baseline_decoder="greedy")
    owners = (0, 1) if args.candidate_owner is None else (args.candidate_owner,)
    identity.update(code_sha256=file_sha256(Path(__file__)), owners=list(owners))
    identity_path = args.output / "identity.json"
    if identity_path.exists():
        if json.loads(identity_path.read_text()) != identity:
            raise ValueError("evaluation identity differs from existing output")
    else:
        identity_path.write_text(json.dumps(identity, sort_keys=True) + "\n")
    completed = {}
    matches = args.output / "matches.jsonl"
    if matches.exists():
        for line in matches.read_text().splitlines():
            row = json.loads(line)
            key = (row["pair"], row["candidate_owner"])
            if (key in completed or row["seed"] != args.seed_base + row["pair"]
                    or row["candidate_owner"] not in owners
                    or not args.start_pair <= row["pair"] < args.start_pair + args.pairs):
                raise ValueError("invalid existing evaluation record")
            completed[key] = row
    policies = [load_event_checkpoint(path, device=args.device).model
                for path in (args.candidate, args.baseline)]
    for policy in policies:
        policy.eval()
    if args.candidate_decoder == "joint-beam":
        from .joint_decode import install_joint_beam

        install_joint_beam(policies[0], width=args.beam_width)
    rollout_type = install_puct(args.puct_root)
    instance = args.output / f"engine-{time.time_ns()}"
    engine_log = (args.output / "engine.log").open("a")
    engine = subprocess.Popen([sys.executable, str(args.engine_script), "--project", str(args.project),
                               "--port", str(args.port), "--instance", str(instance)],
                              cwd=args.project, stdout=engine_log, stderr=subprocess.STDOUT)

    def stop(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        wait_for_engine(engine, instance)
        with matches.open("a", buffering=1) as output:
            for pair in range(args.start_pair, args.start_pair + args.pairs):
                for owner in owners:
                    if (pair, owner) in completed:
                        continue
                    if engine.poll() is not None:
                        raise RuntimeError("native engine exited during evaluation")
                    result = play_game(args.port, policies, (candidate_sha, baseline_sha), rollout_type,
                                       seed=args.seed_base + pair, candidate_owner=owner,
                                       deadline=args.deadline)
                    row = dict(pair=pair, **result)
                    output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                    print(json.dumps(row, sort_keys=True), flush=True)
    finally:
        engine.terminate()
        try:
            engine.wait(timeout=30)
        except subprocess.TimeoutExpired:
            engine.kill()
            engine.wait()
        engine_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
