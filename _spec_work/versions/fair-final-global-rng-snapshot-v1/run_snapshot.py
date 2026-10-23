import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from build_engine import ROOT, sha


AUDIT = ROOT.parent / "fair-final-native-global-rng-audit-v1"
SEED = ROOT.parent / "fair-final-native-seed-audit-v1"
sys.path.insert(0, str(SEED))
from run_seed import connect, execute, run_native_general


PORT = 29781


def upload_sources():
    return {"probe_seed.py": (ROOT / "probe_snapshot.py").read_bytes(),
        "global_rng_audit.py": (AUDIT / "global_rng_audit.py").read_bytes(),
        **{name: (SEED / name).read_bytes() for name in ("seed_audit.py", "seed_worker.py")}}


def launch(build, parent):
    build_record = json.loads((build / "build-manifest.json").read_text())
    if (build_record["schema"] != "fair-final-global-rng-snapshot-build.v1"
            or build_record["builder_sha256"] != sha(ROOT / "build_engine.py")
            or build_record["module_sha256"] != sha(ROOT / "global_snapshot.inc")):
        raise ValueError("global snapshot build implementation differs")
    for name, expected in build_record["artifacts"].items():
        if sha(build / name) != expected:
            raise ValueError("global snapshot build artifact differs")
    source = json.loads((parent / "launch.json").read_text())
    output = ROOT / ("native-snapshot-" + str(time.time_ns()))
    output.mkdir()
    remote = "/home/zhichun/fair-final-global-rng-snapshot-v1/" + output.name
    files = upload_sources()
    settings = {**source["settings"], "root": remote, "port": PORT,
        "probe": remote + "/libcrprobe.so", "engine_sha256": sha(build / "libcrprobe.so"),
        "source_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    files["libcrprobe.so"] = (build / "libcrprobe.so").read_bytes()
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    record = dict(schema="fair-final-global-rng-snapshot-launch.v1", remote=remote, settings=settings,
        uploaded_sha256=identities, frozen_runtime_sha256=source["frozen_runtime_sha256"],
        build_manifest_sha256=sha(build / "build-manifest.json"), build_manifest=build_record,
        parent_launch_sha256=sha(parent / "launch.json"), runner_sha256=sha(__file__),
        contract_sha256=sha(ROOT / "CONTRACT.md"), training_admissible=False, gates_closed=False)
    (output / "registration.json").write_text(json.dumps(record, indent=2) + "\n")
    client = connect()
    try:
        execute(client, f'''
import hashlib,shutil,socket
from pathlib import Path
root=Path({remote!r});project=Path({settings['project']!r})
with socket.socket() as check:check.bind(('127.0.0.1',{PORT}))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if memory<32*1024**3 or shutil.disk_usage('/home/zhichun').free<5*1024**3:raise ValueError('snapshot diagnostic resources insufficient')
for name,expected in {source['frozen_runtime_sha256']!r}.items():
 if hashlib.sha256((project/name).read_bytes()).hexdigest()!=expected:raise ValueError('snapshot frozen runtime differs')
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
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('snapshot upload differs')
environment=dict(os.environ,PYTHONPATH={settings['project']!r})
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
        raise ValueError("snapshot external launch identity differs")
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
                    raise ValueError("existing snapshot archive differs")
                target.write_bytes(content)
                files[name] = hashlib.sha256(content).hexdigest()
        receipt = dict(schema="fair-final-global-rng-snapshot-download.v1", launch_sha256=launch_sha,
            files=files, source_worker_stopped=True, training_admissible=False, gates_closed=False)
        target = attempt / "download.json"
        if target.exists() and json.loads(target.read_text()) != receipt:
            raise ValueError("snapshot download receipt differs")
        target.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_pending_verification", files=len(files), receipt_sha256=sha(target))))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("build", type=Path)
    start.add_argument("parent", type=Path)
    download = commands.add_parser("collect")
    download.add_argument("attempt", type=Path)
    download.add_argument("--launch-sha", required=True)
    arguments = parser.parse_args()
    if arguments.command == "launch":
        launch(arguments.build, arguments.parent)
    else:
        collect(arguments.attempt, arguments.launch_sha)
