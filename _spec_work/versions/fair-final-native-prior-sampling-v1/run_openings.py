import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from prior_sampling import PORT, PROBE_SHA, REPEAT_COUNT, seeds, sha


ROOT = Path(__file__).resolve().parent
SEED_AUDIT = ROOT.parent / "fair-final-native-seed-audit-v1"
sys.path.insert(0, str(SEED_AUDIT))
from run_seed import connect, execute, run_native_general


def launch(parent_path):
    parent = json.loads((parent_path / "attempt.json").read_text())
    expected = run_native_general.refill_runner.source_manifest()
    expected.update({name: sha(run_native_general.ROOT / name)
        for name in ("general_provider.py", "probe_native_general.py")})
    if parent["source_sha256"] != expected or parent["engine_sha256"] != PROBE_SHA:
        raise ValueError("opening diagnostic requires exact frozen native runtime")
    output = ROOT / ("native-openings-" + str(time.time_ns()))
    output.mkdir()
    remote = "/home/zhichun/fair-final-native-prior-sampling-v1/" + output.name
    project = parent["remote"] + "/project"
    files = {name: (ROOT / name).read_bytes()
        for name in ("prior_sampling.py", "probe_openings.py", "opening_worker.py")}
    settings = dict(root=remote, project=project, base_project=run_native_general.BASE + "/project",
        puct_root=run_native_general.BASE + "/project/research/puct-optimized", port=PORT,
        probe=parent["remote"] + "/libcrprobe.so", engine_sha256=PROBE_SHA,
        seeds=seeds(), repeat_count=REPEAT_COUNT, timeout_seconds=14400,
        runtime_sha256=expected,
        source_sha256={name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
    files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    record = dict(schema="fair-final-native-opening-launch.v1", remote=remote, settings=settings,
        uploaded_sha256=identities, parent_attempt_sha256=sha(parent_path / "attempt.json"),
        runner_sha256=sha(__file__), contract_sha256=sha(ROOT / "CONTRACT.md"),
        training_admissible=False, gates_closed=False)
    (output / "registration.json").write_text(json.dumps(record, indent=2) + "\n")
    client = connect()
    try:
        preflight = json.loads(execute(client, f'''
import hashlib,json,shutil,socket
from pathlib import Path
root=Path({remote!r});project=Path({project!r})
with socket.socket() as check:check.bind(('127.0.0.1',{PORT}))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if memory<32*1024**3 or free<5*1024**3:raise ValueError('opening diagnostic resources insufficient')
for name,expected in {expected!r}.items():
 if hashlib.sha256((project/name).read_bytes()).hexdigest()!=expected:raise ValueError('frozen opening runtime identity differs')
root.mkdir(parents=True,exist_ok=False)
print(json.dumps(dict(memory_available=memory,free_bytes=free)))
'''))
        with client.open_sftp() as sftp:
            for name, content in files.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(content)
        process = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {identities!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('opening upload differs')
environment=dict(os.environ,PYTHONPATH={project!r})
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{run_native_general.PYTHON!r},'-u',str(root/'opening_worker.py'),str(root/'settings.json')],cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        record.update(process=process, preflight=preflight)
        (output / "launch.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(dict(output=str(output), process=process, launch_sha256=sha(output / "launch.json"))))
    finally:
        client.close()


def collect(attempt, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("opening external launch identity differs")
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
tails={{}}
for variant in ('primary','repeat'):
 path=Path({record['remote']!r})/variant/'probe.log'
 if path.exists():tails[variant]=path.read_text()[-2000:]
print(json.dumps(dict(alive=alive,progress=tails)))
'''))
        if state["alive"]:
            print(json.dumps(dict(status="running", **state)))
            return
        files = {}
        names = [*record["uploaded_sha256"], "worker.log"]
        for variant in ("primary", "repeat"):
            names.extend(variant + "/" + name for name in ("report.json", "rows.jsonl", "cleanup.json",
                "probe.log", "engine.log", "engine/process.json"))
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
                    raise ValueError("existing opening archive differs")
                target.write_bytes(content)
                files[name] = hashlib.sha256(content).hexdigest()
        receipt = dict(schema="fair-final-native-opening-download.v1", launch_sha256=launch_sha,
            files=files, source_worker_stopped=True, training_admissible=False, gates_closed=False)
        target = attempt / "download.json"
        if target.exists() and json.loads(target.read_text()) != receipt:
            raise ValueError("opening download receipt differs")
        target.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_pending_verification", files=len(files), receipt_sha256=sha(target))))
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("parent", type=Path)
    download = commands.add_parser("collect")
    download.add_argument("attempt", type=Path)
    download.add_argument("--launch-sha", required=True)
    args = parser.parse_args()
    if args.command == "launch":
        launch(args.parent)
    else:
        collect(args.attempt, args.launch_sha)


if __name__ == "__main__":
    main()
