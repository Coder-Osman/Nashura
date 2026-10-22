"""Standard-library client for the two fail-stop persistent learner workers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from tools.event_rl_host import read_reply, stop_process


class PersistentLearner:
    def __init__(self, fleet, checkpoint, *, device="cuda:0", log_directory=None):
        self.fleet = fleet
        self.code = fleet.ROOT / fleet.CFG.get("learner_code", "code")
        manifest_name = fleet.CFG.get("learner_manifest")
        if manifest_name:
            manifest = json.loads((fleet.ROOT / manifest_name).read_text())
            for name, digest in manifest.items():
                if hashlib.sha256((self.code / name).read_bytes()).hexdigest() != digest:
                    raise ValueError(f"learner code hash mismatch: {name}")
        self.processes = {}
        self.failed = False
        directory = log_directory or fleet.ROOT / "learner-sessions" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        self.log_directory = directory
        options = fleet.CFG.get("learner_options", {})
        argv = ["--cache-root", str(fleet.ROOT), "--checkpoint", str(checkpoint),
                "--output", str(directory / "unused.pt"), "--max-shards",
                str(sum(len(host["ports"]) for host in fleet.FLEET.values())),
                "--epochs", "1", "--lr", "0.00001", "--device", device,
                "--minibatch-lanes", "0", "--target-kl", "0.01",
                "--sequence-chunk-steps", "32", "--bc-coefficient", "0", "--kl-coefficient", "0",
                "--expected-temperatures", "1,1,1,1", "--row-batch-size",
                str(options.get("row_batch_size", 128)), "--input-cache-mib",
                str(options.get("input_cache_mib", 512))]
        if options.get("component_gae") is not None:
            from native_runner.training.event_component_gae import ComponentGaeConfig, COMPONENTS
            gae = ComponentGaeConfig.from_dict(options["component_gae"])
            argv += ["--component-gae", "--gamma", str(gae.gamma)]
            for name in COMPONENTS:
                argv += ["--" + name + "-gae-lambda", str(getattr(gae, name + "_lambda"))]
        if fleet.CFG.get("mixed_opponents", {}).get("version") == "mixed-deck.v1":
            mixed = fleet.CFG["mixed_opponents"]
            argv += ["--mixed-deck-config", str(fleet.ROOT / "mixed-deck.json"),
                     "--reference-checkpoint", str(fleet.ROOT / "weights" / (mixed["baseline"]["sha256"] + ".pt")),
                     "--kl-coefficient", ".01", "--kl-target", ".02", "--kl-min", ".0001", "--kl-max", ".1"]
        config = directory / "config.json"
        config.write_text(json.dumps(dict(argv=argv, checkpoint=str(checkpoint),
                                         inference_directory=str(fleet.ROOT / "collector-weights"))))
        try:
            # Spawn together: the verifier warms its independent CPU model while
            # the learner initializes its GPU model. No training overlaps rollout.
            for mode in ("learner", "verifier"):
                with (directory / (mode + ".stderr.log")).open("x") as log:
                    self.processes[mode] = subprocess.Popen(
                        [str(self.code / ".venv/bin/python"), "-u", "tools/event_rl_learner.py", mode, str(config)],
                        cwd=self.code, env=fleet.ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=log, text=True, bufsize=1)
            ready = {mode: read_reply(process, 180) for mode, process in self.processes.items()}
            for mode, value in ready.items():
                if not value.get("ready") or value["checkpoint_sha256"] != checkpoint.stem:
                    raise ValueError(f"{mode} startup identity mismatch")
            if ready["learner"]["contract_sha256"] != ready["verifier"]["contract_sha256"]:
                raise ValueError("learner/verifier contract mismatch")
            self.current = ready["verifier"]
            self.descriptor = ready["learner"]["descriptor"]
            self.prepare_iteration(checkpoint)
            fleet.event("learner-ready", workers=ready, log_directory=str(directory))
        except BaseException:
            self.close()
            raise

    def _request(self, mode, task):
        process = self.processes[mode]
        if process.poll() is not None:
            raise RuntimeError(f"{mode} worker exited")
        process.stdin.write(json.dumps(task) + "\n")
        process.stdin.flush()
        response = read_reply(process, 1800)
        if response.get("request_id") != task["request_id"] or not response.get("success"):
            raise RuntimeError(f"{mode} rejected request: {response}")
        return response

    def prepare_iteration(self, weight):
        if self.failed:
            raise RuntimeError("learner failed; restart from a verified checkpoint")
        d = self.descriptor
        if weight.stem != self.current["checkpoint_sha256"] or d["source_checkpoint_sha256"] != weight.stem:
            raise ValueError("stale persistent inference artifact")
        if d["contract_sha256"] != self.current["contract_sha256"]:
            raise ValueError("persistent inference contract mismatch")
        if hashlib.sha256(Path(d["path"]).read_bytes()).hexdigest() != d["artifact_sha256"]:
            raise ValueError("persistent inference artifact SHA mismatch")
        return dict(d)

    def update(self, cache, weight, out, shards, log):
        if self.failed:
            raise RuntimeError("learner failed; restart from a verified checkpoint")
        try:
            if weight.stem != self.current["checkpoint_sha256"]:
                raise ValueError("learner parent identity mismatch")
            if shards != sum(len(host["ports"]) for host in self.fleet.FLEET.values()):
                raise ValueError("learner shard count changed")
            task = dict(request_id=uuid.uuid4().hex, iteration=self.current["iterations"]+1,
                        parent_checkpoint_sha256=weight.stem, checkpoint=str(weight),
                        output=str(out), cache_root=str(cache), log=str(log))
            started = time.perf_counter()
            update = self._request("learner", task)
            records = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
            if not any(r.get("event") == "event-preflight" and r.get("passed") for r in records):
                raise ValueError("missing successful real-rollout preflight")
            stats = next(r for r in records if r.get("event") == "ppo-update")
            if update["optimizer_steps"] != 1 or stats["optimizer_steps"] != 1:
                raise ValueError("expected exactly one optimizer step per fleet round")
            if stats["steps"] != update["decision_rows"] or update["iteration"] != task["iteration"]:
                raise ValueError("learner response/log mismatch")
            verified = self._request("verifier", {**task,
                **{key: update[key] for key in ("checkpoint_sha256", "optimizer_steps", "decision_rows")}})
            child = self.fleet.publish(out)
            if child.stem != verified["checkpoint_sha256"]:
                raise ValueError("published checkpoint identity mismatch")
            self.current = verified
            self.descriptor = update["descriptor"]
            self.prepare_iteration(child)
            self.fleet.event("update-verified", records=records, learner="persistent.v1",
                             learner_pid=update["pid"], verifier_pid=verified["pid"],
                             update_seconds=update["update_seconds"], export_seconds=update["export_seconds"],
                             verify_seconds=verified["seconds"], total_seconds=time.perf_counter()-started,
                             checkpoint_sha256=child.stem, iteration=task["iteration"])
            return child
        except BaseException:
            self.failed = True
            self.close()
            raise

    def close(self):
        for process in self.processes.values():
            stop_process(process)
        self.processes.clear()
