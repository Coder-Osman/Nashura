"""Re-search FAIR-only roots with clean/noisy teachers on shared reconstructed worlds."""

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import random
import signal
import subprocess
import sys
import time

import torch

from native_runner.training.fair_search.belief import ReplayBeliefProvider
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.contracts import InformationState, semantic_digest
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
from native_runner.training.fair_search.inference import LocalInference
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.native import NativeReplayFactory, install_puct
from native_runner.training.fair_search.search import InformationSetSearch
from native_runner.training.fair_search.storage import atomic_write


def publish(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True, allow_nan=False).encode(), replace=True)


class Lease:
    def __init__(self, world):
        self.world = world

    def __getattr__(self, name):
        return getattr(self.world, name)

    def close(self):
        self.world.restore_root()


class FixedWorld:
    def __init__(self, world):
        self.world = world

    def build(self, information, *, seed, limit):
        if limit != 1 or self.world.root_information().sha256 != information.sha256:
            raise ValueError("fixed-world audit information differs")
        return [Lease(self.world)]


def js_divergence(first, second):
    total_first, total_second = sum(first.values()), sum(second.values())
    value = 0.
    for action in set(first) | set(second):
        left, right = first.get(action, 0) / total_first, second.get(action, 0) / total_second
        mean = (left + right) / 2
        if left:
            value += .5 * left * math.log(left / mean)
        if right:
            value += .5 * right * math.log(right / mean)
    return value


def rollout_value(world, information, action, inference, seed):
    rng = random.Random(seed)
    world.restore_root()
    current = world.information()
    for decision in range(6):
        terminal = world.terminal_result(information.owner)
        if terminal is not None:
            return float(terminal)
        elapsed = (current.tick - information.tick) * .05
        if elapsed >= 10:
            break
        selected = action if decision == 0 else inference.sample(current, seed=rng.randrange(2**63))
        if not current.legal(selected):
            raise ValueError("Q diagnostic proposed illegal action")
        world.apply(selected)
        world.advance(10 - elapsed)
        terminal = world.terminal_result(information.owner)
        if terminal is not None:
            return float(terminal)
        current = world.information()
    _, value = inference.predict(current, seed=rng.randrange(2**63))
    return value if current.owner == information.owner else -value


