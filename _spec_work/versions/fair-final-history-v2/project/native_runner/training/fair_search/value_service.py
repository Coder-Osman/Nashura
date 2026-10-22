import argparse
from concurrent.futures import ProcessPoolExecutor
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

import torch

from .storage import atomic_write
from .value_data import BASE_SHA, decode_episode, publish_episode
from .value_capacity import actor_limit, capacity_config
from .value_features import FeatureCoordinator


HOSTS = ("34", "35", "37", "39", "41")


def validate_upload(root, data, jobs, runtime_sha256):
    episode = decode_episode(data)
    job = jobs.get(episode.episode_id)
    if job is None or job["state"] not in ("running", "complete"):
        raise ValueError("unassigned completed episode")
    locks = Path(root) / "upload-locks"
    locks.mkdir(exist_ok=True)
    with (locks / (episode.episode_id + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = publish_episode(Path(root) / "episodes" / episode.episode_id, data,
                                  expected_job=job, model_sha256=BASE_SHA,
                                  runtime_sha256=runtime_sha256)
    return receipt, episode.attempt


class ValueCoordinator:
    def __init__(self, root, executor=None):
        self.root = Path(root)
        self.executor = executor
        self.uploads = {}
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        self.lock = threading.RLock()
        self.state_path = self.root / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = dict(schema="fair-value-coordinator.v1", started=time.time(), stage=1,
                              stage_since=time.time(), hosts={}, jobs={
                                  job["episode_id"]: dict(**job, attempt=0, state="pending")
                                  for job in self.manifest["jobs"]})
        self.save()

    def save(self):
        atomic_write(self.state_path, json.dumps(self.state, sort_keys=True).encode(), replace=True)

    def counts(self):
        return {state: sum(job["state"] == state for job in self.state["jobs"].values())
                for state in ("pending", "running", "complete", "blocked")}

    def heartbeat(self, payload):
        with self.lock:
            capacity = capacity_config(self.root)
            host = payload["host"]
            if host not in HOSTS:
                raise ValueError("unknown host")
            previous = self.state["hosts"].get(host, {})
            swapping = (payload.get("pswpin", 0) > previous.get("pswpin", payload.get("pswpin", 0))
                        or payload.get("pswpout", 0) > previous.get("pswpout", payload.get("pswpout", 0)))
            swap_streak = previous.get("swap_streak", 0) + 1 if swapping else 0
            healthy = payload["available_gb"] >= 32 and payload["disk_gb"] >= 5 and swap_streak < 3
            healthy_since = previous.get("healthy_since", time.time()) if healthy and previous.get("healthy") else time.time()
            current = {**payload, "healthy": healthy, "healthy_since": healthy_since,
                       "swap_streak": swap_streak, "updated": time.time(),
                       "blocked": previous.get("blocked", False),
                       "actor_failures": previous.get("actor_failures", 0)}
            self.state["hosts"][host] = current
            counts = self.counts()
            if self.state["stage"] == 1 and counts["complete"] >= 32 and counts["running"] == 0:
                completed = [job for job in self.state["jobs"].values() if job["state"] == "complete"]
                projected = sum(job["bytes"] for job in completed) / len(completed) * 4096 * 1.25
                central_ok = shutil.disk_usage(self.root).free >= projected + 5 * 2**30
                self.state["projected_dataset_bytes"] = projected
                self.state["storage_admitted"] = central_ok
                if (all(self.state["hosts"].get(name, {}).get("healthy")
                        and not self.state["hosts"][name]["blocked"]
                        and time.time() - self.state["hosts"][name]["updated"] < 30 for name in HOSTS)
                        and central_ok and not capacity["drain_hosts"]):
                    self.advance_stage(capacity)
            elif time.time() - self.state["stage_since"] >= capacity["observation_seconds"]:
                if all(self.state["hosts"].get(name, {}).get("healthy")
                       and time.time() - self.state["hosts"][name]["updated"] < 30
                       and time.time() - self.state["hosts"][name]["healthy_since"] >= capacity["observation_seconds"]
                       and not self.state["hosts"][name]["blocked"] for name in HOSTS) and not capacity["drain_hosts"]:
                    self.advance_stage(capacity)
            self.save()
            slots = actor_limit(capacity, host, self.state["stage"])
            if not healthy or current["blocked"]:
                slots = 0
            return dict(slots=slots, counts=counts, stage=self.state["stage"],
                        finished=counts["complete"] == len(self.state["jobs"]))

    def advance_stage(self, capacity):
        stages = [stage for stage in capacity["stages"] if stage > self.state["stage"]]
        if not stages:
            return
        completed = self.counts()["complete"]
        if self.state["stage"] > 1:
            baseline = self.state.get("stage_host_completed", {})
            if any(sum(job["state"] == "complete" and job.get("host") == host
                       for job in self.state["jobs"].values()) <= baseline.get(host, 0) for host in HOSTS):
                return
            failures = len(list((self.root / "failures").glob("*.json")))
            if failures > self.state.get("stage_failures", 0):
                return
        elapsed = max(1., time.time() - self.state["stage_since"])
        self.state.setdefault("capacity_history", []).append(dict(
            stage=self.state["stage"], seconds=elapsed, complete=completed,
            games_per_hour=(completed - self.state.get("stage_completed", 0)) * 3600 / elapsed,
            ended=time.time()))
        self.state.update(stage=stages[0], stage_since=time.time(), stage_completed=completed,
                          stage_failures=len(list((self.root / "failures").glob("*.json"))),
                          stage_host_completed={host: sum(job["state"] == "complete" and job.get("host") == host
                                                         for job in self.state["jobs"].values()) for host in HOSTS})

    def claim(self, payload):
        with self.lock:
            host, slot = payload["host"], payload["slot"]
            if host not in HOSTS or type(slot) is not int or slot < 0:
                raise ValueError("invalid actor")
            health = self.state["hosts"].get(host, {})
            if (not health.get("healthy") or health.get("blocked")
                    or time.time() - health.get("updated", 0) > 30
                    or slot >= actor_limit(capacity_config(self.root), host, self.state["stage"])):
                return dict(job=None)
            if shutil.disk_usage(self.root).free < 5 * 2**30:
                return dict(job=None, reason="central-disk-reserve")
            for job in self.state["jobs"].values():
                if job["state"] == "running" and (job["host"], job["slot"]) == (host, slot):
                    return dict(job=job)
            for ordinal, job in enumerate(self.state["jobs"].values()):
                if self.state["stage"] == 1:
                    if ordinal >= 32:
                        break
                    if HOSTS[ordinal % len(HOSTS)] != host:
                        continue
                if job["state"] == "pending":
                    job.update(state="running", host=host, slot=slot, started=time.time())
                    self.save()
                    return dict(job=job)
            return dict(job=None, finished=self.counts()["complete"] == len(self.state["jobs"]))

    def complete(self, data):
        identity = hashlib.sha256(data).hexdigest()
        with self.lock:
            jobs = {key: dict(job) for key, job in self.state["jobs"].items()
                    if job["state"] in ("running", "complete")}
            arguments = self.root, data, jobs, self.manifest["runtime_sha256"]
            future = self.uploads.get(identity)
            if self.executor is not None and future is None:
                future = self.executor.submit(validate_upload, *arguments)
                self.uploads[identity] = future
        try:
            receipt, attempt = validate_upload(*arguments) if future is None else future.result()
        finally:
            with self.lock:
                self.uploads.pop(identity, None)
        with self.lock:
            job = self.state["jobs"][receipt["episode_id"]]
            if job["state"] not in ("running", "complete") or job["attempt"] != attempt:
                raise ValueError("upload assignment changed during validation")
            job.update(state="complete", ended=time.time(), rows=receipt["rows"], bytes=receipt["bytes"])
            self.save()
            return receipt

    def failure(self, payload):
        with self.lock:
            reported = payload["job"]
            job = self.state["jobs"][reported["episode_id"]]
            if job["state"] == "complete":
                return dict(state="complete")
            if (job["attempt"], job["host"], job["slot"]) != (reported["attempt"], payload["host"], payload["slot"]):
                raise ValueError("failure task identity differs")
            atomic_write(self.root / "failures" / f"{job['episode_id']}-{job['attempt']}.json",
                         json.dumps(payload).encode(), replace=True)
            retry = payload.get("transient") is True and job["attempt"] < 2
            job.update(state="pending" if retry else "blocked", last_error=payload["error"],
                       attempt=job["attempt"] + 1)
            if not retry:
                self.state["hosts"][job["host"]]["blocked"] = True
            self.save()
            return dict(state=job["state"])

    def actor_exit(self, payload):
        with self.lock:
            host, slot = payload["host"], payload["slot"]
            running = [job for job in self.state["jobs"].values()
                       if job["state"] == "running" and (job["host"], job["slot"]) == (host, slot)]
            if running:
                job = dict(running[0])
                return self.failure(dict(job=job, host=host, slot=slot, transient=True,
                                         error="actor exited without receipt", exit_code=payload["exit_code"]))
            if payload["exit_code"] != 0:
                host_state = self.state["hosts"][host]
                host_state["actor_failures"] = host_state.get("actor_failures", 0) + 1
                if host_state["actor_failures"] >= 3:
                    host_state["blocked"] = True
                self.save()
            return dict(recorded=True)

    def resume_hosts(self, payload):
        with self.lock:
            hosts = payload["hosts"]
            if not hosts or not set(hosts) <= set(HOSTS) or not payload.get("reason"):
                raise ValueError("invalid explicit recovery")
            if any(job["state"] == "blocked" and job.get("host") in hosts
                   for job in self.state["jobs"].values()):
                raise ValueError("blocked tasks require individual repair")
            failures = list((self.root / "failures").glob("*.json"))
            for path in failures:
                record = json.loads(path.read_text())
                if record.get("host") in hosts and record.get("transient") is not True:
                    raise ValueError("protocol failure requires individual repair")
            for host in hosts:
                health = self.state["hosts"][host]
                if not health["healthy"] or time.time() - health["updated"] > 30:
                    raise ValueError("host health is stale or insufficient")
            for host in hosts:
                self.state["hosts"][host].update(blocked=False, actor_failures=0, healthy_since=time.time())
            self.state.update(stage_since=time.time(), stage_failures=len(failures),
                              stage_completed=self.counts()["complete"],
                              stage_host_completed={host: sum(job["state"] == "complete" and job.get("host") == host
                                                             for job in self.state["jobs"].values()) for host in HOSTS})
            record = dict(hosts=hosts, reason=payload["reason"], time=time.time(), failures=len(failures))
            self.state.setdefault("recoveries", []).append(record)
            self.save()
            return record

    def status(self):
        with self.lock:
            counts = self.counts()
            complete = [job for job in self.state["jobs"].values() if job["state"] == "complete"]
            elapsed = max(1., time.time() - self.state["started"])
            return dict(counts=counts, stage=self.state["stage"], hosts=self.state["hosts"],
                        capacity=capacity_config(self.root), capacity_history=self.state.get("capacity_history", []),
                        games_per_hour=len(complete) * 3600 / elapsed,
                        rows=sum(job["rows"] for job in complete),
                        bytes=sum(job["bytes"] for job in complete),
                        failures=len(list((self.root / "failures").glob("*.json"))),
                        blocked=[job for job in self.state["jobs"].values() if job["state"] == "blocked"])


def serve(root, port):
    torch.set_num_threads(1)
    feature_plan = json.loads((root / "feature-plan.json").read_text()) if (root / "feature-plan.json").exists() else {}
    validation_workers = feature_plan.get("validation_workers", 8)
    if type(validation_workers) is not int or not 1 <= validation_workers <= 32:
        raise ValueError("feature validation worker count must be between 1 and 32")
    executor = ProcessPoolExecutor(max_workers=validation_workers, mp_context=multiprocessing.get_context("spawn"),
                                   initializer=torch.set_num_threads, initargs=(1,))
    coordinator = ValueCoordinator(root, executor=executor)
    features = FeatureCoordinator(root, executor) if (root / "feature-plan.json").exists() else None
    connection = json.loads((root / "connection.json").read_text())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + connection["token"]):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 512 * 2**20:
                    raise ValueError("invalid upload size")
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("incomplete request body")
                if self.path == "/complete":
                    result = coordinator.complete(data)
                elif self.path == "/feature":
                    episode_id = self.headers.get("X-Episode-ID", "")
                    result = features.complete(data, episode_id, self.headers.get("X-Feature-Worker", ""))
                elif self.path == "/feature-claim":
                    result = features.claim(json.loads(data))
                elif self.path == "/feature-failure":
                    result = features.failure(json.loads(data))
                elif self.path == "/feature-status":
                    result = features.status()
                elif self.path == "/status":
                    result = coordinator.status()
                else:
                    operation = {"/claim": coordinator.claim, "/heartbeat": coordinator.heartbeat,
                                 "/failure": coordinator.failure, "/actor-exit": coordinator.actor_exit,
                                 "/resume-hosts": coordinator.resume_hosts}.get(self.path)
                    if operation is None:
                        raise ValueError("unknown route")
                    result = operation(json.loads(data))
                encoded = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as error:
                print(json.dumps(dict(route=self.path, error=repr(error))), flush=True)
                self.send_error(400, "value protocol validation failed")

        def do_GET(self):
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + connection["token"]):
                self.send_error(403)
                return
            prefixes = {"/episode/": "episode.pt.gz", "/episode-marker/": "completed.json",
                        "/feature-cache/": None}
            prefix = next((prefix for prefix in prefixes if self.path.startswith(prefix)), None)
            if prefix is None:
                self.send_error(404)
                return
            episode_id = self.path[len(prefix):]
            if episode_id not in coordinator.state["jobs"]:
                self.send_error(404)
                return
            path = (root / "features" / (episode_id + ".pt") if prefix == "/feature-cache/"
                    else root / "episodes" / episode_id / prefixes[prefix])
            if not path.exists():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def pipeline():
        while True:
            atomic_write(root / "status.json", json.dumps(coordinator.status()).encode(), replace=True)
            training_active = root / "training" / "features-distributed.active"
            if training_active.exists():
                feature_status = features.status() if features is not None else None
                if feature_status is not None:
                    atomic_write(root / "training" / "feature-progress.json", json.dumps(feature_status).encode(), replace=True)
                if feature_status is None or feature_status["counts"]["complete"] != 4096 or feature_status["failures"]:
                    time.sleep(10)
                    continue
                atomic_write(root / "training" / "features-distributed.done.json", json.dumps(feature_status).encode(), replace=True)
            if coordinator.counts()["complete"] == 4096 and time.time() - coordinator.state["stage_since"] >= 600:
                training = root / "training"
                training.mkdir(exist_ok=True)
                if not (training / "done.json").exists():
                    if not (root / "engineering-tests.json").exists():
                        raise ValueError("engineering acceptance missing")
                    with (training / "train.log").open("a") as log:
                        result = subprocess.run([sys.executable, "-u", "-m",
                                                 "native_runner.training.fair_search.value_train",
                                                 "--root", str(root), "--device", "cuda:7"],
                                                cwd=root / "project", stdout=log, stderr=subprocess.STDOUT)
                    if result.returncode:
                        atomic_write(training / "failed.json", json.dumps(dict(exit_code=result.returncode)).encode(), replace=True)
                        return
                result = json.loads((training / "done.json").read_text())
                if (root / "value-evaluation.override.json").exists():
                    return
                if result["acceptance"]["passed"]:
                    with (training / "search-evaluation.log").open("a") as log:
                        result = subprocess.run([sys.executable, "-u", "-m",
                                                 "native_runner.training.fair_search.value_evaluate",
                                                 "--root", str(root)], cwd=root / "project",
                                                stdout=log, stderr=subprocess.STDOUT)
                    atomic_write(training / "search-evaluation-exit.json", json.dumps(dict(exit_code=result.returncode)).encode(), replace=True)
                return
            time.sleep(10)

    threading.Thread(target=pipeline, daemon=True).start()

    def stop(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    finally:
        executor.shutdown(wait=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=29180)
    args = parser.parse_args(argv)
    with (args.root / "coordinator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        serve(args.root, args.port)


if __name__ == "__main__":
    main()
