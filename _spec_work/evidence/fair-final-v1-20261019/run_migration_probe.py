import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def main():
    attempt = "native-migration-" + str(time.time_ns())
    evidence = Path(__file__).parent / attempt
    evidence.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-migration/" + attempt
    fields = [value.strip() for value in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        files = [path for path in (SOURCE / "native_runner").rglob("*.py")
            if "tests" not in path.relative_to(SOURCE).parts]
        files.append(SOURCE / "tools/collect_fair_final_migration.py")
        identities = {str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            for path in files:
                archive.add(path, arcname=str(path.relative_to(SOURCE)), recursive=False)
        (evidence / "source-manifest.json").write_text(json.dumps(identities, sort_keys=True, indent=2))
        (evidence / "launch.json").write_text(json.dumps(dict(remote=remote, engine_sha256=ENGINE_SHA,
            source_checkpoint_sha256=SOURCE_SHA, payload_sha256=hashlib.sha256(payload.getvalue()).hexdigest(),
            worker_sha256=hashlib.sha256(Path(__file__).with_name("migration_probe_worker.py").read_bytes()).hexdigest(),
            training_admissible=False), indent=2))
        preparation = f"""
from pathlib import Path
import json,shutil,socket
root=Path({remote!r})
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if available<32*1024**3: raise RuntimeError('insufficient available memory')
if shutil.disk_usage(root.parent.parent).free<5*1024**3: raise RuntimeError('insufficient probe disk')
with socket.socket() as check: check.bind(('127.0.0.1',29721))
root.mkdir(parents=True,exist_ok=False)
shutil.copytree({BASE + '/project/native_runner'!r},root/'project/native_runner',ignore=shutil.ignore_patterns('__pycache__','tests','*.py'))
(root/'project/tools').mkdir()
print(json.dumps(dict(available_gib=available/1024**3,disk_free_gib=shutil.disk_usage(root).free/1024**3)))
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=90)
        resources, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        print(resources, flush=True)
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/source.tar.gz", "wb") as output:
                output.write(payload.getvalue())
            sftp.put(str(Path(__file__).with_name("migration_probe_worker.py")), remote + "/worker.py")
            with sftp.open(remote + "/project/source-manifest.json", "w") as output:
                output.write(json.dumps(identities, sort_keys=True))
        deployment = f"""
import tarfile
from pathlib import Path
root=Path({remote!r})
with tarfile.open(root/'source.tar.gz') as archive: archive.extractall(root/'project',filter='data')
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", deployment]), timeout=60)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        command = [PYTHON, "-u", remote + "/worker.py", "--root", remote, "--base", BASE,
            "--engine-sha", ENGINE_SHA, "--source-sha", SOURCE_SHA]
        _, stdout, stderr = client.exec_command(shlex.join(command), timeout=4500)
        with (evidence / "worker.log").open("w") as output:
            for line in stdout:
                output.write(line)
                output.flush()
                print(line, end="", flush=True)
        errors = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (evidence / "worker-stderr.log").write_text(errors)
        (evidence / "completion.json").write_text(json.dumps(dict(exit_code=code, remote=remote), indent=2))
        print(errors, flush=True)
        with client.open_sftp() as sftp:
            for name in ("native-migration-report.json", "engine-cleanup.json", "manifest.json", "status.json"):
                try:
                    sftp.get(remote + "/" + name, str(evidence / name))
                except FileNotFoundError:
                    pass
            for index in range(3):
                for name in (f"collector-{index}.log", f"engine-{index}.log"):
                    try:
                        sftp.get(remote + "/" + name, str(evidence / name))
                    except FileNotFoundError:
                        pass
        raise SystemExit(code)
    finally:
        client.close()


if __name__ == "__main__":
    main()
