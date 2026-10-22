import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from continuation_evidence import EVIDENCE, WORKSPACE, audit_failed_prefix, read, sha
from native_runner.training.fair_final.episode import atomic_json
from run_native_probe import PYTHON


def execute(client, code, *, timeout=60):
    _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", code]), timeout=timeout)
    output, errors = stdout.read().decode(), stderr.read().decode()
    if stdout.channel.recv_exit_status():
        raise RuntimeError(errors)
    return json.loads(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    args = parser.parse_args()
    prefix = audit_failed_prefix(args.parent)
    parent_launch = read(args.parent / "launch.json")
    download = read(args.parent / "download-receipt.json")
    version = Path(__file__).resolve().parent
    attempt = "native-search-continuation-" + str(time.time_ns())
    output = version / attempt
    remote = "/home/zhichun/fair-final-v1-20261019-search-continuation/" + attempt
    prefix_files = {name: value for name, value in download["files"].items()
        if (name.startswith("episode/private-audit/") and name != "episode/private-audit/writer.lock")
        or name.startswith("episode/search-boundaries/")
        or name in ("episode/prepare-report.json", "episode/restart-receipt.json",
            "episode/recovery-search-report.json")}
    settings = dict(parent_launch["settings"], ports=[29751, 29752], segment_reserve_seconds=1800)
    uploads = {"continuation-input.json": json.dumps(prefix, sort_keys=True).encode(),
        "parent-download-receipt.json": (args.parent / "download-receipt.json").read_bytes(),
        "parent-launch.json": (args.parent / "launch.json").read_bytes(),
        "parent-failure.json": (args.parent / "evidence/episode/resume-failure.json").read_bytes(),
        "project/tools/probe_search_continuation.py": (version / "probe_search_continuation.py").read_bytes(),
        "project/continuation_worker.py": (version / "continuation_worker.py").read_bytes()}
    settings["driver_sha256"] = {name: hashlib.sha256(uploads[name]).hexdigest() for name in (
        "continuation-input.json", "project/tools/probe_search_continuation.py", "project/continuation_worker.py")}
    for name in ("libcrprobe.so", "source.tar.gz", "project/prior.json",
            "project/probe-source-manifest.json", "project/phase_snapshot_launcher.py"):
        uploads[name] = (args.parent / "evidence" / name).read_bytes()
    uploads["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    upload_sha = {name: hashlib.sha256(value).hexdigest() for name, value in uploads.items()}
    fields = [part.strip() for part in next(line for line in
        (WORKSPACE / "MACHINES.md").read_text().splitlines() if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        preparation = f'''
from pathlib import Path
from contextlib import ExitStack
import hashlib,json,shutil,socket
parent=Path({prefix['parent_remote']!r})
root=Path({remote!r})
if hashlib.sha256((parent/'completion.json').read_bytes()).hexdigest()!={prefix['parent_completion_sha256']!r}:
 raise ValueError('parent completion changed')
parent_process=Path('/proc/{parent_launch['process']['pid']}')
if parent_process.exists():
 stat=(parent_process/'stat').read_text().rsplit(')',1)[1].split()
 if stat[19]=={parent_launch['process']['start_ticks']!r}:
  raise ValueError('parent worker is still alive')
completion=json.loads((parent/'completion.json').read_text())
if completion['complete'] is not False or completion['clean'] is not True:
 raise ValueError('parent is not a cleanly stopped incomplete attempt')
with ExitStack() as stack:
 for port in {settings['ports']!r}:
  connection=stack.enter_context(socket.socket());connection.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if available<32*1024**3 or shutil.disk_usage(parent).free<5*1024**3:
 raise RuntimeError('continuation engineering resource reserve lost')
for name,expected in {prefix_files!r}.items():
 path=parent/name
 if path.stat().st_size!=expected['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=expected['sha256']:
  raise ValueError('parent recovery input changed: '+name)
root.mkdir(parents=True,exist_ok=False)
shutil.copytree(parent/'project',root/'project',ignore=shutil.ignore_patterns('__pycache__'))
for name in {list(prefix_files)!r}:
 target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(parent/name,target)
print(json.dumps(dict(available_gib=available/1024**3,free_gib=shutil.disk_usage(root).free/1024**3)),flush=True)
'''
        resources = execute(client, preparation)
        output.mkdir(parents=True, exist_ok=False)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(30)
            for name, value in uploads.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(value)
                with sftp.open(remote + "/" + name, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != upload_sha[name]:
                        raise ValueError("continuation upload checksum differs: " + name)
        deployment = f'''
from pathlib import Path
import hashlib,json,subprocess
root=Path({remote!r})
for name,expected in {upload_sha!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:
  raise ValueError('continuation payload identity differs: '+name)
manifest=json.loads((root/'project/probe-source-manifest.json').read_text())
for name,expected in manifest.items():
 if hashlib.sha256((root/'project'/name).read_bytes()).hexdigest()!=expected:
  raise ValueError('continuation production source identity differs: '+name)
with (root/'worker.log').open('x') as log:
 process=subprocess.Popen([{PYTHON!r},'-u',str(root/'project/continuation_worker.py'),str(root)],
  cwd=root/'project',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
stat=(Path('/proc')/str(process.pid)/'stat').read_text().rsplit(')',1)[1].split()
print(json.dumps(dict(pid=process.pid,start_ticks=stat[19],remote=str(root))),flush=True)
'''
        process = execute(client, deployment)
        launch = dict(attempt=attempt, remote=remote, parent=str(args.parent.resolve()),
            parent_launch_sha256=sha(args.parent / "launch.json"), source_sha256=prefix["source_sha256"],
            payload_sha256=upload_sha, settings=settings, process=process, resources=resources,
            input_sha256=upload_sha["continuation-input.json"], training_admissible=False, gates_closed=False)
        atomic_json(output / "launch.json", launch)
        atomic_json(output / "continuation-input.json", prefix)
        print(json.dumps(dict(local=str(output), **process, resume_tick=prefix["proof"]["tick"],
            committed_groups=prefix["committed_groups"], resources=resources), indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
