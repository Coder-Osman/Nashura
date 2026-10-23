import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
sys.path[:0] = [str(ROOT.parent / name) for name in ("fair-final-active-migration-v1",
    "fair-final-global-rng-snapshot-v1", "fair-final-native-seed-audit-v1", "fair-final-history-v2/project")]
from selection_ssh import execute_remote, ssh_client
from trace_protocol import sha
from verify_snapshot import validate as validate_parent


PYTHON = "/home/zhichun/firstlight_CR/.venv/bin/python"
PORT = 29785


def connect():
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    return ssh_client("172.16.48.35", fields[3], password=fields[4])


def execute(client, code):
    return execute_remote(client, shlex.join([PYTHON, "-B", "-c", code]), timeout=60)


def upload_sources():
    versions = ROOT.parent
    files = {name: (ROOT / name).read_bytes() for name in
        ("trace_protocol.py", "trace_launcher.py", "probe_trace.py", "trace_worker.py")}
    for name, version in (("seed_audit.py", "fair-final-native-seed-audit-v1"),
            ("global_rng_audit.py", "fair-final-native-global-rng-audit-v1"),
            ("probe_snapshot.py", "fair-final-global-rng-snapshot-v1"),
            ("combat_protocol.py", "fair-final-combat-snapshot-v1"),
            ("selected_collection_worker.py", "fair-final-migration-integration-v1")):
        files[name] = (versions / version / name).read_bytes()
    return files


def launch(parent, *, parent_launch_sha, parent_receipt_sha):
    proof = validate_parent(parent, parent_launch_sha, parent_receipt_sha)
    ancestor = json.loads((parent / "launch.json").read_text())
    service_sha = json.loads((parent / "archive/immediate/engine/process.json").read_text())["phase_snapshot_engine"]["service_sha256"]
    attempt_id = "native-execution-" + str(time.time_ns())
    attempt = ROOT / attempt_id
    remote = "/home/zhichun/fair-final-random-execution-v1/" + attempt_id
    settings = {**ancestor["settings"], "root": remote, "port": PORT}
    settings["probe"] = ancestor["settings"]["probe"]
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import hashlib,json,shutil,socket,subprocess
from pathlib import Path
root=Path({remote!r});project=Path({settings['project']!r})
with socket.socket() as check:check.bind(('127.0.0.1',{PORT}))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if memory<32*1024**3 or shutil.disk_usage(root.parent.parent).free<5*1024**3:raise ValueError('execution trace resources insufficient')
digest=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
for name,expected in {ancestor['frozen_runtime_sha256']!r}.items():
 if digest(project/name)!=expected:raise ValueError('execution trace frozen runtime differs')
if digest({settings['probe']!r})!={proof['engine_sha256']!r}:raise ValueError('execution trace probe differs')
base=Path({settings['base_project']!r})
qemu=base/'research/standalone-engine-20260926/qemu/usr/bin/qemu-aarch64-static'
service=base/'research/standalone-resident-p18-stepatomic/service.py'
if digest(service)!={service_sha!r}:raise ValueError('trace parent service identity differs')
version=subprocess.run([str(qemu),'--version'],capture_output=True,text=True,check=True,timeout=10).stdout
help=subprocess.run([str(qemu),'-d','help'],capture_output=True,text=True,timeout=10)
if 'version 8.2.2' not in version or 'complete traces' not in help.stdout+help.stderr:raise ValueError('QEMU trace semantics differ')
source=service.read_text()
if source.count('deadline = time.monotonic()+140')!=1:raise ValueError('trace startup service source differs')
updated=source.replace('deadline = time.monotonic()+140','deadline = time.monotonic()+600')
print(json.dumps(dict(qemu=str(qemu),qemu_sha256=digest(qemu),qemu_version=version,service_sha256=digest(service),
    startup_service_sha256=hashlib.sha256(updated.encode()).hexdigest())))
