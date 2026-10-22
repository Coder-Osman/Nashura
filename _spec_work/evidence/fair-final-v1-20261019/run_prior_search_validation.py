import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE
from verify_prior_history import validate_prior_history


ROOT = Path(__file__).resolve().parent
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
JOBS = [dict(name=f"stage-{threshold}-seat-{owner}", threshold=threshold, owner=owner,
    ports=[29652+index*2, 29653+index*2])
    for index, (threshold, owner) in enumerate((threshold, owner) for threshold in (1200, 3600) for owner in (0, 1))]


def main():
    validate_prior_history()
    run_id = f"native-prior-search-{time.time_ns()}"
    output = ROOT/run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-prior-search/"+run_id
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
import hashlib,json,shutil,socket
root=Path({remote!r})
with ExitStack() as stack:
 for port in {[port for job in JOBS for port in job['ports']]!r}:
  check=stack.enter_context(socket.socket());check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
probe=Path({BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
engine_sha=hashlib.sha256(probe.read_bytes()).hexdigest()
if available<64*1024**3 or free<5*1024**3 or engine_sha!={ENGINE_SHA!r}:raise RuntimeError('prior search preflight failed')
root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True);(root/'project/tools').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256=engine_sha)
(root/'preflight.json').write_text(json.dumps(preflight,indent=2));print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        print(stdout.read().decode(), flush=True)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        worker, prior = ROOT/"prior_search_worker.py", ROOT/"public-deck-prior-engineering.json"
        settings = dict(root=remote, base=BASE, jobs=JOBS, engine_sha256=ENGINE_SHA)
        with client.open_sftp() as sftp:
            paths = list((SOURCE/"native_runner/training/fair_final").glob("*.py"))
            paths.extend(SOURCE/"tools"/name for name in ("probe_fair_final_prior_search.py",
                "probe_fair_final_extended_search.py", "probe_fair_final_visibility.py"))
            for path in paths:
                relative, content = str(path.relative_to(SOURCE)), path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote+"/project/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/project/"+relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("prior search uploaded source differs")
            uploads = {"project/run.py":worker.read_bytes(), prior.name:prior.read_bytes(),
                "project/probe-source-manifest.json":json.dumps(identities, sort_keys=True).encode(),
                "run-manifest.json":json.dumps(settings).encode()}
            for relative, content in uploads.items():
                with sftp.open(remote+"/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/"+relative, "rb") as handle:
                    if handle.read() != content:
                        raise ValueError("prior search uploaded payload differs")
        attempt = dict(remote=remote, jobs=JOBS, source_sha256=identities,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
            prior_file_sha=hashlib.sha256(prior.read_bytes()).hexdigest(),
            horizon="default-six-groups-200-ticks")
        (output/"attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", remote=remote, output=str(output), jobs=JOBS)), flush=True)
        command = "cd "+shlex.quote(remote+"/project")+" && "+shlex.join([PYTHON, "run.py", remote+"/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=4200)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output/"attempt.json").write_text(json.dumps(dict(attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            names = [name for name in sftp.listdir(remote) if name.endswith((".json", ".log"))]
            names.extend(("project/run.py", "project/probe-source-manifest.json"))
            for job in JOBS:
                names.extend(job["name"]+"/"+name for name in sftp.listdir(remote+"/"+job["name"])
                    if name.endswith((".json", ".log")))
            for name in names:
                destination = output/name
                destination.parent.mkdir(exist_ok=True, parents=True)
                sftp.get(remote+"/"+name, str(destination))
        for job in JOBS:
            path = output/job["name"]/"probe.log"
            print(job["name"], path.read_text()[-3500:] if path.exists() else "probe not started", flush=True)
        if code:
            print(errors, flush=True)
            raise SystemExit(code)
        shards = [dict(job=job, report=json.loads((output/job["name"]/"report.json").read_text()),
            cleanup=json.loads((output/job["name"]/"cleanup.json").read_text())) for job in JOBS]
        if any(not shard["cleanup"]["clean"] for shard in shards):
            raise ValueError("prior search cleanup incomplete")
        report = dict(schema="fair-final-prior-search-suite.v1", status="passed", training_admissible=False,
            **attempt, shards=shards, artifacts={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()})
        (ROOT/"native-prior-search-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", roots=8, effective_simulations=1024)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
