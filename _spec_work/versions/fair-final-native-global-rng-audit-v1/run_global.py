import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from global_rng_audit import PORT, sha


ROOT = Path(__file__).resolve().parent
SEED_ROOT = ROOT.parent / "fair-final-native-seed-audit-v1"
sys.path.insert(0, str(SEED_ROOT))
from run_seed import connect, execute, run_native_general
from seed_audit import PROBE_SHA


def upload_sources():
    return {
        "global_rng_audit.py": (ROOT / "global_rng_audit.py").read_bytes(),
        "probe_seed.py": (ROOT / "probe_global.py").read_bytes(),
        "seed_audit.py": (SEED_ROOT / "seed_audit.py").read_bytes(),
        "seed_worker.py": (SEED_ROOT / "seed_worker.py").read_bytes(),
    }


def launch(parent_path):
    parent = json.loads((parent_path / "attempt.json").read_text())
    expected = run_native_general.refill_runner.source_manifest()
    expected.update({name: sha(run_native_general.ROOT / name)
        for name in ("general_provider.py", "probe_native_general.py")})
    if parent["source_sha256"] != expected or parent["engine_sha256"] != PROBE_SHA:
        raise ValueError("global RNG diagnostic frozen native runtime differs")
    output = ROOT / ("native-global-rng-" + str(time.time_ns()))
    output.mkdir()
    remote = "/home/zhichun/fair-final-native-global-rng-audit-v1/" + output.name
    project = parent["remote"] + "/project"
    files = upload_sources()
    settings = dict(root=remote, project=project, base_project=run_native_general.BASE + "/project",
        puct_root=run_native_general.BASE + "/project/research/puct-optimized", port=PORT,
        probe=parent["remote"] + "/libcrprobe.so", engine_sha256=PROBE_SHA,
        source_sha256={name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
    files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    record = dict(schema="fair-final-native-global-rng-launch.v1", remote=remote, settings=settings,
        uploaded_sha256=identities, parent_attempt_sha256=sha(parent_path / "attempt.json"),
        frozen_runtime_sha256=expected, runner_sha256=sha(__file__),
        static_report_sha256=sha(ROOT / "static-report.json"), contract_sha256=sha(ROOT / "CONTRACT.md"),
        training_admissible=False, gates_closed=False)
    (output / "registration.json").write_text(json.dumps(record, indent=2) + "\n")
    client = connect()
    try:
        execute(client, f'''
import hashlib,shutil,socket
from pathlib import Path
root=Path({remote!r});project=Path({project!r})
with socket.socket() as check:check.bind(('127.0.0.1',{PORT}))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if memory<32*1024**3 or shutil.disk_usage('/home/zhichun').free<5*1024**3:raise ValueError('global RNG diagnostic resources insufficient')
for name,expected in {expected!r}.items():
 if hashlib.sha256((project/name).read_bytes()).hexdigest()!=expected:raise ValueError('frozen global RNG runtime differs')
root.mkdir(parents=True,exist_ok=False)
''')
        with client.open_sftp() as sftp:
            for name, content in files.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(content)
        record["process"] = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {identities!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('global RNG upload differs')
environment=dict(os.environ,PYTHONPATH={project!r})
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{run_native_general.PYTHON!r},'-u',str(root/'seed_worker.py'),str(root/'settings.json')],cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        (output / "launch.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(dict(output=str(output), process=record["process"], launch_sha256=sha(output / "launch.json"))))
    finally:
        client.close()


def collect(attempt, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("global RNG external launch identity differs")
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
progress={{}}
for variant in ('immediate','delayed'):
 path=Path({record['remote']!r})/variant/'progress.json'
 if path.exists():progress[variant]=[row['threshold'] for row in json.loads(path.read_text())['records']]
print(json.dumps(dict(alive=alive,progress=progress)))
'''))
        if state["alive"]:
            print(json.dumps(dict(status="running", **state)))
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
                    raise ValueError("existing global RNG archive differs")
                target.write_bytes(content)
                files[name] = hashlib.sha256(content).hexdigest()
        receipt = dict(schema="fair-final-native-global-rng-download.v1", launch_sha256=launch_sha,
            files=files, source_worker_stopped=True, training_admissible=False, gates_closed=False)
        target = attempt / "download.json"
        if target.exists() and json.loads(target.read_text()) != receipt:
            raise ValueError("global RNG download receipt differs")
        target.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_pending_verification", files=len(files), receipt_sha256=sha(target))))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("parent", type=Path)
    download = commands.add_parser("collect")
    download.add_argument("attempt", type=Path)
    download.add_argument("--launch-sha", required=True)
    arguments = parser.parse_args()
    if arguments.command == "launch":
        launch(arguments.parent)
    else:
        collect(arguments.attempt, arguments.launch_sha)