'''))
        files = upload_sources()
        settings.update(state, engine_sha256=proof["engine_sha256"], source_sha256={
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
        files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
        identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
        registration = dict(schema="FairFinalRandomExecutionLaunch/v1", remote=remote,
            settings=settings, uploaded_sha256=identities,
            frozen_runtime_sha256=ancestor["frozen_runtime_sha256"], parent_verification=proof,
            parent_launch_sha256=parent_launch_sha, parent_receipt_sha256=parent_receipt_sha,
            runner_sha256=sha(__file__), contract_sha256=sha(ROOT / "CONTRACT.md"),
            training_admissible=False, gates_closed=False)
        attempt.mkdir(exist_ok=False)
        (attempt / "registration.json").write_text(json.dumps(registration, indent=2) + "\n")
        execute(client, f"from pathlib import Path; Path({remote!r}).mkdir(parents=True,exist_ok=False)")
        with client.open_sftp() as sftp:
            for name, data in files.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(data)
        process = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {identities!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('execution trace upload differs')
environment=dict(os.environ,PYTHONPATH={settings['project']!r},PYTHONDONTWRITEBYTECODE='1')
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{PYTHON!r},'-B','-u',str(root/'trace_worker.py'),str(root/'settings.json')],cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        launch_record = dict(registration, process=process)
        (attempt / "launch.json").write_text(json.dumps(launch_record, indent=2) + "\n")
        print(json.dumps(dict(attempt=str(attempt), process=process, launch_sha256=sha(attempt / "launch.json"))))
    finally:
        client.close()


def collect(attempt, *, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("execution trace pinned launch differs")
    record = json.loads((attempt / "launch.json").read_text())
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import json,time
from pathlib import Path
process=Path('/proc')/str({record['process']['pid']!r})
alive=False;reused=False
if process.exists():
 fields=(process/'stat').read_text().rsplit(')',1)[1].split()
 reused=fields[19]!={record['process']['start_ticks']!r};alive=not reused and fields[0]!='Z'
root=Path({record['remote']!r});files={{}}
for name in ('calibration/report.json','calibration/failure.json','calibration/cleanup.json','trace/progress.json','trace/failure.json','trace/cleanup.json','completion.json','failure.json'):
 path=root/name
 if path.exists():files[name]=json.loads(path.read_text())
print(json.dumps(dict(alive=alive,pid_reused=reused,timestamp=time.time(),files=files)))
'''))
        if state["pid_reused"]:
            raise ValueError("execution trace worker PID reused")
        (attempt / ("poll-" + str(time.time_ns()) + ".json")).write_text(json.dumps(state, indent=2))
        if state["alive"]:
            print(json.dumps(dict(status="running", timestamp=state["timestamp"],
                progress=state["files"].get("trace/progress.json"),
                calibration_complete="calibration/report.json" in state["files"])))
            return
        names = [*record["uploaded_sha256"], "worker.log", "completion.json", "failure.json"]
        for mode in ("calibration", "trace"):
            names += [mode + "/" + name for name in ("trace-settings.json", "report.json", "failure.json",
                "address-evidence.json", "progress.json", "cleanup.json", "engine-owner.json", "engine.log",
                "probe.log", "probe-process.json", "status.json", "qemu.log", "trace-summary.json", "engine/process.json")]
        metadata = {}
        with client.open_sftp() as sftp:
            for name in names:
                try:
                    handle = sftp.open(record["remote"] + "/" + name, "rb")
                except FileNotFoundError:
                    continue
                destination = attempt / "archive" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".partial")
                digest, size = hashlib.sha256(), 0
                with handle, temporary.open("xb") as output:
                    while chunk := handle.read(1024 * 1024):
                        size += len(chunk)
                        if size > 256 * 1024**2:
                            raise ValueError("bounded trace archive member exceeded")
                        digest.update(chunk);output.write(chunk)
                if destination.exists() and sha(destination) != digest.hexdigest():
                    raise ValueError("existing execution archive changed")
                temporary.replace(destination)
                metadata[name] = dict(sha256=digest.hexdigest(), bytes=size)
        receipt = dict(schema="FairFinalRandomExecutionDownload/v1", launch_sha256=launch_sha,
            files=metadata, worker_stopped=True, training_admissible=False, gates_closed=False)
        (attempt / "download.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_requires_independent_verification", files=len(metadata),
            receipt_sha256=sha(attempt / "download.json"))))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("parent", type=Path)
    start.add_argument("--parent-launch-sha", required=True)
    start.add_argument("--parent-receipt-sha", required=True)
    archive = commands.add_parser("collect")
    archive.add_argument("attempt", type=Path)
    archive.add_argument("--launch-sha", required=True)
    arguments = parser.parse_args()
    if arguments.command == "launch":
        launch(arguments.parent, parent_launch_sha=arguments.parent_launch_sha,
            parent_receipt_sha=arguments.parent_receipt_sha)
    else:
        collect(arguments.attempt, launch_sha=arguments.launch_sha)
