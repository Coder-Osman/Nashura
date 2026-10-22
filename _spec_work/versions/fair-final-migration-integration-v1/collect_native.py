import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
ARTIFACTS = (
    "deployment.json", "supervisor.py", "resume_worker.py", "resume-settings.json",
    "training-process.json", "status.json", "completion.json", "supervisor-failure.json",
    "phase-results.json", "parent-receipt.json", "round0.pt", "round0.report.json",
    "parent-round0.pt", "parent-round0.report.json", "parent-completion.json",
    "comparison/midpoint.pt", "comparison/reference.pt", "comparison/resumed.pt",
    "comparison/reference.json", "comparison/resumed.json",
    "training.log", "supervisor.log", "worker.log", "reference.log", "resumed.log",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def publish_archive(attempt, launch, inventory, read_remote):
    if inventory["status"] != "terminated":
        return dict(status=inventory["status"], training_admissible=False, formal_g2_pass=False)
    expected = dict(launch.get("source_sha256", {}))
    expected.update(launch.get("uploaded_sha256", {}))
    if "payload.tar.gz" in expected:
        package = read_remote("payload.tar.gz")
        if sha(package) != expected["payload.tar.gz"]:
            raise ValueError("frozen deployment package differs")
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            manifest = archive.extractfile("deployment.json").read()
        expected.update(json.loads(manifest))
        expected["deployment.json"] = sha(manifest)
    if "supervisor_sha256" in launch:
        expected["supervisor.py"] = launch["supervisor_sha256"]
    for name, digest in expected.items():
        if inventory["files"].get(name, {}).get("sha256") != digest:
            raise ValueError("frozen deployment differs: " + name)
    identity = dict(remote=launch["remote"], launch_sha256=sha((attempt / "launch.json").read_bytes()),
        files=inventory["files"], training_admissible=False, formal_g2_pass=False)
    receipt_path = attempt / "native-download-receipt.json"
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text())
        if set(previous) != {"timestamp", *identity} or any(previous[key] != value for key, value in identity.items()):
            raise ValueError("pinned native archive identity differs")
    for name, metadata in inventory["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("native archive path escapes attempt")
        path = attempt / "native-evidence" / relative
        data = path.read_bytes() if path.exists() else read_remote(name)
        if len(data) != metadata["bytes"] or sha(data) != metadata["sha256"]:
            raise ValueError("native archive checksum differs: " + name)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".partial")
            temporary.write_bytes(data)
            temporary.replace(path)
    if not receipt_path.exists():
        temporary = receipt_path.with_suffix(".partial")
        temporary.write_text(json.dumps(dict(timestamp=time.time(), **identity), indent=2))
        temporary.replace(receipt_path)
    return dict(status="terminated_archive_only", completion=inventory.get("completion"),
        files=len(inventory["files"]), deployment_verified=True,
        training_admissible=False, formal_g2_pass=False, native_resume_verified=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    launch = json.loads((args.attempt / "launch.json").read_text())
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = f'''
import hashlib,json
from pathlib import Path
root=Path({launch['remote']!r})
processes=[{launch['process']!r}]
for name in ('training-process.json','status.json'):
 path=root/name
 if path.exists():
  payload=json.loads(path.read_text())
  process=payload.get('process',payload)
  if 'pid' in process and 'start_ticks' in process:processes.append(process)
running=False
for process in processes:
 path=Path('/proc',str(process['pid']),'stat')
 if path.exists():
  values=path.read_text().rsplit(')',1)[1].split()
  running=running or (values[19]==process['start_ticks'] and values[0]!='Z')
if running:
 print(json.dumps(dict(status='running')))
elif not (root/'completion.json').exists() and not (root/'supervisor-failure.json').exists():
 print(json.dumps(dict(status='stopped_without_completion')))
else:
 names=set({list(launch.get('source_sha256', {}))!r}) | set({list(launch.get('uploaded_sha256', {}))!r})
 names.update(name for name in {ARTIFACTS!r} if (root/name).is_file())
 deployment=root/'deployment.json'
 if deployment.exists():names.update(json.loads(deployment.read_text()))
 files={{}}
 for name in sorted(names):
  data=(root/name).read_bytes()
  files[name]=dict(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
 completion=root/'completion.json'
 print(json.dumps(dict(status='terminated',files=files,
  completion=json.loads(completion.read_text()) if completion.exists() else None)))
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join(["python3", "-c", probe]), timeout=60)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        inventory = json.loads(output)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(60)

            def read_remote(name):
                with sftp.open(launch["remote"] + "/" + name, "rb") as handle:
                    return handle.read()

            report = publish_archive(args.attempt, launch, inventory, read_remote)
        print(json.dumps(report, indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
