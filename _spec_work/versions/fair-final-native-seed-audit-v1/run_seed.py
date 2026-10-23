import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
GENERAL = ROOT.parent / "fair-final-general-fallback-v1"
sys.path.insert(0, str(GENERAL))
import run_native_general
sys.path.insert(0, str(ROOT.parent / "fair-final-history-v2/project"))
from seed_audit import PROBE_SHA, sha, static_report


def connect():
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    return client


def execute(client, code):
    _, stdout, stderr = client.exec_command(shlex.join([run_native_general.PYTHON, "-c", code]), timeout=60)
    raw, errors = stdout.read().decode(), stderr.read().decode()
    if stdout.channel.recv_exit_status():
        raise RuntimeError(errors)
    return raw


def launch(parent_path):
    parent = json.loads((parent_path / "attempt.json").read_text())
    expected = run_native_general.refill_runner.source_manifest()
    expected.update({name: sha(GENERAL / name) for name in ("general_provider.py", "probe_native_general.py")})
    engine_sha = run_native_general.validate_phase_engine_build(
        json.loads((run_native_general.refill_runner.BUILD / "build-manifest.json").read_text()))
    if parent["source_sha256"] != expected or parent["engine_sha256"] != engine_sha or engine_sha != PROBE_SHA:
        raise ValueError("seed audit requires exact frozen native runtime")
    output = ROOT / ("native-seed-" + str(time.time_ns()))
    output.mkdir()
    remote = "/home/zhichun/fair-final-native-seed-audit-v1/" + output.name
    project = parent["remote"] + "/project"
    files = {name: (ROOT / name).read_bytes() for name in ("seed_audit.py", "probe_seed.py", "seed_worker.py")}
    settings = dict(root=remote, project=project, base_project=run_native_general.BASE + "/project",
        puct_root=run_native_general.BASE + "/project/research/puct-optimized", port=29775,
        probe=parent["remote"] + "/libcrprobe.so", engine_sha256=engine_sha,
        source_sha256={name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
    files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    client = connect()
    try:
        execute(client, f'''
import hashlib,shutil,socket
from pathlib import Path
root=Path({remote!r});project=Path({project!r})
with socket.socket() as check:check.bind(('127.0.0.1',29775))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if memory<32*1024**3 or shutil.disk_usage('/home/zhichun').free<5*1024**3:raise ValueError('seed audit resources insufficient')
for name,expected in {expected!r}.items():
 if hashlib.sha256((project/name).read_bytes()).hexdigest()!=expected:raise ValueError('frozen seed runtime identity differs')
root.mkdir(parents=True,exist_ok=False)
''')
        with client.open_sftp() as sftp:
            for name, content in files.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(content)
        process = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {identities!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('seed audit upload differs')
environment=dict(os.environ,PYTHONPATH={project!r})
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{run_native_general.PYTHON!r},'-u',str(root/'seed_worker.py'),str(root/'settings.json')],cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        record = dict(schema="fair-final-native-seed-launch.v1", remote=remote, process=process,
            settings=settings, uploaded_sha256=identities, frozen_runtime_sha256=expected,
            parent_attempt_sha256=sha(parent_path / "attempt.json"), runner_sha256=sha(__file__),
            static_report_sha256=sha(ROOT / "static-report.json"), training_admissible=False, gates_closed=False)
        (output / "launch.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(dict(output=str(output), process=process, launch_sha256=sha(output / "launch.json"))))
    finally:
        client.close()


def collect(attempt, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("seed launch external identity differs")
    record = json.loads((attempt / "launch.json").read_text())
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import json
from pathlib import Path
process=Path('/proc')/str({record['process']['pid']!r})
alive=False
if process.exists():
 fields=(process/'stat').read_text().rsplit(')',1)[1].split()
 alive=fields[19]=={record['process']['start_ticks']!r} and fields[0]!='Z'
print(json.dumps(dict(alive=alive)))
'''))
        if state["alive"]:
            print(json.dumps(dict(status="running", training_admissible=False, gates_closed=False)))
            return
        files = {}
        names = [*record["uploaded_sha256"], "worker.log"]
        for variant in ("immediate", "delayed"):
            names.extend(variant + "/" + name for name in ("report.json", "failure.json", "progress.json",
                "cleanup.json", "probe.log", "engine.log", "engine/process.json"))
        with client.open_sftp() as sftp:
            for name in names:
                try:
                    with sftp.open(record["remote"] + "/" + name, "rb") as handle:
                        content = handle.read()
                except FileNotFoundError:
                    continue
                target = attempt / "archive" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != content:
                    raise ValueError("existing seed archive bytes differ")
                target.write_bytes(content)
                files[name] = hashlib.sha256(content).hexdigest()
        receipt = dict(schema="fair-final-native-seed-download.v1", launch_sha256=launch_sha,
            files=files, source_worker_stopped=True, training_admissible=False, gates_closed=False)
        target = attempt / "download.json"
        if target.exists() and json.loads(target.read_text()) != receipt:
            raise ValueError("seed download receipt differs")
        target.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_pending_verification", files=len(files), receipt_sha256=sha(target))))
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    static = commands.add_parser("static")
    static.add_argument("library", type=Path)
    start = commands.add_parser("launch")
    start.add_argument("frozen_general_attempt", type=Path)
    download = commands.add_parser("collect")
    download.add_argument("attempt", type=Path)
    download.add_argument("--launch-sha", required=True)
    args = parser.parse_args()
    if args.command == "static":
        report = static_report(args.library)
        with (ROOT / "static-report.json").open("x") as handle:
            handle.write(json.dumps(report, indent=2) + "\n")
        print(json.dumps(dict(status=report["status"], static_report_sha256=sha(ROOT / "static-report.json"))))
    elif args.command == "launch":
        launch(args.frozen_general_attempt)
    else:
        collect(args.attempt, args.launch_sha)


if __name__ == "__main__":
    main()
