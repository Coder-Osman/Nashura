import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

import torch

from .contracts import require_sha, semantic_digest
from .model import load_checkpoint
from .storage import atomic_write
from .value_data import BASE_SHA, load_episode
from .value_train import cache_episode, load_cache, policy_digest


def validate_feature(root, data, job, policy_sha256):
    root = Path(root)
    require_sha(policy_sha256)
    episode_id = job["episode_id"]
    cache = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    if (cache.get("schema") != "fair-value-features.v1" or cache.get("episode_id") != episode_id
            or cache.get("model_sha256") != BASE_SHA or cache.get("policy_sha256") != policy_sha256):
        raise ValueError("feature cache identity differs")
    marker = root / "episodes" / episode_id / "completed.json"
    source = json.loads(marker.read_text())
    episode = load_episode(marker)
    if (episode.seed != job["seed"] or episode.split != job["split"]
            or episode.model_sha256 != BASE_SHA or episode.runtime_sha256 != job["runtime_sha256"]
            or cache.get("split") != episode.split or cache.get("source_sha256") != source["semantic_sha256"]):
        raise ValueError("feature source identity differs")
    rows = len(episode.decisions)
    for key, width in (("features", job["feature_width"]), ("baseline", 3)):
        tensor = cache[key]
        if (tensor.dtype != torch.float32 or tensor.shape != (rows, width)
                or not bool(torch.isfinite(tensor).all())):
            raise ValueError("invalid feature tensor")
    for key, attribute in (("labels", "label"), ("owners", "owner"), ("ticks", "tick")):
        expected = torch.tensor([getattr(row, attribute) for row in episode.decisions], dtype=torch.long)
        if cache[key].dtype != torch.long or not torch.equal(cache[key], expected):
            raise ValueError("feature labels or ordering differ")
    if cache["input_sha256"] != [semantic_digest(row.frame) for row in episode.decisions]:
        raise ValueError("feature input semantics differ")
    feature_path = root / "features" / (episode_id + ".pt")
    feature_marker = feature_path.with_suffix(".json")
    lock_path = root / "upload-locks" / (episode_id + ".feature.lock")
    lock_path.parent.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if feature_marker.exists():
            existing = json.loads(feature_marker.read_text())
            if existing["sha256"] != digest:
                raise ValueError("conflicting feature cache")
        receipt = dict(schema="fair-value-feature-manifest.v1", episode_id=episode_id,
                       rows=rows, bytes=len(data), sha256=digest, source_sha256=cache["source_sha256"],
                       model_sha256=BASE_SHA, policy_sha256=policy_sha256, seed=episode.seed,
                       split=episode.split, runtime_sha256=episode.runtime_sha256, data_file=feature_path.name)
        atomic_write(feature_path, data, replace=True)
        atomic_write(feature_marker, json.dumps(receipt, sort_keys=True).encode(), replace=True)
    return receipt


