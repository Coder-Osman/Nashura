import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    args = parser.parse_args()
    launch_path = args.attempt / "launch.json"
    if hashlib.sha256(launch_path.read_bytes()).hexdigest() != args.launch_sha:
        raise ValueError("mode collection launch identity differs")
    launch = json.loads(launch_path.read_text())
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        script = f'''
from pathlib import Path
import json
process=Path('/proc')/str({launch['process']['pid']!r})
alive=False
if process.exists():
 fields=(process/'stat').read_text().rsplit(')',1)[1].split()
 alive=fields[19]=={launch['process']['start_ticks']!r} and fields[0]!='Z'
print(json.dumps(dict(alive=alive)))
'''
        _, stdout, stderr = client.exec_command(shlex.join([
            "/home/zhichun/firstlight_CR/.venv/bin/python", "-c", script]), timeout=30)
        raw, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        if json.loads(raw)["alive"]:
            raise ValueError("mode worker still active; no archive published")
        files = {}
        with client.open_sftp() as sftp:
            for name in (*launch["uploaded_sha256"], "report.json", "failure.json", "progress.json",
                    "cleanup.json", "worker.log", "probe.log", "engine.log", "engine/process.json"):
                try:
                    with sftp.open(launch["remote"] + "/" + name, "rb") as handle:
                        content = handle.read()
                except FileNotFoundError:
                    continue
                target = args.attempt / "archive" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != content:
                    raise ValueError("existing mode archive bytes differ")
                target.write_bytes(content)
                files[name] = hashlib.sha256(content).hexdigest()
        receipt = dict(schema="fair-final-native-mode-download.v1", launch_sha256=args.launch_sha,
            files=files, source_worker_stopped=True, training_admissible=False, gates_closed=False)
        target = args.attempt / "download.json"
        if target.exists() and json.loads(target.read_text()) != receipt:
            raise ValueError("mode download receipt differs")
        target.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_pending_verification", files=len(files),
            receipt_sha256=hashlib.sha256(target.read_bytes()).hexdigest())))
    finally:
        client.close()


if __name__ == "__main__":
    main()
