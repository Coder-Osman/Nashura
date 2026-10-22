import hashlib
import json
from pathlib import Path
import shlex
import stat
import sys
import time

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"
sys.path.insert(0, str(EVIDENCE))

from run_native_probe import BASE, PYTHON
from verify_local_reconditioning import validate_phase_engine_build


JOBS = [dict(name="tick-110-seat-0", tick=110, owner=0, ports=[29741, 29742])]
BUILD = EVIDENCE / "phase-snapshot-engine-1792485763481586000"
TRACE = EVIDENCE / "native-refill-trajectory-1792489561748498000/project/input-trace.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    version = json.loads((ROOT / "version-manifest.json").read_text())
    paths = {name: digest for name, digest in version["source_files"].items()
        if name.startswith(("native_runner/", "tools/"))
        and "/tests/" not in name and not name.startswith("native_runner/probe/")}
    for name, expected in paths.items():
        if sha(ROOT / "project" / name) != expected:
            raise ValueError("isolated source identity changed")
    return paths


def payloads(remote, engine_sha, identities):
    settings = dict(root=remote, base=BASE, jobs=JOBS, engine_sha256=engine_sha,
        engine_path=remote + "/libcrprobe.so")
    return {"project/run.py": (EVIDENCE / "refill_provider_worker.py").read_bytes(),
        "project/input-trace.json": TRACE.read_bytes(),
        "project/public-deck-prior-engineering.json": (EVIDENCE / "public-deck-prior-engineering.json").read_bytes(),
        "project/probe-source-manifest.json": json.dumps(identities, sort_keys=True).encode(),
        "run-manifest.json": json.dumps(settings).encode(),
        "libcrprobe.so": (BUILD / "libcrprobe.so").read_bytes(),
        "engine-build-manifest.json": (BUILD / "build-manifest.json").read_bytes(),
        "project/phase_snapshot_launcher.py": (EVIDENCE / "phase_snapshot_launcher.py").read_bytes()}


def main():
    engine_sha = validate_phase_engine_build(json.loads((BUILD / "build-manifest.json").read_text()))
    identities = source_manifest()
    run_id = "native-history-v2-refill-" + str(time.time_ns())
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-history-v2/" + run_id
    fields = [value.strip() for value in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f'''
from contextlib import ExitStack
import json, shutil, socket
from pathlib import Path
root=Path({remote!r})
with ExitStack() as stack:
 for port in {[port for job in JOBS for port in job['ports']]!r}:
  connection=stack.enter_context(socket.socket());connection.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<64*1024**3 or free<5*1024**3:raise RuntimeError('bounded native diagnostic resource reserve lost')
root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256={engine_sha!r},formal_storage_gate_passed=False)
(root/'preflight.json').write_text(json.dumps(preflight,indent=2));print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=30)
        preflight = stdout.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        print(preflight, flush=True)
        uploads = payloads(remote, engine_sha, identities)
        with client.open_sftp() as sftp:
            directories = {remote, remote + "/project"}

            def upload(relative, content):
                destination = remote + "/" + relative
                for parent in reversed(Path(destination).parents):
                    name = str(parent)
                    if name.startswith(remote) and name not in directories:
                        sftp.mkdir(name)
                        directories.add(name)
                with sftp.open(destination, "wb") as handle:
                    handle.write(content)
                with sftp.open(destination, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != hashlib.sha256(content).hexdigest():
                        raise ValueError("uploaded diagnostic payload differs")

            for name in identities:
                upload("project/" + name, (ROOT / "project" / name).read_bytes())
            for name, content in uploads.items():
                upload(name, content)
        attempt = dict(schema="fair-final-history-v2-native-attempt.v1", remote=remote, jobs=JOBS,
            source_sha256=identities, engine_sha256=engine_sha, training_admissible=False,
            version_manifest_sha256=sha(ROOT / "version-manifest.json"),
            runner_sha256=sha(Path(__file__)), worker_sha256=sha(EVIDENCE / "refill_provider_worker.py"),
            payload_sha256={name: hashlib.sha256(content).hexdigest() for name, content in uploads.items()},
            purpose="history-v2-bounded-local-Full128-hidden-pairs-and-incremental-replay")
        (output / "attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", output=str(output), jobs=JOBS, source_files=len(identities))), flush=True)
        command = "cd " + shlex.quote(remote + "/project") + " && " + shlex.join([
            PYTHON, "-u", "run.py", remote + "/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=5800)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output / "attempt.json").write_text(json.dumps(dict(attempt, exit_code=code,
            stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            def collect(relative=""):
                directory = remote + ("/" + relative if relative else "")
                for entry in sftp.listdir_attr(directory):
                    name = (relative + "/" if relative else "") + entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        collect(name)
                    elif name.endswith((".json", ".log")) or name in (
                        "project/run.py", "project/phase_snapshot_launcher.py"):
                        destination = output / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        sftp.get(remote + "/" + name, str(destination))
            collect()
        print(json.dumps(dict(status="collected", exit_code=code, output=str(output))), flush=True)
        raise SystemExit(code)
    finally:
        client.close()


if __name__ == "__main__":
    main()