class FeatureCoordinator:
    def __init__(self, root, executor):
        self.root = Path(root)
        self.executor = executor
        self.lock = threading.RLock()
        self.inflight = {}
        self.plan = json.loads((self.root / "feature-plan.json").read_text())
        manifest = json.loads((self.root / "manifest.json").read_text())
        if self.plan["manifest_sha256"] != semantic_digest(manifest):
            raise ValueError("feature plan dataset differs")
        self.path = self.root / "feature-state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else dict(
            started=time.time(), jobs={job["episode_id"]: dict(**job, state="pending",
                feature_width=self.plan["feature_width"], runtime_sha256=manifest["runtime_sha256"])
                for job in manifest["jobs"]}, workers={}, failures=[])
        for episode_id, job in self.state["jobs"].items():
            path = self.root / "features" / (episode_id + ".json")
            if path.exists():
                receipt = json.loads(path.read_text())
                if (receipt.get("schema") == "fair-value-feature-manifest.v1"
                        and receipt.get("policy_sha256") == self.plan["policy_sha256"]):
                    job.update(state="complete", receipt=receipt)
        self.save()

    def save(self):
        atomic_write(self.path, json.dumps(self.state, sort_keys=True).encode(), replace=True)

    def status(self):
        with self.lock:
            counts = {state: sum(job["state"] == state for job in self.state["jobs"].values())
                      for state in ("pending", "running", "complete", "failed")}
            return dict(counts=counts, seconds=time.time() - self.state["started"],
                        workers=self.state["workers"], failures=self.state["failures"])

    def claim(self, payload):
        worker = payload["worker"]
        if worker not in self.plan["workers"]:
            raise ValueError("unknown feature worker")
        with self.lock:
            self.state["workers"][worker] = dict(updated=time.time(), **payload)
            if self.state["failures"] or (self.root / "features.pause").exists():
                return dict(job=None, paused=True)
            if not payload.get("healthy") or payload.get("policy_sha256") != self.plan["policy_sha256"]:
                return dict(job=None, paused=True)
            for job in self.state["jobs"].values():
                if job["state"] == "running" and job.get("worker") == worker:
                    return dict(job=job)
            for job in self.state["jobs"].values():
                if job["state"] == "pending":
                    job.update(state="running", worker=worker, started=time.time())
                    marker = self.root / "episodes" / job["episode_id"] / "completed.json"
                    job["source"] = json.loads(marker.read_text())
                    cache_marker = self.root / "features" / (job["episode_id"] + ".json")
                    if cache_marker.exists():
                        job["reuse_sha256"] = json.loads(cache_marker.read_text())["sha256"]
                    self.save()
                    return dict(job=job)
            self.save()
            return dict(job=None, finished=all(job["state"] == "complete" for job in self.state["jobs"].values()))

    def complete(self, data, episode_id, worker):
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            job = self.state["jobs"].get(episode_id)
            if job is None or worker not in self.plan["workers"]:
                raise ValueError("invalid feature assignment")
            if job["state"] == "complete":
                if job["receipt"]["sha256"] != digest:
                    raise ValueError("conflicting feature completion")
                return job["receipt"]
            if job["state"] != "running" or job.get("worker") != worker:
                raise ValueError("feature worker assignment differs")
            future = self.inflight.get((episode_id, digest))
            if future is None:
                future = self.executor.submit(validate_feature, self.root, data, dict(job), self.plan["policy_sha256"])
                self.inflight[(episode_id, digest)] = future
        try:
            receipt = future.result()
            with self.lock:
                job.update(state="complete", receipt=receipt, ended=time.time())
                self.save()
            return receipt
        finally:
            with self.lock:
                self.inflight.pop((episode_id, digest), None)

    def failure(self, payload):
        with self.lock:
            if payload["worker"] not in self.plan["workers"]:
                raise ValueError("unknown failed feature worker")
            self.state["failures"].append(dict(**payload, time=time.time()))
            self.save()
        return dict(paused=True)


