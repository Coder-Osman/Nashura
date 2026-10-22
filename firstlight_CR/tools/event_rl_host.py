"""Persistent host supervisor; JSON lines on stdin/stdout, one worker per port."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import traceback
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def read_reply(process, timeout):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError("collector response deadline exceeded")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"collector closed its output (exit={process.poll()})")
    return json.loads(line)


def stop_process(process):
    if process is None:
        return
    if process.stdin:
        process.stdin.close()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.stdout:
        process.stdout.close()


class Host:
    def __init__(self, config):
        self.config = config
        self.processes = {}
        self.inference_process = None
        self.inference_client = None
        self.inference_directory = None
        self.inference_socket = None
        self.logs = Path(config["run"]) / "collector-workers"
        self.logs.mkdir(exist_ok=True)
        code = Path(config["code"])
        manifest = json.loads(Path(config["manifest"]).read_text())
        for name, digest in manifest.items():
            if hashlib.sha256((code / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"collector code hash mismatch: {name}")

    def prepare_inference(self, weights, *, histories=(), round_id=None):
        device = self.config.get("inference_device")
        if device is None:
            return
        from native_runner.training.event_inference_service import Client
        if self.inference_process is None:
            self.inference_directory = tempfile.TemporaryDirectory(prefix="cr-infer-", dir=self.config.get("inference_temp_directory", "/tmp"))
            self.inference_socket = str(Path(self.inference_directory.name) / "service.sock")
            code = Path(self.config["code"])
            env = {**os.environ, "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
            with (self.logs / "inference.stderr.log").open("a") as log:
                self.inference_process = subprocess.Popen(
                    [str(code / ".venv/bin/python"), "-u", "-m", "native_runner.training.event_inference_service",
                     self.inference_socket, "--device", device, "--max-batch",
                     str(self.config.get("inference_max_batch", 16)), "--wait-ms",
                     str(self.config.get("inference_wait_ms", 2.))],
                    cwd=code, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, bufsize=1)
            if not read_reply(self.inference_process, 90).get("ready"):
                raise RuntimeError("inference service did not become ready")
            self.inference_client = Client(self.inference_socket)
        elif self.inference_process.poll() is not None:
            raise RuntimeError("inference service exited")
        # collect() is synchronous: all previous matches have completed before
        # swapping weights; each request also carries the exact artifact identity.
        if round_id is None:
            self.inference_client.request("load", descriptor=weights)
        else:
            descriptors = {d["source_checkpoint_sha256"]: d for d in [*histories, weights]}
            self.inference_client.request("load_set", descriptors=list(descriptors.values()))
            self.inference_client.request("begin_round", round_id=round_id)

    def start(self, port):
        code = Path(self.config["code"])
        env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""}
        with (self.logs / f"port-{port}.stderr.log").open("a") as log:
            process = subprocess.Popen(
                [str(code / ".venv/bin/python"), "-u", str(code / "tools/event_rl_worker.py"), str(port)],
                cwd=code, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                text=True, bufsize=1)
        self.processes[port] = process
        return process

    def worker(self, task):
        port = task["port"]
        attempts = []
        for attempt in range(3):
            suffix = uuid.uuid4().hex if task.get("evaluation") else str(attempt)
            current = {**task, "out": str(Path(task["iteration_dir"]) / f"port-{port}-attempt-{suffix}")}
            process = self.processes.get(port)
            if process is None or process.poll() is not None:
                stop_process(process)
                process = self.start(port)
            try:
                process.stdin.write(json.dumps(current) + "\n")
                process.stdin.flush()
                result = read_reply(process, 2000)
                if not result.get("success"):
                    raise RuntimeError(result.get("error", "worker failed"))
                if (result["seed"] != task["seed"] or result["port"] != port or
                        result["source_checkpoint_sha256"] != task["weights"]["source_checkpoint_sha256"]):
                    raise ValueError("worker returned a different task identity")
                if result.get("mixed_opponents") != task.get("mixed_opponents"):
                    raise ValueError("worker returned a different mixed opponent identity")
                result["attempts"] = attempts
                return result
            except Exception:
                attempts.append(dict(attempt=attempt, seed=task["seed"], out=current["out"], error=traceback.format_exc()))
                stop_process(process)
                self.processes.pop(port, None)
        return dict(port=port, success=False, attempts=attempts)

    def evaluate(self, task):
        from native_runner.training.mixed_evaluation import validate_spec, match_metadata
        descriptors = task["descriptors"]
        for sha, descriptor in descriptors.items():
            if (sha != descriptor["source_checkpoint_sha256"] or
                    hashlib.sha256(Path(descriptor["path"]).read_bytes()).hexdigest() != descriptor["artifact_sha256"]):
                raise ValueError("evaluation inference artifact mismatch")
        if not task["jobs"] or len({j["port"] for j in task["jobs"]}) != len(task["jobs"]):
            raise ValueError("evaluation ports must be unique within a wave")
        first, *others = descriptors.values()
        round_id = "eval-" + task["wave_id"]
        self.prepare_inference(first, histories=others, round_id=round_id)
        jobs = []
        for job in task["jobs"]:
            spec = validate_spec(job["spec"])
            if job["port"] not in self.config["ports"]:
                raise ValueError("evaluation port outside configured fleet")
            directory = Path(self.config["run"]) / "evaluation-jobs" / spec["sha256"]
            directory.mkdir(parents=True, exist_ok=True)
            jobs.append(dict(port=job["port"], seed=spec["seed"], iteration_dir=str(directory),
                weights=descriptors[spec["candidate_sha256"]], opponent_weights=descriptors[spec["opponent_sha256"]],
                evaluation=spec, mixed_opponents=match_metadata(spec, job["port"]),
                inference_socket=self.inference_socket, sampling_protocol="per-match-cpu-uniform4.v2",
                capture_mode=self.config.get("capture_mode", "json"),
                defer_observations=self.config.get("defer_observations", False)))
        with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
            results = list(pool.map(self.worker, jobs))
        if self.inference_client:
            self.inference_client.request("end_round", round_id=round_id)
        if not all(r["success"] for r in results):
            raise RuntimeError(f"evaluation worker failed: {results}")
        return dict(success=True, results=results, wave_id=task["wave_id"])

    def collect(self, task):
        root = Path(self.config["run"])
        if shutil.disk_usage(root).free <= 1_500_000_000:
            raise RuntimeError("collector disk space guard")
        iteration_dir = root / f"iteration-{task['iteration']:06d}"
        iteration_dir.mkdir(exist_ok=True)
        with (iteration_dir / "collection-job.json").open("x") as stream:
            json.dump(task, stream)
        weights = task["weights"]
        if hashlib.sha256(Path(weights["path"]).read_bytes()).hexdigest() != weights["artifact_sha256"]:
            raise ValueError("host inference artifact SHA mismatch")
        mixed_plan = task.get("match_plan")
        histories = task.get("historical_weights", {})
        round_id = None if mixed_plan is None else mixed_plan["sha256"]
        if mixed_plan is not None:
            from native_runner.training.event_opponents import validate_plan, match_metadata
            validate_plan(mixed_plan)
            if mixed_plan["iteration"] != task["iteration"] or mixed_plan["current_sha256"] != weights["source_checkpoint_sha256"]:
                raise ValueError("host plan identity mismatch")
            matches = [m for m in mixed_plan["matches"] if m["host"] == task["host"]]
            if {m["port"] for m in matches} != set(self.config["ports"]):
                raise ValueError("host plan port mismatch")
            for descriptor in histories.values():
                if descriptor["contract_sha256"] != weights["contract_sha256"] or hashlib.sha256(
                    Path(descriptor["path"]).read_bytes()).hexdigest() != descriptor["artifact_sha256"]:
                    raise ValueError("host historical inference artifact mismatch")
        self.prepare_inference(weights, histories=list(histories.values()), round_id=round_id)
        jobs = [dict(task, port=port, iteration_dir=str(iteration_dir),
                     inference_socket=self.inference_socket,
                     sampling_protocol=self.config.get("sampling_protocol"),
                     capture_mode=self.config.get("capture_mode", "json"),
                     defer_observations=self.config.get("defer_observations", False),
                     seed=task["seed_base"] + task["iteration"] * 100000 + int(task["host"]) * 1000 + slot)
                for slot, port in enumerate(self.config["ports"])]
        if mixed_plan is not None:
            by_port = {m["port"]: m for m in matches}
            for job in jobs:
                match = by_port[job["port"]]
                job.update(seed=match["seed"], mixed_opponents=match_metadata(mixed_plan, match),
                           opponent_weights=None if match["opponent"] is None else histories[match["opponent"]["sha256"]])
        with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
            results = list(pool.map(self.worker, jobs))
        if round_id is not None and self.inference_client:
            self.inference_client.request("end_round", round_id=round_id)
        (iteration_dir / "host-results.json").write_text(json.dumps(results))
        if not all(r["success"] for r in results):
            raise RuntimeError("host has exhausted collector retries; see host-results.json")
        ready = iteration_dir / "ready"
        ready.mkdir()
        for result in results:
            target = ready / f"port-{result['port']}"
            target.mkdir()
            for name, digest in result["files"].items():
                source = Path(result["out"]) / name
                if source.name != name or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                    raise ValueError("worker shard integrity failure")
                os.link(source, target / name)
        (ready / "results.json").write_text(json.dumps(results))
        return dict(success=True, ready=str(ready), results=results)

    def close(self):
        with concurrent.futures.ThreadPoolExecutor(max(1, len(self.processes))) as pool:
            list(pool.map(stop_process, self.processes.values()))
        self.processes.clear()
        if self.inference_client:
            # Workers are gone; service termination cannot strand a match.
            self.inference_client.close()
        if self.inference_process:
            self.inference_process.terminate()
            stop_process(self.inference_process)
        if self.inference_directory:
            self.inference_directory.cleanup()


def main():
    host = Host(json.loads(sys.argv[1]))
    try:
        for line in sys.stdin:
            try:
                task = json.loads(line)
                result = host.evaluate(task) if task.get("operation") == "evaluate" else host.collect(task)
                print(json.dumps(result), flush=True)
            except BaseException:
                print(json.dumps({"success": False, "error": traceback.format_exc()}), flush=True)
                return 1
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
