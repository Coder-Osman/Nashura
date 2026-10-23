import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import stat
import sys
import time

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
HISTORY = ROOT.parent / "fair-final-history-v2"
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"
sys.path.insert(0, str(HISTORY / "project"))
sys.path.insert(0, str(EVIDENCE))

from run_native_probe import BASE, PYTHON
from verify_local_reconditioning import validate_phase_engine_build


DEPENDENCY = HISTORY / "run_native_refill.py"
SPEC = importlib.util.spec_from_file_location("frozen_history_refill_runner", DEPENDENCY)
refill_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refill_runner)
JOBS = [dict(name="tick-110-seat-0", tick=110, owner=0, ports=[29771, 29772])]


def digest(content):
    return hashlib.sha256(content).hexdigest()


def payloads(remote, engine_sha, identities):
    files = refill_runner.payloads(remote, engine_sha, identities)
    settings = json.loads(files["run-manifest.json"])
    settings["jobs"] = JOBS
    files["run-manifest.json"] = json.dumps(settings, sort_keys=True).encode()
    worker = files["project/run.py"].decode()
    target = "project/tools/probe_fair_final_refill_provider.py"
    if worker.count(target) != 1:
        raise ValueError("frozen worker probe entry identity differs")
    files["project/run.py"] = worker.replace(target, "project/probe_native_general.py").encode()
    for name in ("general_provider.py", "probe_native_general.py"):
        files["project/" + name] = (ROOT / name).read_bytes()
    return files


def main():
    engine_sha = validate_phase_engine_build(json.loads((refill_runner.BUILD / "build-manifest.json").read_text()))
    identities = refill_runner.source_manifest()
    identities.update({name: digest((ROOT / name).read_bytes())
        for name in ("general_provider.py", "probe_native_general.py")})
    run_id = "native-general-local-" + str(time.time_ns())
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-general-fallback-v1/" + run_id
    files = payloads(remote, engine_sha, identities)
    registration = dict(schema="fair-final-general-native-attempt.v1", remote=remote, jobs=JOBS,
        engine_sha256=engine_sha, source_sha256=identities, training_admissible=False,
        runner_sha256=digest(Path(__file__).read_bytes()),
        dependency_sha256=digest(DEPENDENCY.read_bytes()),
        history_version_sha256=digest((HISTORY / "version-manifest.json").read_bytes()),
        payload_sha256={name: digest(content) for name, content in files.items()},
        purpose="general-provider-native-local-hidden-pairs-incremental-Full128")
    (output / "attempt.json").write_text(json.dumps(registration, indent=2))
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
  stack.enter_context(socket.socket()).bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<64*1024**3 or free<5*1024**3:raise RuntimeError('native diagnostic reserve lost')
root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256={engine_sha!r},formal_storage_gate_passed=False)
(root/'preflight.json').write_text(json.dumps(preflight,indent=2));print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=30)
        preflight = stdout.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        print(preflight, flush=True)
        with client.open_sftp() as sftp:
            directories = {remote, remote + "/project"}
            def upload(name, content):
                destination = remote + "/" + name
                for parent in reversed(Path(destination).parents):
                    if str(parent).startswith(remote) and str(parent) not in directories:
                        sftp.mkdir(str(parent))
                        directories.add(str(parent))
                with sftp.open(destination, "wb") as handle:
                    handle.write(content)
                with sftp.open(destination, "rb") as handle:
                    if digest(handle.read()) != digest(content):
                        raise ValueError("uploaded native payload differs")
            for name in refill_runner.source_manifest():
                upload("project/" + name, (HISTORY / "project" / name).read_bytes())
            for name, content in files.items():
                upload(name, content)
        print(json.dumps(dict(status="running", output=str(output), jobs=JOBS)), flush=True)
        command = "cd " + shlex.quote(remote + "/project") + " && " + shlex.join([
            PYTHON, "-u", "run.py", remote + "/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=5800)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output / "attempt.json").write_text(json.dumps(dict(registration, exit_code=code,
            stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            def collect(relative=""):
                for entry in sftp.listdir_attr(remote + ("/" + relative if relative else "")):
                    name = (relative + "/" if relative else "") + entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        collect(name)
                    elif name.endswith((".json", ".log")) or name in files and name.endswith(".py"):
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
