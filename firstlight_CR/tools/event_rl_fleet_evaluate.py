"""Wait for a completed fleet run, then compare frozen checkpoints on both sides.

Runs next to an existing run's orchestrator.py without changing training code.
Every pair uses a new engine seed twice, with the RL checkpoint swapping sides.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shlex
import subprocess
import time
import traceback


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def parse_match(events, *, rl_owner, rl_sha, opponent_sha):
    loaded = {e["role"]: e for e in events if e.get("event") == "checkpoint-loaded"}
    expected = {"learner": rl_sha if rl_owner == 0 else opponent_sha,
                "opponent": opponent_sha if rl_owner == 0 else rl_sha}
    for role, digest in expected.items():
        if loaded.get(role, {}).get("checkpoint_sha256") != digest:
            raise ValueError(f"checkpoint mismatch for {role}")
    if loaded["learner"]["contract_sha256"] != loaded["opponent"]["contract_sha256"]:
        raise ValueError("checkpoint contracts differ")
    matches = [e for e in events if "match" in e]
    if len(matches) != 1 or matches[0].get("terminal") is not True:
        raise ValueError("expected exactly one complete terminal match")
    match = matches[0]
    rewards = [float(match["results"][str(owner)]) for owner in (0, 1)]
    if any(r not in (-1.0, 0.0, 1.0) for r in rewards) or sum(rewards) != 0:
        raise ValueError("expected unshaped zero-sum terminal results")
    reward = rewards[rl_owner]
    return {"rl_owner": rl_owner, "rl_sha256": rl_sha,
            "opponent_sha256": opponent_sha, "reward": reward,
            "score": (reward + 1) / 2,
            **{k: match.get(k) for k in ["terminal", "final_tick", "elapsed_s",
                 "rows", "cancelled_hand_slot", "cancelled_play_kind", "act_violation"]}}


def summarize(records, expected_pairs, bootstrap_samples=5000):
    pairs = {}
    for record in records:
        sides = pairs.setdefault(record["pair_id"], {})
        if record["rl_owner"] in sides:
            raise ValueError("duplicate side in evaluation pair")
        sides[record["rl_owner"]] = record
    if len(pairs) != expected_pairs or any(set(p) != {0, 1} for p in pairs.values()):
        raise ValueError("evaluation needs all scheduled complete pairs")
    def counts(items):
        n = len(items)
        wins = sum(x["reward"] > 0 for x in items)
        draws = sum(x["reward"] == 0 for x in items)
        return {"games": n, "wins": wins, "draws": draws, "losses": n-wins-draws,
                "win_rate": wins/n, "score_rate": (wins+0.5*draws)/n}
    pair_scores = [sum(x["score"] for x in sides.values()) / 2 for sides in pairs.values()]
    rng = random.Random(9172026)
    samples = sorted(sum(rng.choices(pair_scores, k=expected_pairs))/expected_pairs
                     for _ in range(bootstrap_samples))
    ci = [samples[int(0.025*bootstrap_samples)], samples[min(bootstrap_samples-1, int(0.975*bootstrap_samples))]]
    return {**counts(records), "pairs": expected_pairs,
            "by_rl_owner": {str(o): counts([x for x in records if x["rl_owner"] == o]) for o in (0, 1)},
            "score_rate_95_ci": ci, "ci_method": "paired percentile bootstrap, 5000 samples, seed 9172026",
            "interpretation": "RL advantage" if ci[0] > 0.5 else "RL disadvantage" if ci[1] < 0.5 else "inconclusive"}


class Evaluation:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.output = self.root / "strength-eval-v1"
        self.output.mkdir(exist_ok=True)
        spec = importlib.util.spec_from_file_location("fleet", self.root / "orchestrator.py")
        self.fleet = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fleet)
        self.config = self.fleet.CFG

    def state(self, event, **kwargs):
        value = {"event": event, "time": time.time(), **kwargs}
        write_json(self.output / "status.json", value)
        print(json.dumps(value), flush=True)

    def completed_weight(self):
        status = json.loads((self.root / "status.json").read_text())
        if status["event"] in ("fatal", "stopped"):
            raise RuntimeError(f"training ended without completion: {status['event']}")
        bootstrap = json.loads((self.root / "bootstrap-status.json").read_text())
        if bootstrap.get("phase") == "failed":
            raise RuntimeError("training bootstrap failed")
        if status["event"] != "completed":
            pid = int((self.root / "orchestrator.pid").read_text())
            os.kill(pid, 0)
            return None
        if status["iterations"] != self.config["iterations"]:
            raise ValueError("training completed count differs from configuration")
        success = json.loads((self.root / f"iteration-{status['iterations']:06d}" / "success.json").read_text())
        if success["checkpoint"] != status["checkpoint"]:
            raise ValueError("final successful checkpoint differs from completed status")
        return Path(status["checkpoint"])

    def check_stop(self):
        if (self.output / "STOP").exists():
            raise RuntimeError("evaluation STOP requested")

    def prepare(self, final):
        baseline = self.root / "weights" / (self.config["initial_checkpoint_sha256"] + ".pt")
        opponents = [("IL", baseline)]
        for iteration in (5, 10, 15):
            success = json.loads((self.root / f"iteration-{iteration:06d}" / "success.json").read_text())
            opponents.append((f"RL-{iteration:02d}", Path(success["checkpoint"])))
        all_weights = [final, *[p for _, p in opponents]]
        for p in all_weights:
            if p.parent != self.root / "weights" or hashlib.sha256(p.read_bytes()).hexdigest() != p.stem:
                raise ValueError(f"weight identity mismatch: {p}")
        load_check = """import sys, torch
