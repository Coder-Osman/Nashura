import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent
VERSIONS = ROOT.parent
sys.path[:0] = [str(VERSIONS / name) for name in (
    "fair-final-random-execution-v1", "fair-final-general-fallback-v1",
    "fair-final-global-rng-snapshot-v1")]
from run_trace import connect, execute, PYTHON
from run_native_general import BASE, refill_runner
from verify_snapshot import validate as validate_parent


PORTS = [29791, 29792]
PARENT = VERSIONS / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000"
PARENT_LAUNCH = "e0edd59a2f1d353513393ccf28a26858045d41102ac3ca906ffc89674ae13a3a"
PARENT_RECEIPT = "81a365987f351d08d46c50e1d2c3d486b17d8a97494bea6331d55304cd6e468a"
BUILD = VERSIONS / "fair-final-global-rng-snapshot-v1/build-1792733963764581000"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def payloads(remote):
    sources = refill_runner.source_manifest()
    files = {"project/" + name: (refill_runner.ROOT / "project" / name).read_bytes()
        for name in sources}
    files.update(refill_runner.payloads(remote, sha(BUILD / "libcrprobe.so"), sources))
    files["libcrprobe.so"] = (BUILD / "libcrprobe.so").read_bytes()
    for name in ("project/run.py", "run-manifest.json", "engine-build-manifest.json"):
        del files[name]
    for name in ("general_provider.py", "probe_native_general.py"):
        files["project/" + name] = (VERSIONS / "fair-final-general-fallback-v1" / name).read_bytes()
    for name in ("probe_search.py", "snapshot_audit.py"):
        files["project/" + name] = (ROOT / name).read_bytes()
    sources.update({name.removeprefix("project/"): hashlib.sha256(content).hexdigest()
        for name, content in files.items() if name in {"project/" + entry for entry in
            ("general_provider.py", "probe_native_general.py", "probe_search.py", "snapshot_audit.py")}})
    files["project/probe-source-manifest.json"] = json.dumps(sources, sort_keys=True).encode()
    files["worker.py"] = (ROOT / "worker.py").read_bytes()
    files["selected_collection_worker.py"] = (VERSIONS /
        "fair-final-migration-integration-v1/selected_collection_worker.py").read_bytes()
    return files, sources


def prepare():
    parent = validate_parent(PARENT, PARENT_LAUNCH, PARENT_RECEIPT)
    engine_sha = sha(BUILD / "libcrprobe.so")
    if engine_sha != parent["engine_sha256"]:
        raise ValueError("global search parent engine differs")
    attempt = ROOT / ("native-global-search-" + str(time.time_ns()))
    remote = "/home/zhichun/fair-final-global-search-v1/" + attempt.name
    files, sources = payloads(remote)
    settings = dict(root=remote, base=BASE, ports=PORTS, engine_sha256=engine_sha,
        source_sha256=sources, payload_sha256={name: hashlib.sha256(content).hexdigest()
            for name, content in files.items()})
    files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    record = dict(schema="FairFinalGlobalSearchPreparation/v1", remote=remote, settings=settings,
        payload_sha256={name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        parent_launch_sha256=PARENT_LAUNCH, parent_receipt_sha256=PARENT_RECEIPT,
        parent_verification=parent, runner_sha256=sha(__file__),
        verifier_sha256=sha(ROOT / "verify_search.py"), contract_sha256=sha(ROOT / "CONTRACT.md"),
        status="prepared_not_launched", training_admissible=False, gates_closed=False)
    attempt.mkdir()
    for name, content in files.items():
        destination = attempt / "payload" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (attempt / "preparation.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(dict(attempt=str(attempt), status=record["status"],
        preparation_sha256=sha(attempt / "preparation.json"), files=len(files))))


def preparation(attempt, expected):
    if sha(attempt / "preparation.json") != expected:
        raise ValueError("global search external preparation identity differs")
    record = json.loads((attempt / "preparation.json").read_text())
    if (record["schema"] != "FairFinalGlobalSearchPreparation/v1"
            or record["remote"] != "/home/zhichun/fair-final-global-search-v1/" + attempt.name
            or record["settings"]["ports"] != PORTS
            or record["runner_sha256"] != sha(__file__)
            or record["verifier_sha256"] != sha(ROOT / "verify_search.py")
            or record["contract_sha256"] != sha(ROOT / "CONTRACT.md")
            or record["parent_launch_sha256"] != PARENT_LAUNCH
            or record["parent_receipt_sha256"] != PARENT_RECEIPT
            or record["parent_verification"] != validate_parent(PARENT, PARENT_LAUNCH, PARENT_RECEIPT)
            or record["status"] != "prepared_not_launched"
            or record["training_admissible"] is not False or record["gates_closed"] is not False):
        raise ValueError("global search preparation implementation differs")
    files, sources = payloads(record["remote"])
    files["settings.json"] = json.dumps(record["settings"], sort_keys=True).encode()
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    if (record["payload_sha256"] != identities or record["settings"]["source_sha256"] != sources
            or record["settings"]["root"] != record["remote"] or record["settings"]["base"] != BASE
            or record["settings"]["payload_sha256"] != {name: value for name, value in identities.items()
                if name != "settings.json"}
            or record["settings"]["engine_sha256"] != sha(BUILD / "libcrprobe.so")):
        raise ValueError("global search preparation payload identity differs")
    for name, expected_sha in identities.items():
        if sha(attempt / "payload" / name) != expected_sha:
            raise ValueError("global search prepared payload changed")
    return record