def audit_root(raw, model, teacher_sha, config, rollout_type, port, output):
    info = InformationState.from_payload(raw["information"])
    if (info.sha256 != raw["observation_sha256"] or semantic_digest(info.masks) != raw["mask_sha256"]
            or raw["teacher_sha256"] != teacher_sha or raw["search_config_sha256"] != config.sha256):
        raise ValueError("clean root provenance differs")
    clean_config = replace(config, root_noise=0.)
    inference = LocalInference(model, teacher_sha, config=config)
    factory = NativeReplayFactory([port], {0: model, 1: model}, {0: teacher_sha, 1: teacher_sha},
                rollout_type, diagnostic_directory=output / "replay-diagnostics", search_only=True,
                transport="compact-delta", conditional_draws=True)
    repetitions = []
    for repeat in range(2):
        seed = int(hashlib.sha256(f"p0-clean:{info.sha256}:{repeat}".encode()).hexdigest()[:15], 16)
        belief = ReplayBeliefProvider(factory, attempts=32)
        worlds = belief.build(info, seed=seed, limit=1)
        world = worlds[0]
        try:
            fixed = FixedWorld(world)
            variants = {}
            for name, budget, training, settings in (("clean32", 32, False, clean_config),
                    ("noisy32", 32, True, config), ("clean128", 128, False, clean_config)):
                result = InformationSetSearch(fixed, inference, config=settings).run(info, seed=seed + 1,
                    training=training, diagnostic_simulations=budget, audit_root=True)
                variants[name] = dict(visits=result.visits, selected=result.selected,
                        mode=min(result.visits, key=lambda key: (-result.visits[key], key)),
                        audit=result.audit, seconds=result.seconds, timings=result.timings,
                        search_config=asdict(settings), diagnostic_simulations=budget)
            teacher_action = variants["clean32"]["mode"]
            actions = {teacher_action}
            for record in raw["decoded"].values():
                actions.update((record["greedy"], record["joint_beam"]))
            estimates = {action: [rollout_value(world, info, action, inference, seed + 1000 + ordinal)
                                  for ordinal in range(4)] for action in sorted(actions)}
            regressions = {name + ":" + decoder: [teacher - student for teacher, student in
                        zip(estimates[teacher_action], estimates[record[decoder]])]
                        for name, record in raw["decoded"].items() for decoder in ("greedy", "joint_beam")}
            world.restore_root()
            if world.root_information().sha256 != info.sha256:
                raise ValueError("root changed after clean audit")
            repetitions.append(dict(seed=seed, belief=belief.profile, variants=variants,
                    noisy_clean_jsd=js_divergence(variants["noisy32"]["visits"], variants["clean32"]["visits"]),
                    original_clean_jsd=js_divergence(raw["original_visits"], variants["clean32"]["visits"]),
                    q_diagnostic=dict(method="four-common-seed-policy-continuations-six-events-ten-seconds",
                                      independent_of_search_rng=True, approximate=True,
                                      values=estimates, paired_regrets=regressions)))
        finally:
            world.close()
        if factory.used:
            raise ValueError("clean audit leaked world handle")
    first = repetitions[0]["variants"]["clean32"]
    return dict(schema="fair-clean-teacher-label.v1", observation_sha256=info.sha256,
                mask_sha256=raw["mask_sha256"], teacher_sha256=teacher_sha,
                search_config_sha256=clean_config.sha256, clean_visits=first["visits"],
                teacher_mode=first["mode"], selected_plan=raw["selected_plan"],
                executed_action=None, execution_status="unknown", owner=info.owner, tick=info.tick,
                split=raw["split"], result=raw["result"], repetitions=repetitions,
                clean_mode_repeat_agreement=first["mode"] == repetitions[1]["variants"]["clean32"]["mode"],
                trace_digests=factory.completed_traces, promoted=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--engine-script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device", default="cuda:7")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards or args.limit is not None and args.limit < 1:
        parser.error("invalid audit shard or limit")
    torch.set_num_threads(1)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((args.source / "manifest.json").read_text())
    config = SearchConfig(**manifest["config"])
    model, _, identity = load_checkpoint(args.source / "initial.pt", device=args.device,
                                          expected_sha256=manifest["checkpoint_sha256"])
    model.eval()
    rollout_type = install_puct(args.project / "research/puct-optimized")
    instance = args.output / f"engine-{time.time_ns()}"
    log = (args.output / "engine.log").open("a")
    engine = subprocess.Popen([sys.executable, str(args.engine_script), "--project", str(args.project),
                               "--port", str(args.port), "--instance", str(instance)], stdout=log, stderr=subprocess.STDOUT)

    def stop(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    completed = 0
    try:
        wait_for_engine(engine, instance)
        while True:
            if engine.poll() is not None:
                raise RuntimeError("clean audit engine exited")
            queue_path = args.audit / "roots.json"
            if not queue_path.exists():
                time.sleep(5)
                continue
            queue = json.loads(queue_path.read_text())
            jobs = [row for index, row in enumerate(queue["roots"]) if index % args.shards == args.shard]
            if args.limit:
                jobs = jobs[:args.limit]
            for job in jobs:
                destination = args.output / "labels" / (job["key"] + ".json")
                if destination.exists():
                    if json.loads(destination.read_text())["input_sha256"] != job["sha256"]:
                        raise ValueError("completed clean input differs")
                    continue
                path = Path(job["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != job["sha256"]:
                    raise ValueError("clean root file checksum differs")
                raw = torch.load(path, map_location="cpu", weights_only=True)
                publish(args.output / "status.json", dict(stage="searching", root=job["key"], completed=completed))
                with torch.inference_mode():
                    result = audit_root(raw, model, identity, config, rollout_type, args.port, args.output)
                result["input_sha256"] = job["sha256"]
                publish(destination, result)
                completed += 1
                print(json.dumps(dict(stage="root-completed", root=job["key"], completed=completed)), flush=True)
            if queue["complete"] or args.limit and len(jobs) >= args.limit:
                publish(args.output / "status.json", dict(stage="completed", completed=len(jobs), promoted=False))
                break
            time.sleep(5)
    except Exception as error:
        publish(args.output / "status.json", dict(stage="failed", completed=completed, error=str(error)))
        raise
    finally:
        engine.terminate()
        try:
            engine.wait(timeout=30)
        except subprocess.TimeoutExpired:
            engine.kill()
            engine.wait()
        log.close()


if __name__ == "__main__":
    main()