from native_runner.training.event_checkpoint import load_event_checkpoint
contracts=set()
for path in sys.argv[1:]:
 loaded=load_event_checkpoint(path,device='cpu')
 assert loaded.model.backbone.config.snapshot_only
 assert all(torch.isfinite(p).all() for p in loaded.model.parameters())
 contracts.add(loaded.contract_sha256)
assert len(contracts)==1
print('five checkpoints passed load and contract checks')
"""
        subprocess.run([str(self.fleet.PY), "-c", load_check, *map(str, all_weights)],
                       cwd=self.fleet.CODE, env={**self.fleet.ENV, "CUDA_VISIBLE_DEVICES": ""}, check=True)
        def host_prepare(h):
            r = self.fleet.FLEET[h]["run"]
            if h != "34":
                for weight in all_weights:
                    self.fleet.transport(h, str(weight), r+"/weights/"+weight.name)
            code = ("import pathlib,hashlib,json,shutil; r=pathlib.Path("+repr(r)+"); "
                    "assert shutil.disk_usage(r).free>1500000000, 'disk space guard'; "
                    "m=json.loads((r/'manifest.json').read_text()); "
                    "assert all(hashlib.sha256((r/'code'/p).read_bytes()).hexdigest()==v for p,v in m.items()); "
                    "assert all(hashlib.sha256((r/'weights'/name).read_bytes()).hexdigest()==name[:-3] for name in "
                    +repr([p.name for p in all_weights])+ "); (r/'strength-eval-v1').mkdir(exist_ok=True)")
            self.fleet.ssh(h, "python3 -c " + shlex.quote(code), timeout=180)
        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            list(pool.map(host_prepare, self.fleet.FLEET))
        return opponents

    def match(self, h, port, pair_id, seed, owner, label, final, opponent):
        local = self.output / label / f"pair-{pair_id:04d}" / f"rl-owner-{owner}.json"
        if local.exists():
            result = json.loads(local.read_text())
            if (result["rl_sha256"], result["opponent_sha256"], result["seed"]) != (final.stem, opponent.stem, seed):
                raise ValueError("saved evaluation belongs to another configuration")
            return result
        self.check_stop()
        r = self.fleet.FLEET[h]["run"]
        first, second = (final, opponent) if owner == 0 else (opponent, final)
        errors = []
        for attempt in range(3):
            self.check_stop()
            out = f"{r}/strength-eval-v1/{label}/pair-{pair_id:04d}/rl-owner-{owner}-attempt-{time.time_ns()}"
            argv = ["--checkpoint", r+"/weights/"+first.name,
                    "--opponent-checkpoint", r+"/weights/"+second.name,
                    "--out-dir", out, "--port", str(port), "--matches", "1", "--seed", str(seed),
                    "--max-wait-ticks", "300", "--max-advance-ticks", "120", "--max-seconds", "1800",
                    "--margin-beta", "0", "--deck0", "golem-beatdown", "--deck1", "golem-beatdown",
                    "--temperatures", "1,1,1,1", "--device", "cpu"]
            hook = f'import torch,runpy; torch.manual_seed({seed}); runpy.run_path("tools/event_rl_rollout.py",run_name="__main__")'
            # Each retry preserves the seed and both checkpoints, and keeps its log.
            command = ("cd "+shlex.quote(r+"/code")+" && mkdir -p "+shlex.quote(out)
                       +" && env OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 CUDA_VISIBLE_DEVICES= "
                       +".venv/bin/python -c "+shlex.quote(hook)+" "+shlex.join(argv)
                       +" > "+shlex.quote(out+"/collector.log")+" 2>&1")
            try:
                self.fleet.ssh(h, command, timeout=2000)
                read = ("import json,pathlib; p=pathlib.Path("+repr(out+"/collector.log")+"); "
                        "print(json.dumps([json.loads(x) for x in p.read_text().splitlines() if x.startswith('{')]))")
                response = self.fleet.ssh(h, "python3 -c "+shlex.quote(read), capture_output=True, text=True, timeout=45)
                result = parse_match(json.loads(response.stdout), rl_owner=owner,
                                     rl_sha=final.stem, opponent_sha=opponent.stem)
                result.update(host=h, port=port, pair_id=pair_id, seed=seed, opponent=label,
                              remote_output=out, retries=errors)
                local.parent.mkdir(parents=True, exist_ok=True)
                write_json(local, result)
                return result
            except Exception as exc:
                errors.append({"attempt": attempt, "error": str(exc), "remote_output": out})
        local.parent.mkdir(parents=True, exist_ok=True)
        write_json(local.with_suffix(".failed.json"), errors)
        raise RuntimeError(f"three attempts failed for {label}, pair {pair_id}, owner {owner}")

    def run(self, wait):
        lock = (self.output / "evaluation.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (self.output / "report.json").exists():
            print("Evaluation already complete", flush=True)
            return
        (self.output / "pid").write_text(str(os.getpid()))
        self.state("waiting_for_training")
        while True:
            self.check_stop()
            final = self.completed_weight()
            if final is not None:
                break
            if not wait:
                print("Training still running; evaluation not started", flush=True)
                return
            time.sleep(30)
        # Acquire the training lock before reusing its simulator ports.
        training_lock = (self.root / "orchestrator.lock").open("a")
        while True:
            try:
                fcntl.flock(training_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                self.check_stop()
                time.sleep(5)
        self.state("preparing", final_checkpoint=str(final))
        opponents = self.prepare(final)
        slots = [(h, port) for h, f in self.fleet.FLEET.items() for port in f["ports"]]
        pairs_per_opponent = len(slots)*2
        plan = {"schema": "event-strength-evaluation.v1", "final_checkpoint": str(final),
                "final_sha256": final.stem, "opponents": {label: p.stem for label,p in opponents},
                "pairs_per_opponent": pairs_per_opponent, "games_per_opponent": pairs_per_opponent*2,
                "seed_base": 927000000, "temperatures": [1,1,1,1], "deck": "golem-beatdown",
                "level": 11, "margin_beta": 0, "side_swapped": True,
                "scope": "frozen checkpoints, stochastic native battles, no training updates"}
        write_json(self.output / "plan.json", plan)
        reports = {}
        for group, (label, opponent) in enumerate(opponents):
            self.state("evaluating", opponent=label, completed_opponents=list(reports))
            def worker(slot):
                index, (h, port) = slot
                time.sleep(index % 24 * 0.3)
                records = []
                for repeat in range(2):
                    pair_id = index*2+repeat
                    seed = 927000000+group*10000+pair_id
                    for owner in (0, 1):
                        records.append(self.match(h, port, pair_id, seed, owner, label, final, opponent))
                return records
            records = []
            with concurrent.futures.ThreadPoolExecutor(len(slots)) as pool:
                for result in pool.map(worker, enumerate(slots)):
                    records.extend(result)
            reports[label] = summarize(records, pairs_per_opponent)
            write_json(self.output / label / "summary.json", reports[label])
            self.state("opponent_complete", opponent=label, summary=reports[label])
        report = {"plan": plan, "completed_at": time.time(), "results": reports}
        write_json(self.output / "report.json", report)
        lines = ["# Final RL battle evaluation", "", f"Final checkpoint: `{final.stem}`", "",
                 "| Opponent | Games | W / D / L | Score rate | Paired 95% CI |", "| --- | ---: | --- | ---: | --- |"]
        for label, result in reports.items():
            lo, hi = result["score_rate_95_ci"]
            lines.append(f"| {label} | {result['games']} | {result['wins']} / {result['draws']} / {result['losses']} | {result['score_rate']:.2%} | {lo:.2%}–{hi:.2%} |")
        lines += ["", "Score rate counts draws as half a win. Confidence intervals resample seed pairs.",
                  "These results cover this deck, level, temperature and checkpoint set only."]
        (self.output / "report.md").write_text("\n".join(lines)+"\n")
        self.state("completed", report=str(self.output / "report.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    evaluation = Evaluation(args.run_root)
    try:
        evaluation.run(args.wait)
    except BlockingIOError:
        print("Evaluation already running", flush=True)
    except Exception:
        evaluation.state("failed", error=traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
