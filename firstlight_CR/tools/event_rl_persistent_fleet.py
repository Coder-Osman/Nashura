"""Persistent collection adapter for the existing synchronous fleet learner."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time

from tools.event_rl_host import read_reply, stop_process


class PersistentFleet:
    def __init__(self, fleet):
        self.fleet = fleet
        self.processes = {}
        self.descriptor = None
        self.code = fleet.ROOT / fleet.CFG.get("collector_code", "collector-code")
        self.lock = threading.Lock()
        self.plan = None
        self.histories = {}

    def prepare_iteration(self, weight, descriptor=None, *, iteration=None):
        start = time.perf_counter()
        if descriptor is None:
            result = self.fleet.checked(
                [str(self.code / ".venv/bin/python"), "-m", "native_runner.training.event_inference",
                 str(weight), str(self.fleet.ROOT / "collector-weights")],
                cwd=self.code, env={**self.fleet.ENV, "CUDA_VISIBLE_DEVICES": ""},
                capture_output=True, text=True)
            descriptor = json.loads(result.stdout)
        self.descriptor = dict(descriptor)
        if self.descriptor["source_checkpoint_sha256"] != weight.stem:
            raise ValueError("export source identity mismatch")
        if hashlib.sha256(Path(self.descriptor["path"]).read_bytes()).hexdigest() != self.descriptor["artifact_sha256"]:
            raise ValueError("export artifact SHA mismatch")
        self.fleet.event("inference-export", **self.descriptor, seconds=time.perf_counter()-start)
        if self.fleet.CFG.get("mixed_opponents"):
            self.prepare_opponents(weight, iteration=iteration)

    def prepare_opponents(self, weight, *, iteration):
        from native_runner.training.event_opponents import VERSION, checkpoint_records, select_pool, freeze_plan
        f = self.fleet
        mixed = f.CFG["mixed_opponents"]
        if mixed["version"] not in (VERSION, "mixed-deck.v1") or iteration is None:
            raise ValueError("mixed opponent policy or iteration missing")
        records = checkpoint_records(f.ROOT)
        if mixed["version"] == "mixed-deck.v1":
            records += [mixed["baseline"]]
        if not any(r["iteration"] == iteration-1 and r["sha256"] == weight.stem for r in records):
            raise ValueError("current model is not the previous complete checkpoint")
        extra = {}
        if mixed["version"] == "mixed-deck.v1":
            from native_runner.training.mixed_decks import build_plan, select_pool as mixed_pool
            pool = mixed_pool(records, round_number=iteration - mixed["start_iteration"],
                              start_iteration=mixed["start_iteration"], baseline=mixed["baseline"], fixed=mixed["fixed"])
            extra = dict(builder=build_plan, start_iteration=mixed["start_iteration"],
                         action_constraints=mixed["action_constraints"], deck_policy=mixed.get("deck_policy"),
                         reward_config=mixed.get("reward_config"), allocation_policy=mixed.get("allocation_policy"))
        else:
            pool = select_pool(records, iteration-1, weight.stem)
        self.plan = freeze_plan(f.ROOT / "match-plans" / f"iteration-{iteration:06d}.json",
            iteration=iteration, seed_base=f.CFG["seed_base"], current_sha=weight.stem,
            pool=pool, fleet=f.FLEET, **extra)
        from native_runner.training.event_history_assets import (
            VERSION as PREPARATION_VERSION, probe_artifacts,
            validate_descriptor, artifact_path,
        )
        started = time.perf_counter()
        contract_sha = self.descriptor["contract_sha256"]
        selected = sorted({r["sha256"] for rows in self.plan["pool"].values() for r in rows})
        if weight.stem in selected and mixed["version"] != "mixed-deck.v1":
            raise ValueError("historical pool aliases current checkpoint")
        archive = self.history_request("37", dict(operation="archive",
            root=f.FLEET["37"]["run"], sources=selected, contract_sha=contract_sha))
        descriptors = archive["descriptors"]
        if set(descriptors) != set(selected):
            raise ValueError("archive response changed historical pool")
        for sha, descriptor in descriptors.items():
            validate_descriptor(descriptor, sha, contract_sha)
        archive_seconds = time.perf_counter() - started
        local = probe_artifacts(f.ROOT, descriptors, contract_sha, current_bytes=self.descriptor["bytes"])
        if local["missing"]:
            self.transfer_histories("37", descriptors, local["missing"], pull=True)
            probe_artifacts(f.ROOT, descriptors, contract_sha,
                            current_bytes=self.descriptor["bytes"], require_complete=True)
        self.histories = {sha: dict(d, path=str(artifact_path(f.ROOT, d))) for sha, d in descriptors.items()}
        local_seconds = time.perf_counter() - started - archive_seconds

        def prepare_host(h):
            begin = time.perf_counter()
            task = dict(operation="probe", root=f.FLEET[h]["run"], descriptors=self.histories,
                        contract_sha=contract_sha, current_bytes=self.descriptor["bytes"])
            response = self.history_request(h, task)
            missing = response["missing"]
            if len(set(missing)) != len(missing) or set(missing) - set(self.histories):
                raise ValueError("invalid host missing-artifact response")
            if missing:
                self.transfer_histories(h, self.histories, missing)
                self.history_request(h, dict(task, require_complete=True))
            return dict(host=h, transferred=len(missing), seconds=time.perf_counter()-begin)

        # Each host has an independent filesystem and SSH connection. Join the
        # entire preparation barrier before dispatching any collection job.
        hosts = [h for h in f.FLEET if h != "34"]
        with concurrent.futures.ThreadPoolExecutor(len(hosts)) as pool:
            prepared = list(pool.map(prepare_host, hosts))
        preparation = dict(version=PREPARATION_VERSION, seconds=time.perf_counter()-started,
            archive_seconds=archive_seconds, archive_exported=archive["exported"],
            local_seconds=local_seconds, local_transferred=len(local["missing"]), hosts=prepared)
        directory = f.ROOT / f"iteration-{iteration:06d}"
        directory.mkdir(exist_ok=True)
        (directory / "match-plan.json").write_text(json.dumps(self.plan, indent=2))
        f.event("mixed-plan-frozen", iteration=iteration, plan_sha256=self.plan["sha256"],
                policy_version=self.plan["version"], pool=self.plan["pool"],
                games=self.plan["expected_games"], expected_player_lanes=self.plan["expected_player_lanes"],
                history_preparation=preparation)

    def history_request(self, h, task):
        f = self.fleet
        code = Path(f.FLEET[h]["run"]) / f.CFG["collector_code"]
        command = "cd " + shlex.quote(str(code)) + " && " + shlex.join([
            "env", "OMP_NUM_THREADS=1", "MKL_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1", "CUDA_VISIBLE_DEVICES=",
            str(code / ".venv/bin/python"), "-m", "native_runner.training.event_history_assets", json.dumps(task)])
        result = f.ssh(h, command, capture_output=True, text=True, timeout=120)
        return json.loads(result.stdout)

    def transfer_histories(self, h, descriptors, missing, *, pull=False):
        """One rsync connection transfers only this host's missing SHA files."""
        if not missing:
            return
        from native_runner.training.event_history_assets import valid_sha
        names = sorted({valid_sha(descriptors[sha]["artifact_sha256"]) + ".pt" for sha in missing})
        f = self.fleet
        local = str(f.ROOT / "history-inference") + "/"
        remote = "zhichun@172.16.48." + h + ":" + str(Path(f.FLEET[h]["run"]) / "history-inference") + "/"
        key = "firstlight-worker-link" if h in ("35", "37") else "firstlight-capture-link"
        ssh = shlex.join(["ssh", "-i", str(Path.home() / ".ssh" / key),
                          "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20"])
        f.checked(["rsync", "-a", "--ignore-existing", "--files-from=-", "-e", ssh,
                   remote if pull else local, local if pull else remote],
                  input="\n".join(names) + "\n", text=True, timeout=120)

    def start(self, h):
        f = self.fleet
        host = f.FLEET[h]
        root = Path(host["run"])
        code = root / f.CFG.get("collector_code", "collector-code")
        config = dict(run=str(root), code=str(code), ports=host["ports"],
                      manifest=str(root / f.CFG.get("collector_manifest", "collector-manifest.json")))
        config.update(f.CFG.get("collector_options", {}))
        config.update(host.get("collector_options", {}))
        command = shlex.join([str(code / ".venv/bin/python"), "-u", str(code / "tools/event_rl_host.py"), json.dumps(config)])
        if h == "34":
            argv = ["bash", "-c", command]
        else:
            key = "firstlight-worker-link" if h in ("35", "37") else "firstlight-capture-link"
            argv = ["ssh", "-i", str(Path.home() / ".ssh" / key), "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20",
                    "-o", "ServerAliveInterval=30", f"zhichun@172.16.48.{h}", command]
        with (f.ROOT / f"collector-host-{h}.log").open("a") as log:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=log, text=True, bufsize=1)
        with self.lock:
            self.processes[h] = process
        return process

    def rollout_host(self, h, iteration, weight):
        started = time.perf_counter()
        f = self.fleet
        descriptor = dict(self.descriptor)
        if descriptor["source_checkpoint_sha256"] != weight.stem:
            raise ValueError("stale inference export")
        remote_root = Path(f.FLEET[h]["run"])
        remote_weights = remote_root / "collector-weights"
        if h != "34":
            f.ssh(h, "mkdir -p " + shlex.quote(str(remote_weights)), timeout=30)
            f.transport(h, descriptor["path"], str(remote_weights / Path(descriptor["path"]).name))
        descriptor["path"] = str(remote_weights / Path(descriptor["path"]).name)
        process = self.processes.get(h)
        if process is None:
            process = self.start(h)
        elif process.poll() is not None:
            raise RuntimeError("host supervisor exited; refuse to duplicate an uncertain iteration")
        task = dict(iteration=iteration, weights=descriptor, seed_base=f.CFG["seed_base"], host=h)
        if self.plan is not None:
            if self.plan["iteration"] != iteration or self.plan["current_sha256"] != weight.stem:
                raise ValueError("stale mixed round plan")
            task.update(match_plan=self.plan, historical_weights={sha: dict(d, path=str(
                remote_root / "history-inference" / Path(d["path"]).name)) for sha, d in self.histories.items()})
        process.stdin.write(json.dumps(task) + "\n")
        process.stdin.flush()
        response = read_reply(process, 6100)
        if not response.get("success"):
            raise RuntimeError(response.get("error", "host collector failed"))
        collection_done = time.perf_counter()
        target = f.ROOT / f"iteration-{iteration:06d}" / "collected" / ("host-" + h)
        target.parent.mkdir(parents=True, exist_ok=True)
        if h == "34":
            shutil.copytree(response["ready"], target)
        else:
            # One transfer per host, rather than 18/24 serial SSH handshakes.
            f.transport(h, response["ready"] + "/", str(target) + "/", pull=True)
        results = json.loads((target / "results.json").read_text())
        if results != response["results"] or {r["port"] for r in results} != set(f.FLEET[h]["ports"]):
            raise ValueError("collection manifest mismatch")
        for result in results:
            if not result["success"] or result["source_checkpoint_sha256"] != weight.stem:
                raise ValueError("collection behavior version mismatch")
            if self.plan is not None:
                from native_runner.training.event_opponents import match_metadata
                match = next(m for m in self.plan["matches"] if m["host"] == h and m["port"] == result["port"])
                if result.get("mixed_opponents") != match_metadata(self.plan, match) or result["seed"] != match["seed"]:
                    raise ValueError("collection mixed plan mismatch")
            for name, digest in result["files"].items():
                if Path(name).name != name:
                    raise ValueError("invalid shard filename")
                if hashlib.sha256((target / f"port-{result['port']}" / name).read_bytes()).hexdigest() != digest:
                    raise ValueError("transferred shard SHA mismatch")
        (target.parent.parent / f"host-{h}-barrier.json").write_text(json.dumps(results))
        f.event("host-barrier", host=h, results=results, collector="persistent.v1",
                collection_seconds=collection_done-started, transfer_seconds=time.perf_counter()-collection_done)
        return results

    def close(self):
        with concurrent.futures.ThreadPoolExecutor(max(1, len(self.processes))) as pool:
            list(pool.map(stop_process, self.processes.values()))
        self.processes.clear()