def launch(attempt, preparation_sha):
    record = preparation(attempt, preparation_sha)
    if (attempt / "launch.json").exists():
        raise ValueError("global search attempt already launched; inspect same worker")
    client = connect()
    remote = record["remote"]
    try:
        state = json.loads(execute(client, f'''
from contextlib import ExitStack
import json,shutil,socket
from pathlib import Path
root=Path({remote!r})
with ExitStack() as stack:
 for port in {PORTS!r}:stack.enter_context(socket.socket()).bind(('127.0.0.1',port))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if memory<32*1024**3 or free<5*1024**3:raise ValueError('global search resources insufficient')
root.mkdir(parents=True,exist_ok=False)
print(json.dumps(dict(memory_available=memory,free_bytes=free)))
'''))
        (attempt / "preflight.json").write_text(json.dumps(state, indent=2) + "\n")
        with client.open_sftp() as sftp:
            directories = {remote}
            for name in record["payload_sha256"]:
                for parent in reversed(Path(name).parents):
                    destination = remote + "/" + str(parent)
                    if str(parent) != "." and destination not in directories:
                        sftp.mkdir(destination)
                        directories.add(destination)
                sftp.put(str(attempt / "payload" / name), remote + "/" + name)
        process = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {record['payload_sha256']!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('global search upload differs')
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{PYTHON!r},'-B','-u',str(root/'worker.py'),str(root/'settings.json')],cwd=root,env=dict(os.environ,PYTHONPATH=str(root/'project'),PYTHONDONTWRITEBYTECODE='1'),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        launch_record = dict(record, schema="FairFinalGlobalSearchLaunch/v1", process=process,
            preparation_sha256=preparation_sha, preflight=state, status="launched")
        (attempt / "launch.json").write_text(json.dumps(launch_record, indent=2) + "\n")
        print(json.dumps(dict(status="running_requires_inspection", attempt=str(attempt),
            launch_sha256=sha(attempt / "launch.json"), process=process)))
    finally:
        client.close()


def collect(attempt, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("global search external launch identity differs")
    record = json.loads((attempt / "launch.json").read_text())
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import json,time
from pathlib import Path
root=Path({record['remote']!r});process=Path('/proc')/str({record['process']['pid']!r})
live=False
if process.exists():
 fields=(process/'stat').read_text().rsplit(')',1)[1].split()
 if fields[19]!={record['process']['start_ticks']!r}:raise ValueError('global search worker PID reused')
 live=fields[0]!='Z'
files={{}}
for name in ('completion.json','failure.json','tick-110-seat-0/progress.json','tick-110-seat-0/failure.json'):
 if (root/name).exists():files[name]=json.loads((root/name).read_text())
print(json.dumps(dict(live=live,timestamp=time.time(),files=files)))
'''))
        (attempt / ("poll-" + str(time.time_ns()) + ".json")).write_text(json.dumps(state, indent=2))
        if state["live"]:
            print(json.dumps(dict(status="running", timestamp=state["timestamp"],
                completed_roots=len(state["files"].get("tick-110-seat-0/progress.json", {}).get("records", [])))))
            return
        names = [*record["payload_sha256"], "worker.log", "completion.json", "failure.json"]
        prefix = "tick-110-seat-0/"
        names.extend(prefix + name for name in ("report.json", "progress.json", "failure.json",
            "cleanup.json", "status.json", "probe.log", "probe-process.json",
            "history-initial-0.json", "history-initial-1.json", "history-successor-0.json", "history-successor-1.json"))
        for port in PORTS:
            names.extend(prefix + name for name in ("engine-" + str(port) + ".log",
                "engine-" + str(port) + "/process.json"))
        files = {}
        with client.open_sftp() as sftp:
            for name in names:
                try:
                    source = sftp.open(record["remote"] + "/" + name, "rb")
                except FileNotFoundError:
                    continue
                with source:
                    content = source.read(64 * 1024**2 + 1)
                if len(content) > 64 * 1024**2:
                    raise ValueError("global search archive exceeds member bound")
                destination = attempt / "archive" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.read_bytes() != content:
                    raise ValueError("global search existing archive changed")
                destination.write_bytes(content)
                files[name] = hashlib.sha256(content).hexdigest()
        receipt = dict(schema="FairFinalGlobalSearchDownload/v1", launch_sha256=launch_sha,
            worker_stopped=True, files=files, training_admissible=False, gates_closed=False)
        (attempt / "download.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(dict(status="archived_requires_independent_verification",
            receipt_sha256=sha(attempt / "download.json"), files=len(files))))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    for name in ("launch", "collect"):
        subparser = commands.add_parser(name)
        subparser.add_argument("attempt", type=Path)
        subparser.add_argument("--" + ("preparation-sha" if name == "launch" else "launch-sha"), required=True)
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        prepare()
    elif arguments.command == "launch":
        launch(arguments.attempt, arguments.preparation_sha)
    else:
        collect(arguments.attempt, arguments.launch_sha)
