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
HISTORY = ROOT.with_name("fair-final-history-v2")
COMMON = ROOT.with_name("fair-final-legal-reservations-v1")
sys.path.insert(0, str(WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"))
spec = importlib.util.spec_from_file_location("history_native_refill", HISTORY / "run_native_refill.py")
history_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history_runner)


def main():
    identities = history_runner.source_manifest()
    engine_sha = history_runner.validate_phase_engine_build(json.loads(
        (history_runner.BUILD / "build-manifest.json").read_text()))
    run_id = "native-pocket-collision-" + str(time.time_ns())
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-pocket-collision/" + run_id
    port = 29744
    settings = dict(root=remote, base=history_runner.BASE, port=port, engine_sha256=engine_sha)
    uploads = {"project/" + name: (HISTORY / "project" / name).read_bytes() for name in identities}
    uploads.update({"project/probe.py": (ROOT / "probe.py").read_bytes(),
        "project/legal_probe.py": (COMMON / "probe.py").read_bytes(),
        "project/run.py": (COMMON / "worker.py").read_bytes(),
        "project/input-trace.json": history_runner.TRACE.read_bytes(),
        "project/phase_snapshot_launcher.py": (history_runner.EVIDENCE / "phase_snapshot_launcher.py").read_bytes(),
        "libcrprobe.so": (history_runner.BUILD / "libcrprobe.so").read_bytes(),
        "run-manifest.json": json.dumps(settings).encode()})
    for name in ("probe.py", "legal_probe.py"):
        identities[name] = hashlib.sha256(uploads["project/" + name]).hexdigest()
    uploads["project/probe-source-manifest.json"] = json.dumps(identities, sort_keys=True).encode()
    fields = [value.strip() for value in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f'''
import json,shutil,socket
from pathlib import Path
with socket.socket() as connection: connection.bind(('127.0.0.1',{port}))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<64*1024**3 or free<5*1024**3:raise RuntimeError('bounded pocket probe resource reserve lost')
root=Path({remote!r});root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
record=dict(memory_available=available,free_bytes=free,formal_storage_gate_passed=False)
(root/'preflight.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))
'''
        _, stdout, stderr = client.exec_command(shlex.join([history_runner.PYTHON, "-c", preparation]), timeout=30)
        preflight = stdout.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        with client.open_sftp() as sftp:
            directories = {remote, remote + "/project"}
            for relative, content in uploads.items():
                destination = remote + "/" + relative
                for parent in reversed(Path(destination).parents):
                    name = str(parent)
                    if name.startswith(remote) and name not in directories:
                        sftp.mkdir(name)
                        directories.add(name)
                with sftp.open(destination, "wb") as handle:
                    handle.write(content)
                with sftp.open(destination, "rb") as handle:
                    if hashlib.sha256(handle.read()).digest() != hashlib.sha256(content).digest():
                        raise ValueError("pocket probe uploaded content differs")
        attempt = dict(schema="fair-final-pocket-collision-attempt.v1", remote=remote,
            engine_sha=engine_sha, port=port, preflight=json.loads(preflight), training_admissible=False,
            runner_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            history_manifest_sha=hashlib.sha256((HISTORY / "version-manifest.json").read_bytes()).hexdigest(),
            payload_sha={name: hashlib.sha256(content).hexdigest() for name, content in uploads.items()})
        (output / "attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", output=str(output), port=port)), flush=True)
        command = "cd " + shlex.quote(remote + "/project") + " && " + shlex.join([
            history_runner.PYTHON, "-u", "run.py", remote + "/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=1450)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        attempt.update(exit_code=code, stdout=collected, stderr=errors)
        (output / "attempt.json").write_text(json.dumps(attempt, indent=2))
        with client.open_sftp() as sftp:
            def collect(relative=""):
                directory = remote + ("/" + relative if relative else "")
                for entry in sftp.listdir_attr(directory):
                    name = (relative + "/" if relative else "") + entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        collect(name)
                    elif name.endswith((".json", ".log")):
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
