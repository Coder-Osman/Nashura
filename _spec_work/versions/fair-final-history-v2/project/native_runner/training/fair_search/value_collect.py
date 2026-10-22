import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

import torch

from .evaluate_policy import wait_for_engine
from .inference import action_key
from .model import load_checkpoint
from .native import NativeMatchRuntime, install_puct
from .storage import atomic_write
from .value_data import BASE_SHA, SAMPLING_SHA, ValueDecision, ValueEpisode, encode_episode, terminal_label


def sample_actions(model, frozen, job, decision_index):
    device = next(model.parameters()).device
    selected = {}
    for owner, information in sorted(frozen.items()):
        payload = f"value-direct.v1:{job['episode_id']}:{job['seed']}:{owner}:{decision_index}:{information.tick}"
        seed = int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big") % (2**63)
        context, _ = model.encode(information.frame)
        masks = {name: mask.to(device) for name, mask in information.masks.items()}
        with torch.random.fork_rng(devices=[device.index or 0] if device.type == "cuda" else []):
            torch.manual_seed(seed)
            action = model.policy.select_action(context, **masks, sample=True, validate=True,
                                                temperatures=(1., 1., 1., 1.)).action
        selected[owner] = action_key(action)
        if not information.legal(selected[owner]):
            raise ValueError("direct policy produced an illegal action")
    return selected


def collect_game(runtime, model, job, *, runtime_sha256, max_seconds=1800., progress=None):
    started = time.monotonic()
    rows = []
    try:
        with torch.inference_mode():
            while not runtime.ended:
                if time.monotonic() - started >= max_seconds:
                    raise TimeoutError("direct game deadline is not a draw")
                frozen = runtime.freeze_due_information()
                if not frozen:
                    runtime.advance()
                    continue
                selected = sample_actions(model, frozen, job, len(rows))
                pending = [ValueDecision(len(rows) + offset, owner, information.tick,
                                          information.frame, dict(information.masks), selected[owner], 1)
                           for offset, (owner, information) in enumerate(sorted(frozen.items()))]
                runtime.commit_simultaneous(selected, frozen)
                rows.extend(pending)
                if progress is not None:
                    progress(dict(episode_id=job["episode_id"], tick=max(info.tick for info in frozen.values()),
                                  rows=len(rows), elapsed=time.monotonic() - started))
        terminal = runtime.terminal_evidence()
        rows = tuple(replace(row, label=terminal_label(terminal["winner"], row.owner)) for row in rows)
        return ValueEpisode(job["episode_id"], job["seed"], job["split"], job["attempt"], BASE_SHA,
                            runtime_sha256, SAMPLING_SHA, rows, terminal)
    finally:
        runtime.close()


class CoordinatorClient:
    def __init__(self, root):
        self.config = json.loads((Path(root) / "connection.json").read_text())

    def request(self, route, payload=None, data=None):
        if data is None:
            data = json.dumps(payload or {}).encode()
        request = urllib.request.Request(self.config["url"] + route, data=data,
                                         headers={"Authorization": "Bearer " + self.config["token"]})
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    root = args.root
    worker = root / "actors" / f"slot-{args.slot}"
    worker.mkdir(parents=True, exist_ok=True)
    model, _, _ = load_checkpoint(root / "initial.pt", device=args.device, expected_sha256=BASE_SHA)
    model.eval().requires_grad_(False)
    manifest = json.loads((root / "manifest.json").read_text())
    rollout_type = install_puct(root / "project/research/puct-optimized")
    client = CoordinatorClient(root)
    port = 29000 + args.slot
    instance = worker / f"engine-{time.time_ns()}"

    def stop(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (worker / "engine.log").open("a") as log:
        process = subprocess.Popen([sys.executable, str(root / "isolated_engine.py"), "--project",
                                    str(root / "project"), "--port", str(port), "--instance", str(instance)],
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for_engine(process, instance)
            while True:
                assignment = client.request("/claim", dict(host=args.host, slot=args.slot))
                if assignment.get("finished"):
                    return 0
                job = assignment.get("job")
                if job is None:
                    time.sleep(10)
                    continue
                try:
                    if process.poll() is not None:
                        raise RuntimeError("owned engine exited")
                    runtime = NativeMatchRuntime(port, {0: model, 1: model}, {0: BASE_SHA, 1: BASE_SHA},
                                                 rollout_type=rollout_type, seed=job["seed"],
                                                 match_id=job["episode_id"] + f"-{job['attempt']}",
                                                 transport="compact-delta")
                    episode = collect_game(runtime, model, job, runtime_sha256=manifest["runtime_sha256"],
                                           progress=lambda record: atomic_write(
                                               worker / "progress.json", json.dumps(record).encode(), replace=True))
                    data = encode_episode(episode)
                    pending = worker / (job["episode_id"] + f"-{job['attempt']}.pt.gz")
                    atomic_write(pending, data, replace=True)
                    receipt = None
                    for retry in range(3):
                        try:
                            receipt = client.request("/complete", data=data)
                            break
                        except (OSError, TimeoutError):
                            if retry == 2:
                                raise
                            time.sleep(10)
                    if receipt.get("sha256") != hashlib.sha256(data).hexdigest():
                        raise ValueError("central receipt differs")
                    atomic_write(worker / (job["episode_id"] + ".receipt.json"),
                                 json.dumps(receipt).encode(), replace=True)
                    pending.unlink()
                except Exception as error:
                    evidence = dict(job=job, host=args.host, slot=args.slot, error=repr(error),
                                    traceback=traceback.format_exc(), transient=isinstance(error, (OSError, TimeoutError))
                                    and not isinstance(error, urllib.error.HTTPError))
                    atomic_write(worker / f"failure-{job['episode_id']}-{job['attempt']}.json",
                                 json.dumps(evidence).encode(), replace=True)
                    client.request("/failure", evidence)
                    raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
