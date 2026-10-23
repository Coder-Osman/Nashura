import argparse
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_selection import GATES, publish, read, sha
from verify_active import archived


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
COMPARISON = ("reference.json", "inheritance.json", "frozen.json",
    "frozen-midpoint.pt", "frozen-reference.pt", "frozen-resumed.pt",
    "trainable-midpoint.pt", "trainable-reference.pt")
INPUTS = ("payload.tar.gz", "deployment.json", "benchmark/report.json") + tuple(
    "active-comparison/" + name for name in COMPARISON)


def failed_parent(evidence):
    evidence = Path(evidence)
    phases = read(evidence / "phase-results.json")
    failure = read(evidence / "supervisor-failure.json")
    if ((evidence / "completion.json").exists()
            or [item["phase"] for item in phases] != ["benchmark", "reference", "frozen", "trainable"]
            or [item["exit_code"] for item in phases] != [0, 0, 0, 124]
            or failure["training_admissible"] is not False
            or failure["error"] != "RuntimeError('trainable failed: 124')"
            or (evidence / "active-comparison/trainable.json").exists()
            or (evidence / "active-comparison/trainable-resumed.pt").exists()
            or len({(item["process"]["pid"], item["process"]["start_ticks"]) for item in phases}) != 4):
        raise ValueError("retry requires the preserved trainable-timeout parent")
    return phases


def request_for(parent, *, launch_sha, receipt_sha, device_index, timeout_seconds):
    if type(device_index) is not int or not 0 <= device_index < 8:
        raise ValueError("GPU index differs")
    if type(timeout_seconds) is not int or not 1800 <= timeout_seconds <= 86400:
        raise ValueError("independent bounded retry budget differs")
    launch, evidence, receipt = archived(parent, launch_sha=launch_sha, receipt_sha=receipt_sha)
    failed_parent(evidence)
    names = set(INPUTS) | set(launch["source_sha256"])
    if not names <= set(receipt["files"]):
        raise ValueError("parent omits frozen retry inputs")
    files = {name: receipt["files"][name] for name in sorted(names)}
    return dict(schema="FairFinalActiveResumeRetry/v1", parent_remote=launch["remote"],
        parent_launch_sha256=launch_sha, parent_receipt_sha256=receipt_sha,
        parent_failure_sha256=sha(evidence / "supervisor-failure.json"), files=files,
        source="/home/zhichun/fair-teacher-r0-vs-m0-20261014/candidate.pt",
        data_root=launch["parent"] + "/data", device_index=device_index,
        timeout_seconds=timeout_seconds, **GATES)


def launch_retry(parent, *, launch_sha, receipt_sha, device_index=1, timeout_seconds=28800):
    request = request_for(parent, launch_sha=launch_sha, receipt_sha=receipt_sha,
        device_index=device_index, timeout_seconds=timeout_seconds)
    parent_launch = read(Path(parent) / "launch.json")
    output = ROOT / ("native-active-retry-" + str(time.time_ns()))
    output.mkdir()
    publish(output / "retry-input.json", request)
    remote = "/home/zhichun/" + output.name
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    uploads = {"retry-input.json": output / "retry-input.json", "supervisor.py": ROOT / "retry_runtime.py",
        "run_selection.py": ROOT / "run_selection.py"}
    uploaded = {name: sha(path) for name, path in uploads.items()}
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=15)
    try:
        def execute(program):
            _, stdout, stderr = client.exec_command(shlex.join([python, "-B", "-c", program]), timeout=120)
            result, errors = stdout.read().decode(), stderr.read().decode()
            if stdout.channel.recv_exit_status():
                raise RuntimeError(errors[-2000:])
            return result

        preparation = f'''
import hashlib,json,shutil,subprocess
from pathlib import Path
parent=Path({request['parent_remote']!r})
root=Path({remote!r})
processes={[parent_launch['process']]!r}+json.loads((parent/'phase-results.json').read_text())
for entry in processes:
 process=entry.get('process',entry)
 stat=Path('/proc',str(process['pid']),'stat')
 if stat.exists():
  parts=stat.read_text().rsplit(')',1)[1].split()
  if parts[19]==process['start_ticks'] and parts[0]!='Z':raise ValueError('parent is still running')
memory=int(next(row.split()[1] for row in Path('/proc/meminfo').read_text().splitlines() if row.startswith('MemAvailable:')))/1024**2
if memory<32 or shutil.disk_usage(parent).free<5*1024**3:raise ValueError('engineering resource floor failed')
gpu=subprocess.check_output(['nvidia-smi','-i',{str(device_index)!r},'--query-gpu=index,uuid,name,memory.used','--format=csv,noheader,nounits'],text=True).strip()
if int(gpu.rsplit(',',1)[1].strip())>1024:raise ValueError('retry GPU is occupied')
if hashlib.sha256((parent/'supervisor-failure.json').read_bytes()).hexdigest()!={request['parent_failure_sha256']!r}:raise ValueError('parent failure changed')
for name,metadata in {request['files']!r}.items():
 path=parent/name
 if path.is_symlink() or path.stat().st_size!=metadata['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['sha256']:raise ValueError('parent retry input changed: '+name)
root.mkdir(exist_ok=False)
for name in {list(request['files'])!r}:
 target=root/name
 target.parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(parent/name,target)
(root/'resources.json').write_text(json.dumps(dict(available_gib=memory,free_gib=shutil.disk_usage(root).free/1024**3,gpu=gpu)))
'''
        execute(preparation)
        with client.open_sftp() as sftp:
            for name, path in uploads.items():
                sftp.put(str(path), remote + "/" + name, confirm=True)
        deploy = f'''
import hashlib,json,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {uploaded!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('retry upload differs')
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{python!r},'-B','-u',str(root/'supervisor.py'),str(root),'--input-sha',{uploaded['retry-input.json']!r}],stdout=log,stderr=subprocess.STDOUT,cwd=root,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''
        process = json.loads(execute(deploy))
        launch = dict(attempt=output.name, remote=remote, process=process,
            parent_launch_sha256=launch_sha, parent_receipt_sha256=receipt_sha,
            payload_sha256=parent_launch["payload_sha256"], source_sha256=parent_launch["source_sha256"],
            uploaded_sha256=uploaded, supervisor_sha256=uploaded["supervisor.py"],
            retry_input_sha256=uploaded["retry-input.json"], **GATES)
        publish(output / "launch.json", launch)
        return dict(local=str(output), remote=remote, process=process, **GATES)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--device-index", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=28800)
    args = parser.parse_args()
    print(json.dumps(launch_retry(args.parent, launch_sha=args.launch_sha, receipt_sha=args.receipt_sha,
        device_index=args.device_index, timeout_seconds=args.timeout_seconds)), flush=True)
