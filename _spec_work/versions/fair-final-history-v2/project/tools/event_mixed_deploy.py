"""Stage an isolated four-deck run, then hand over at a complete PPO boundary."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import time


def checked(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def remote(host, command, **kwargs):
    if host == "34":
        return checked(["bash", "-c", command], **kwargs)
    key = "firstlight-worker-link" if host in ("35", "37") else "firstlight-capture-link"
    return checked(["ssh", "-i", str(Path.home() / ".ssh" / key), "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20",
                    "-o", "ServerAliveInterval=30", f"zhichun@172.16.48.{host}", command], **kwargs)


def local_stage(source, parent, name, audit):
    audit.mkdir(parents=True, exist_ok=True)
    archive = audit / "code-overlay.tar.gz"
    files = sorted(p for folder in ("native_runner", "tools") for p in (source / folder).rglob("*.py")
                   if "__pycache__" not in p.parts)
    with tarfile.open(archive, "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=str(path.relative_to(source)), recursive=False)
    overlay = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (audit / "overlay-manifest.json").write_text(json.dumps(overlay, indent=2))
    remote_audit = parent + "/mixed-deck-handoff-20260920"
    checked(["ssh", "-o", "BatchMode=yes", "gujiga@172.16.48.34", "mkdir -p " + shlex.quote(remote_audit)])
    for path in (archive, audit / "overlay-manifest.json", Path(__file__).resolve()):
        checked(["scp", "-q", str(path), "gujiga@172.16.48.34:" + remote_audit + "/" + path.name])
    command = shlex.join(["python3", remote_audit + "/event_mixed_deploy.py", "remote-stage",
                         "--parent", parent, "--name", name, "--audit", remote_audit])
    checked(["ssh", "-o", "BatchMode=yes", "gujiga@172.16.48.34", command])


def remote_stage(parent, name, audit):
    cfg = json.loads((parent / "fleet.json").read_text())
    root = parent.parent / name
    if root.exists():
        raise ValueError("new mixed run directory already exists")
    source = parent / cfg["learner_code"]
    root.mkdir()
    for folder in ("weights", "match-plans", "evaluations"):
        (root / folder).mkdir()
    shutil.copytree(source, root / "code", symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    with tarfile.open(audit / "code-overlay.tar.gz") as tar:
        tar.extractall(root / "code", filter="data")
    overlay = json.loads((audit / "overlay-manifest.json").read_text())
    for path, sha in overlay.items():
        if hashlib.sha256((root / "code" / path).read_bytes()).hexdigest() != sha:
            raise ValueError("staged overlay SHA mismatch")
    manifest = {str(p.relative_to(root / "code")): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ("native_runner", "tools") for p in (root / "code" / folder).rglob("*")
                if p.is_file() and p.suffix in (".py", ".json") and "__pycache__" not in p.parts}
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    shutil.copy2(root / "code/tools/event_rl_fleet_resume.py", root / "fleet_runtime.py")
    fleet = {h: dict(value, parent_run=value["run"], run=str(Path(value["run"]).parent / name))
             for h, value in cfg["fleet"].items()}
    prepared = dict(parent_run=str(parent), new_run=str(root), fleet=fleet, original_config=cfg,
                    manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest())
    (audit / "staging.json").write_text(json.dumps(prepared, indent=2))

    def stage_host(h):
        if h == "34":
            return dict(host=h, free_bytes=shutil.disk_usage(root).free)
        value = fleet[h]
        target = Path(value["run"])
        script = ("from pathlib import Path; import json,shutil; r=Path(" + repr(str(target)) + "); "
                  "assert not r.exists(); r.mkdir(); (r/'weights').mkdir(); (r/'code').mkdir(); "
                  "print(json.dumps({'host':" + repr(h) + ",'free_bytes':shutil.disk_usage(r).free}))")
        response = remote(h, "python3 -c " + shlex.quote(script), capture_output=True, text=True, timeout=30)
        key = "firstlight-worker-link" if h in ("35", "37") else "firstlight-capture-link"
        ssh = shlex.join(["ssh", "-i", str(Path.home() / ".ssh" / key), "-o", "BatchMode=yes"])
        checked(["rsync", "-a", "-e", ssh, str(root / "code") + "/",
                 f"zhichun@172.16.48.{h}:" + str(target / "code") + "/"], timeout=120)
        checked(["rsync", "-a", "-e", ssh, str(root / "manifest.json"),
                 f"zhichun@172.16.48.{h}:" + str(target / "manifest.json")], timeout=60)
        # Runtime assets live at each host's own base, not at the learner's path.
        links = {name: str(Path(value["base"]) / name) for name in (".venv", "build", "checkpoints", "user-provided")}
        fix = """from pathlib import Path
import json,hashlib
r=Path(ROOT); links=LINKS
for name,target in links.items():
 p=r/'code'/name
 if p.is_symlink(): p.unlink()
 elif p.exists(): raise ValueError('unexpected non-link runtime asset')
 p.symlink_to(target, target_is_directory=True)