def request(connection, route, payload=None, data=None, headers=None, raw=False):
    body = data if data is not None else json.dumps(payload or {}).encode()
    if route.startswith(("/episode/", "/feature-cache/", "/episode-marker/")):
        body = None
    query = urllib.request.Request(connection["url"] + route, data=body,
        headers={"Authorization": "Bearer " + connection["token"], **(headers or {})})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(query, timeout=600) as response:
                return response.read() if raw else json.load(response)
        except urllib.error.HTTPError:
            raise
        except (OSError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(5)


def download_episode(connection, job, folder):
    data = request(connection, "/episode/" + job["episode_id"], raw=True)
    source = job.get("source") or request(connection, "/episode-marker/" + job["episode_id"])
    if len(data) != source["bytes"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError("downloaded episode checksum differs")
    atomic_write(folder / "episode.pt.gz", data, replace=True)
    atomic_write(folder / "completed.json", json.dumps(source).encode(), replace=True)
    return folder / "completed.json"


def verify_reference(model, connection, plan, folder):
    identity = plan["reference_episode"]
    marker = download_episode(connection, dict(episode_id=identity), folder)
    episode = load_episode(marker)
    raw = request(connection, "/feature-cache/" + identity, raw=True)
    if hashlib.sha256(raw).hexdigest() != plan["reference_sha256"]:
        raise ValueError("reference cache identity differs")
    cache = torch.load(io.BytesIO(raw), map_location="cpu", weights_only=True)
    indices = random.Random(20261012).sample(range(len(episode.decisions)), min(3, len(episode.decisions)))
    maximum = 0.
    with torch.inference_mode():
        for index in indices:
            context, logits = model.encode(episode.decisions[index].frame)
            actual = context.value_context.cpu()
            expected = cache["features"][index:index + 1]
            torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-5)
            torch.testing.assert_close(logits.cpu(), cache["baseline"][index:index + 1], rtol=1e-5, atol=1e-5)
            maximum = max(maximum, float((actual - expected).abs().max()))
    return dict(passed=True, rows=indices, max_absolute_error=maximum,
                deterministic=torch.are_deterministic_algorithms_enabled())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.set_num_threads(1)
    root = args.root
    plan = json.loads((root / "feature-plan.json").read_text())
    connection = json.loads((root / "connection.json").read_text())
    if args.worker not in plan["workers"]:
        raise ValueError("worker not in frozen feature plan")
    folder = root / "feature-workers" / args.worker
    folder.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    with (folder / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            model, _, _ = load_checkpoint(root / "initial.pt", device=args.device, expected_sha256=BASE_SHA)
            model.eval().requires_grad_(False)
            identity = policy_digest(model)
            if identity != plan["policy_sha256"]:
                raise ValueError("frozen policy differs")
            with tempfile.TemporaryDirectory(dir=args.scratch, prefix="fair-feature-audit-") as temporary:
                audit = verify_reference(model, connection, plan, Path(temporary))
            atomic_write(folder / "audit.json", json.dumps(audit).encode(), replace=True)
            while True:
                memory = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()}
                payload = dict(worker=args.worker, policy_sha256=identity,
                               healthy=memory["MemAvailable"] >= 32 * 2**20
                               and shutil.disk_usage(args.scratch).free >= 5 * 2**30)
                assignment = request(connection, "/feature-claim", payload)
                if assignment.get("finished"):
                    atomic_write(folder / "done.json", json.dumps(dict(time=time.time())).encode(), replace=True)
                    return 0
                job = assignment.get("job")
                if job is None:
                    time.sleep(10)
                    continue
                started = time.time()
                atomic_write(folder / "progress.json", json.dumps(dict(episode_id=job["episode_id"],
                             stage="encoding", started=started)).encode(), replace=True)
                existing = root / "features" / (job["episode_id"] + ".pt")
                if job.get("reuse_sha256"):
                    data = request(connection, "/feature-cache/" + job["episode_id"], raw=True)
                    if hashlib.sha256(data).hexdigest() != job["reuse_sha256"]:
                        raise ValueError("reused feature checksum differs")
                elif existing.exists() and existing.with_suffix(".json").exists():
                    load_cache(existing)
                    data = existing.read_bytes()
                else:
                    with tempfile.TemporaryDirectory(dir=args.scratch, prefix="fair-feature-") as temporary:
                        destination = Path(temporary)
                        marker = download_episode(connection, job, destination)
                        cache_episode(model, marker, destination / "cache.pt", expected_policy_sha=identity)
                        data = (destination / "cache.pt").read_bytes()
                receipt = request(connection, "/feature", data=data, headers={
                    "X-Episode-ID": job["episode_id"], "X-Feature-Worker": args.worker})
                if receipt["sha256"] != hashlib.sha256(data).hexdigest():
                    raise ValueError("feature receipt checksum differs")
                atomic_write(folder / "progress.json", json.dumps(dict(episode_id=job["episode_id"],
                             stage="accepted", seconds=time.time() - started, ended=time.time())).encode(), replace=True)
        except Exception as error:
            record = dict(worker=args.worker, error=repr(error), traceback=traceback.format_exc())
            atomic_write(folder / "failed.json", json.dumps(record).encode(), replace=True)
            request(connection, "/feature-failure", record)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
