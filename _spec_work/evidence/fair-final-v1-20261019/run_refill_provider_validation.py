import hashlib
import json
from pathlib import Path
import shlex
import stat
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE
from verify_local_reconditioning import validate_phase_engine_build
from verify_refill import validate_refill_trajectory


ROOT = Path(__file__).resolve().parent
BUILD = ROOT/"phase-snapshot-engine-1792485763481586000"
TRACE = ROOT/"native-refill-trajectory-1792489561748498000/project/input-trace.json"
PROBES = ("probe_fair_final_refill_provider.py", "probe_fair_final_local_search.py",
    "probe_fair_final_prior_search.py", "probe_fair_final_extended_search.py", "probe_fair_final_visibility.py")
JOBS = [dict(name=f"tick-{tick}-seat-{owner}", tick=tick, owner=owner,
    ports=[29711+index*2, 29712+index*2])
    for index, (tick, owner) in enumerate((tick, owner) for tick in (110, 2598, 5602) for owner in (0, 1))]


def source_manifest():
    paths = list((SOURCE/"native_runner/training/fair_final").glob("*.py"))
    paths.append(SOURCE/"native_runner/training_capture.py")
    paths.extend(SOURCE/"tools"/name for name in PROBES)
    return {str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def payloads(remote, engine_sha, identities):
    settings = dict(root=remote, base=BASE, jobs=JOBS, engine_sha256=engine_sha,
        engine_path=remote+"/libcrprobe.so")
    return {"project/run.py": (ROOT/"refill_provider_worker.py").read_bytes(),
        "project/input-trace.json": TRACE.read_bytes(),
        "project/public-deck-prior-engineering.json": (ROOT/"public-deck-prior-engineering.json").read_bytes(),
        "project/probe-source-manifest.json": json.dumps(identities, sort_keys=True).encode(),
        "run-manifest.json": json.dumps(settings).encode(), "libcrprobe.so": (BUILD/"libcrprobe.so").read_bytes(),
        "engine-build-manifest.json": (BUILD/"build-manifest.json").read_bytes(),
        "project/phase_snapshot_launcher.py": (ROOT/"phase_snapshot_launcher.py").read_bytes()}


def main():
    validate_refill_trajectory()
    engine_sha = validate_phase_engine_build(json.loads((BUILD/"build-manifest.json").read_text()))
    run_id = f"native-refill-provider-{time.time_ns()}"
    output = ROOT/run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-refill-provider/"+run_id
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
 for port in {[port for job in JOBS for port in job['ports']]!r}:
  check=stack.enter_context(socket.socket());check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<64*1024**3 or free<5*1024**3:raise RuntimeError('refill provider preflight failed')
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
        identities = source_manifest()
        uploads = payloads(remote, engine_sha, identities)
        with client.open_sftp() as sftp:
            for relative, digest in identities.items():
                with sftp.open(remote+"/project/"+relative, "wb") as handle:
                    handle.write((SOURCE/relative).read_bytes())
                with sftp.open(remote+"/project/"+relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != digest:
                        raise ValueError("refill provider uploaded source differs")
            for relative, content in uploads.items():
                with sftp.open(remote+"/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/"+relative, "rb") as handle:
                    if handle.read() != content:
                        raise ValueError("refill provider uploaded payload differs")
        attempt = dict(remote=remote, jobs=JOBS, source_sha256=identities, engine_sha256=engine_sha,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            worker_sha256=hashlib.sha256((ROOT/"refill_provider_worker.py").read_bytes()).hexdigest(),
            payload_sha256={name: hashlib.sha256(content).hexdigest() for name, content in uploads.items()},
            purpose="refill-local-Full128-hidden-draw-intervention-real-successor")
        (output/"attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", output=str(output), remote=remote, jobs=JOBS)), flush=True)
        command = "cd "+shlex.quote(remote+"/project")+" && "+shlex.join([PYTHON, "run.py", remote+"/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=6000)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output/"attempt.json").write_text(json.dumps(dict(attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            def collect(relative=""):
                for entry in sftp.listdir_attr(remote+("/"+relative if relative else "")):
                    name = (relative+"/" if relative else "")+entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        collect(name)
                    elif name.endswith((".json", ".log")) or name in ("project/run.py", "project/phase_snapshot_launcher.py"):
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
            raise ValueError("refill provider native suite failed or leaked engines")
        report = dict(schema="fair-final-refill-provider-suite.v1", status="passed", training_admissible=False,
            **attempt, shards=shards, artifacts={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()})
        (ROOT/"native-refill-provider-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", shards=len(shards), searches=24, simulations=3072)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