m=json.loads((r/'manifest.json').read_text())
assert all(hashlib.sha256((r/'code'/p).read_bytes()).hexdigest()==sha for p,sha in m.items())
""".replace("ROOT", repr(str(target))).replace("LINKS", repr(links))
        remote(h, "python3 -c " + shlex.quote(fix), timeout=45)
        return json.loads(response.stdout)

    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        hosts = list(pool.map(stage_host, fleet))
    prepared["hosts"] = hosts
    (audit / "staging.json").write_text(json.dumps(prepared, indent=2))
    print(json.dumps(dict(event="staged", **prepared)), flush=True)


def alive(pid):
    path = Path("/proc") / str(pid) / "stat"
    return path.exists() and path.read_text().split()[2] != "Z"


def activate(parent, audit):
    staged = json.loads((audit / "staging.json").read_text())
    root = Path(staged["new_run"])
    lock = (audit / "handoff.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def state(event, **kwargs):
        value = dict(event=event, time=time.time(), **kwargs)
        temporary = audit / "handoff-status.tmp"
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(audit / "handoff-status.json")
        print(json.dumps(value), flush=True)

    cfg = json.loads((parent / "fleet.json").read_text())
    if cfg != staged["original_config"]:
        raise ValueError("parent configuration changed after staging")
    if not json.loads((audit / "tests-passed.json").read_text())["passed"]:
        raise ValueError("local verification gate not passed")
    old_pids = [int((parent / name).read_text()) for name in ("orchestrator.pid", "pipeline.pid")]
    state("waiting-for-complete-round", old_pids=old_pids)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = json.loads((parent / "status.json").read_text())
        if status["event"] == "update-start" and 0 <= time.time() - status["time"] < 10:
            request = dict(reason="four-deck mixed RL handoff", finish_iteration=status["iteration"], time=time.time())
            with (parent / "STOP").open("x") as stream:
                json.dump(request, stream)
            (audit / "pause-request.json").write_text(json.dumps(request, indent=2))
            state("finishing-current-round", **{k: v for k, v in request.items() if k != "time"})
            break
        if status["event"] in ("fatal", "stopped", "completed"):
            raise RuntimeError(f"parent is not running: {status}")
        time.sleep(.25)
    else:
        raise TimeoutError("parent round boundary timeout")
    deadline = time.monotonic() + 600
    while any(alive(pid) for pid in old_pids):
        if time.monotonic() > deadline:
            raise TimeoutError("parent did not exit at complete boundary")
        time.sleep(.5)
    latest = json.loads(sorted(parent.glob("iteration-*/success.json"))[-1].read_text())
    status = json.loads((parent / "status.json").read_text())
    if latest["iteration"] != request["finish_iteration"] or status.get("phase") != "before-collection":
        raise ValueError("handoff is not at the expected completed round boundary")
    baseline = dict(iteration=latest["iteration"], sha256=Path(latest["checkpoint"]).stem)
    fixed = [dict(iteration=n, sha256=Path(json.loads((parent / f"iteration-{n:06d}/success.json").read_text())["checkpoint"]).stem)
             for n in (600, 645)]
    os.link(latest["checkpoint"], root / "weights" / (baseline["sha256"] + ".pt"))
    archive_root = Path(staged["fleet"]["37"]["run"])
    old_archive = Path(cfg["fleet"]["37"]["run"])
    copy = """from pathlib import Path
import hashlib,os
old=Path(OLD); new=Path(NEW)
for sha in SHAS:
 p=old/'weights'/(sha+'.pt'); q=new/'weights'/(sha+'.pt')
 assert hashlib.sha256(p.read_bytes()).hexdigest()==sha
 if not q.exists(): os.link(p,q)
""".replace("OLD", repr(str(old_archive))).replace("NEW", repr(str(archive_root))).replace(
        "SHAS", repr([baseline["sha256"], *(r["sha256"] for r in fixed)]))
    remote("37", "python3 -c " + shlex.quote(copy), timeout=90)
    mixed = dict(version="mixed-deck.v1", start_iteration=baseline["iteration"], baseline=baseline,
                 fixed=fixed, action_constraints={"delay": {"minimum_ms": 100}})
    new = dict(cfg, fleet=staged["fleet"], parent_run=str(parent), initial_checkpoint_sha256=baseline["sha256"],
               source_checkpoint=latest["checkpoint"], baseline_checkpoint_sha256=baseline["sha256"],
               start_iteration=baseline["iteration"], start_optimizer_steps=baseline["iteration"],
               training_mode="bounded", additional_iterations=100, target_iteration=baseline["iteration"] + 100,
               duration_seconds=None, seed_base=2180920000, mixed_opponents=mixed,
               collector_code="code", learner_code="code", evaluation_code="code",
               collector_manifest="manifest.json", learner_manifest="manifest.json", evaluation_manifest="manifest.json",
               evaluation=dict(enabled=True, interval=10), mode="Four-deck mixed PPO, 100 updates, protected Golem")
    new["collector_options"] = dict(cfg["collector_options"], sampling_protocol="per-match-cpu-uniform4.v2")
    (root / "fleet.json").write_text(json.dumps(new, indent=2))
    (root / "mixed-deck.json").write_text(json.dumps(mixed, indent=2))
    (root / "B0.json").write_text(json.dumps(dict(**baseline, source=latest, original_preserved=True), indent=2))
    (root / "last-qualified.json").write_text(json.dumps(dict(mixed_round=0, checkpoint_sha256=baseline["sha256"])))
    state("starting-native-smoke-and-baseline", B0=baseline, run=str(root))
    with (root / "training.log").open("x") as log:
        process = subprocess.Popen(["python3", "-u", str(root / "code/tools/event_mixed_train.py"), "--run-root", str(root)],
            cwd=root, env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    state("activated", B0=baseline, run=str(root), orchestrator_pid=process.pid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("local-stage", "remote-stage", "activate"))
    parser.add_argument("--parent", required=True)
    parser.add_argument("--name", default="event-mixed-deck-100r-20260920")
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "local-stage":
        local_stage(Path(__file__).resolve().parents[1], args.parent, args.name, args.audit)
    elif args.mode == "remote-stage":
        remote_stage(Path(args.parent), args.name, args.audit)
    else:
        activate(Path(args.parent), args.audit)


if __name__ == "__main__":
    main()
