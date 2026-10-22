import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import PYTHON, WORKSPACE
from verify_search_episode import atomic_json, read, sha, validate_episode
from native_runner.training.fair_final.storage import durable_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    launch = read(args.attempt / "launch.json")
    fields = [part.strip() for part in next(line for line in
        (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = f'''
import hashlib,json
from pathlib import Path
root=Path({launch['remote']!r})
completion=root/'completion.json'
if not completion.exists():
 print(json.dumps(dict(status='running',complete=False,training_admissible=False)),flush=True)
else:
 names=set({list(launch['payload_sha256'])!r})
 names.update(str(path.relative_to(root)) for path in (root/'episode').rglob('*') if path.is_file())
 names.update(name for name in ('completion.json','phase-results.json','worker.log','prepare.log','resume.log') if (root/name).exists())
 for port in {launch['settings']['ports']!r}:
  names.update(name for name in (f'engine-{{port}}.log',f'engine-{{port}}/process.json') if (root/name).exists())
 files={{name:dict(sha256=hashlib.sha256((root/name).read_bytes()).hexdigest(),bytes=(root/name).stat().st_size) for name in sorted(names)}}
 print(json.dumps(dict(status='terminated',completion=json.loads(completion.read_text()),files=files)),flush=True)
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", probe]), timeout=30)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        inventory = json.loads(output)
        if inventory["status"] == "running":
            print(json.dumps(inventory), flush=True)
            return
        destination = args.attempt / "evidence"
        destination.mkdir(exist_ok=True)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(30)
            for relative, expected in inventory["files"].items():
                path = destination / relative
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    raise ValueError("search episode evidence path escapes attempt")
                if path.exists():
                    if path.stat().st_size != expected["bytes"] or sha(path) != expected["sha256"]:
                        raise ValueError("previously downloaded search evidence differs: " + relative)
                    continue
                with sftp.open(launch["remote"] + "/" + relative, "rb") as handle:
                    data = handle.read()
                if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                    raise ValueError("search episode download checksum differs: " + relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                durable_write(path, data)
        atomic_json(args.attempt / "download-receipt.json", dict(timestamp=time.time(), remote=launch["remote"],
            files=inventory["files"], training_admissible=False))
        try:
            report = validate_episode(destination, launch=launch)
        except Exception as error:
            atomic_json(args.attempt / "verification-failure.json", dict(status="failed", error=repr(error),
                training_admissible=False))
            raise
        atomic_json(args.attempt / "verification.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
