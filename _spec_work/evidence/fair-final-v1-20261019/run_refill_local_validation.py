from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shlex
import stat
import sys
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE
from verify_local_reconditioning import validate_phase_engine_build
from verify_refill import validate_refill_trajectory


ROOT = Path(__file__).resolve().parent
BUILD = ROOT/"phase-snapshot-engine-1792485763481586000"
INPUT = ROOT/"native-refill-trajectory-1792489561748498000/trajectory"
JOBS = [dict(name=f"tick-{tick}-seat-{owner}", tick=tick, owner=owner,
    history=f"history-{tick}-{owner}.json", next_history=f"next-history-{tick}-{owner}.json", port=29701+index*2+owner)
    for index, tick in enumerate((110, 2598, 5602)) for owner in (0, 1)]


def successor_histories():
    sys.path.insert(0, str(SOURCE))
    from native_runner.training.fair_final.history import FairHistory, OwnDecision, PublicDeployment
    from native_runner.training.fair_final.policy import FullAction
    trace_path = INPUT.parent/"project/input-trace.json"
    trace = json.loads(trace_path.read_text())
    outputs = {}
    for job in JOBS:
        history = FairHistory.from_payload(json.loads((INPUT/job["history"]).read_text()))
        current = next(row for row in trace["boundaries"] if row["tick"] == history.root_tick)
        following = next(row for row in trace["boundaries"] if row["tick"] > history.root_tick)
        owner = str(history.opening.owner)
        if current["fair_sha"][owner] != history.root_fair_sha:
            raise ValueError("archived history and successor source differ")
        deployments = tuple(sorted(PublicDeployment(row["tick"], row["owner"], row["card_id"],
            *(int(value) for value in row["position"]))
            for tick in trace["ticks"] if history.root_tick < tick["tick"] <= following["tick"]
            for row in tick["public_executions"]))
        successor = replace(history, root_tick=following["tick"], root_fair_sha=following["fair_sha"][owner],
            decisions=(*history.decisions, OwnDecision(history.root_tick, history.root_fair_sha,
                FullAction(**current["actions"][owner]))), deployments=(*history.deployments, *deployments))
        if not successor.extends(history):
            raise ValueError("generated successor breaks FAIR prefix")
        outputs[job["next_history"]] = json.dumps(successor.to_payload(), sort_keys=True).encode()
    return outputs


def main():
    validate_refill_trajectory()
    successors = successor_histories()
    engine_manifest = json.loads((BUILD/"build-manifest.json").read_text())
    engine_sha = validate_phase_engine_build(engine_manifest)
    run_id = f"native-refill-local-{time.time_ns()}"
    output = ROOT/run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-refill-local/"+run_id
    fields = [field.strip() for field in next(line for line in (WORKSPACE/"MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f'''
from pathlib import Path
from contextlib import ExitStack
import json,shutil,socket
root=Path({remote!r})
with ExitStack() as stack:
 for port in {[job['port'] for job in JOBS]!r}:
  check=stack.enter_context(socket.socket());check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<64*1024**3 or free<5*1024**3:raise RuntimeError('refill local preflight failed')
root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True);(root/'project/tools').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256={engine_sha!r})
(root/'preflight.json').write_text(json.dumps(preflight,indent=2));print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        print(stdout.read().decode(), flush=True)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        worker = ROOT/"refill_local_worker.py"
        settings = dict(root=remote, base=BASE, jobs=JOBS, engine_sha256=engine_sha, engine_path=remote+"/libcrprobe.so")
        with client.open_sftp() as sftp:
            paths = list((SOURCE/"native_runner/training/fair_final").glob("*.py"))
            paths.extend((SOURCE/"native_runner/training_capture.py", SOURCE/"tools/probe_fair_final_refill_local.py"))
            for path in paths:
                relative, content = str(path.relative_to(SOURCE)), path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote+"/project/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/project/"+relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("refill local deployed source differs")
            uploads = {"project/run.py": worker.read_bytes(),
                **{"project/"+job["history"]: (INPUT/job["history"]).read_bytes() for job in JOBS},
                **{"project/"+name: content for name, content in successors.items()},
                "project/public-deck-prior-engineering.json": (ROOT/"public-deck-prior-engineering.json").read_bytes(),
                "project/probe-source-manifest.json": json.dumps(identities, sort_keys=True).encode(),
                "run-manifest.json": json.dumps(settings).encode(), "libcrprobe.so": (BUILD/"libcrprobe.so").read_bytes(),
                "engine-build-manifest.json": (BUILD/"build-manifest.json").read_bytes(),
                "project/phase_snapshot_launcher.py": (ROOT/"phase_snapshot_launcher.py").read_bytes()}
            for relative, content in uploads.items():
                with sftp.open(remote+"/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/"+relative, "rb") as handle:
                    if handle.read() != content:
                        raise ValueError("refill local payload differs")
        attempt = dict(remote=remote, jobs=JOBS, source_sha256=identities, engine_sha256=engine_sha,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
            payload_sha256={name: hashlib.sha256(content).hexdigest() for name, content in uploads.items()},
            purpose="refill-unseen-relabeling-and-real-history-successor")
        (output/"attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", output=str(output), remote=remote, jobs=JOBS)), flush=True)
        command = "cd "+shlex.quote(remote+"/project")+" && "+shlex.join([PYTHON, "run.py", remote+"/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=1500)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output/"attempt.json").write_text(json.dumps(dict(attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            def collect(relative=""):
                for entry in sftp.listdir_attr(remote+("/"+relative if relative else "")):
                    name = (relative+"/" if relative else "")+entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        collect(name)
                    elif name.endswith((".json", ".log")):
                        destination = output/name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        sftp.get(remote+"/"+name, str(destination))
            collect()
        for job in JOBS:
            path = output/job["name"]/"probe.log"
            print(job["name"], path.read_text()[-2500:] if path.exists() else "probe not started", flush=True)
        if code:
            print(errors, flush=True)
            raise SystemExit(code)
        shards = [dict(job=job, report=json.loads((output/job["name"]/"report.json").read_text()),
            cleanup=json.loads((output/job["name"]/"cleanup.json").read_text())) for job in JOBS]
        if any(not shard["cleanup"]["clean"] or shard["report"]["status"] != "passed" for shard in shards):
            raise ValueError("refill local native suite failed or leaked engines")
        report = dict(schema="fair-final-refill-local-suite.v1", status="passed", training_admissible=False,
            **attempt, shards=shards, artifacts={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()})
        (ROOT/"native-refill-local-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", shards=len(shards))), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
