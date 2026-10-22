import argparse
import hashlib
import json
from pathlib import Path
import shlex
import tarfile

import paramiko

from run_native_probe import BASE, PYTHON, WORKSPACE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    evidence = (root / args.attempt).resolve()
    if evidence.parent != root:
        raise ValueError("independent migration evidence directory required")
    launch = json.loads((evidence / "launch.json").read_text())
    remote = launch["remote"]
    fields = [value.strip() for value in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/status.json") as handle:
                if json.load(handle).get("status") != "passed":
                    raise ValueError("complete native migration collection required before update probe")
            probe = root / "migration_native_training_probe.py"
            sftp.put(str(probe), remote + "/training-probe.py")
        probe_sha = hashlib.sha256(probe.read_bytes()).hexdigest()
        bounded = f"""
import hashlib,json,subprocess
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'training-probe.py').read_bytes()).hexdigest()!={probe_sha!r}: raise ValueError('training probe upload changed')
with (root/'training-probe.log').open('w') as output:
 result=subprocess.run([{PYTHON!r},'-u',str(root/'training-probe.py'),'--root',str(root),'--source',{BASE + '/candidate.pt'!r}],stdout=output,stderr=subprocess.STDOUT,timeout=600)
print(json.dumps(dict(exit_code=result.returncode)),flush=True)
raise SystemExit(result.returncode)
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-u", "-c", bounded]), timeout=660)
        output, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        print(output + errors, flush=True)
        with client.open_sftp() as sftp:
            sftp.get(remote + "/training-probe.log", str(evidence / "training-probe.log"))
        if code:
            raise RuntimeError((evidence / "training-probe.log").read_text()[-6000:])
        package = f"""
import hashlib,json,tarfile
from pathlib import Path
root=Path({remote!r})
names=['data','manifest.json','frozen-tasks-private.json','engine-cleanup.json','native-migration-report.json',
 'native-migration-update-report.json','project/source-manifest.json','worker.py','training-probe.py','engineering-migration-update.pt']
names += ['engine-%d/process.json'%index for index in range(3)]
names += ['collector-%d.log'%index for index in range(3)]
with tarfile.open(root/'evidence.tar.gz','w:gz') as archive:
 for name in names: archive.add(root/name,arcname=name)
path=root/'evidence.tar.gz'
print(json.dumps(dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)),flush=True)
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-u", "-c", package]), timeout=180)
        result = stdout.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        archive_identity = json.loads(result)
        with client.open_sftp() as sftp:
            sftp.get(remote + "/evidence.tar.gz", str(evidence / "evidence.tar.gz"))
        if hashlib.sha256((evidence / "evidence.tar.gz").read_bytes()).hexdigest() != archive_identity["sha256"]:
            raise ValueError("native migration evidence download identity differs")
        destination = evidence / "evidence"
        destination.mkdir(exist_ok=False)
        with tarfile.open(evidence / "evidence.tar.gz") as archive:
            archive.extractall(destination, filter="data")
        (evidence / "finalization.json").write_text(json.dumps(dict(status="passed", archive=archive_identity,
            training_probe_sha256=probe_sha, training_admissible=False), indent=2))
        print(json.dumps(dict(status="passed", attempt=args.attempt, **archive_identity)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
