import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent
VERSIONS = ROOT.parent
sys.path[:0] = [str(VERSIONS / name) for name in (
    "fair-final-global-search-v1", "fair-final-random-execution-v1")]
from run_search import BUILD, PARENT, PARENT_LAUNCH, PARENT_RECEIPT, refill_runner, validate_parent
from run_trace import connect, execute, PYTHON


PORT = 29803
BASE_VERIFIER_SHA = "42acd66f7b779b8757318b9a6959b49ea770bfabecd77df14107817afbed2102"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def launch():
    if sha(VERSIONS / "fair-final-legal-reservations-v1/verify.py") != BASE_VERIFIER_SHA:
        raise ValueError("threshold base verifier identity differs")
    proof = validate_parent(PARENT, PARENT_LAUNCH, PARENT_RECEIPT)
    engine_sha = sha(BUILD / "libcrprobe.so")
    if proof["engine_sha256"] != engine_sha:
        raise ValueError("verified global snapshot engine required")
    attempt = ROOT / ("native-thresholds-" + str(time.time_ns()))
    remote = "/home/zhichun/fair-final-affordability-boundaries-v1/" + attempt.name
    sources = refill_runner.source_manifest()
    files = {"project/" + name: (refill_runner.ROOT / "project" / name).read_bytes()
        for name in sources}
    for name in ("probe.py", "cases.py"):
        files["project/" + name] = (ROOT / name).read_bytes()
    files["project/reservation_base.py"] = (VERSIONS / "fair-final-legal-reservations-v1/probe.py").read_bytes()
    sources.update({name: hashlib.sha256(files["project/" + name]).hexdigest()
        for name in ("probe.py", "cases.py", "reservation_base.py")})
    files.update({"project/probe-source-manifest.json": json.dumps(sources, sort_keys=True).encode(),
        "project/input-trace.json": refill_runner.TRACE.read_bytes(),
        "project/phase_snapshot_launcher.py": (refill_runner.EVIDENCE / "phase_snapshot_launcher.py").read_bytes(),
        "project/run.py": (VERSIONS / "fair-final-legal-reservations-v1/worker.py").read_bytes(),
        "libcrprobe.so": (BUILD / "libcrprobe.so").read_bytes(),
        "run-manifest.json": json.dumps(dict(root=remote, base=refill_runner.BASE,
            port=PORT, engine_sha256=engine_sha)).encode()})
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    attempt.mkdir()
    for name, content in files.items():
        destination = attempt / "payload" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    registration = dict(schema="FairFinalThresholdLaunch/v1", remote=remote, port=PORT,
        payload_sha=identities, source_files=sources, engine_sha=engine_sha,
        runner_sha=sha(__file__), verifier_sha=sha(ROOT / "verify.py"),
        base_verifier_sha=BASE_VERIFIER_SHA,
        contract_sha=sha(ROOT / "CONTRACT.md"), parent_launch_sha=PARENT_LAUNCH,
        parent_receipt_sha=PARENT_RECEIPT, training_admissible=False, gates_closed=False)
    (attempt / "registration.json").write_text(json.dumps(registration, indent=2))
    client = connect()
    try:
        preflight = json.loads(execute(client, f'''
import json,shutil,socket
from pathlib import Path
with socket.socket() as check:check.bind(('127.0.0.1',{PORT}))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if memory<32*1024**3 or free<5*1024**3:raise ValueError('threshold probe reserve lost')
Path({remote!r}).mkdir(parents=True,exist_ok=False)
print(json.dumps(dict(memory_available=memory,free_bytes=free)))
'''))
        with client.open_sftp() as sftp:
            directories = {remote}
            for name in files:
                for parent in reversed(Path(name).parents):
                    directory = remote + "/" + str(parent)
                    if str(parent) != "." and directory not in directories:
                        sftp.mkdir(directory)
                        directories.add(directory)
                sftp.put(str(attempt / "payload" / name), remote + "/" + name)
        process = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {identities!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('threshold upload differs')
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{PYTHON!r},'-B','-u',str(root/'project/run.py'),str(root/'run-manifest.json')],cwd=root/'project',env=dict(os.environ,PYTHONPATH=str(root/'project'),PYTHONDONTWRITEBYTECODE='1'),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        registration.update(process=process, preflight=preflight)
        (attempt / "launch.json").write_text(json.dumps(registration, indent=2))
        return dict(status="launched_requires_inspection", attempt=str(attempt), launch_sha=sha(attempt / "launch.json"))
    finally:
        client.close()


def collect(attempt, launch_sha):
    attempt = Path(attempt)
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("external threshold launch identity differs")
    record = json.loads((attempt / "launch.json").read_text())
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import json,time
from pathlib import Path
root=Path({record['remote']!r});process=Path('/proc')/str({record['process']['pid']!r})
alive=False
if process.exists():
 fields=(process/'stat').read_text().rsplit(')',1)[1].split()
 if fields[19]!={record['process']['start_ticks']!r}:raise ValueError('threshold PID reused')
 alive=fields[0]!='Z'
files={{name:json.loads((root/name).read_text()) for name in ('cleanup.json','progress.json','failure.json') if (root/name).exists()}}
print(json.dumps(dict(alive=alive,timestamp=time.time(),files=files)))
'''))
        (attempt / ("poll-" + str(time.time_ns()) + ".json")).write_text(json.dumps(state))
        if state["alive"]:
            return dict(status="running", **state)
        receipt = {}
        with client.open_sftp() as sftp:
            for name in ("report.json", "cleanup.json", "progress.json", "failure.json", "worker.log", "probe.log", "engine.log"):
                try:
                    with sftp.open(record["remote"] + "/" + name, "rb") as handle:
                        content = handle.read(64 * 1024**2 + 1)
                except FileNotFoundError:
                    continue
                if len(content) > 64 * 1024**2:
                    raise ValueError("threshold archive bound exceeded")
                destination = attempt / "archive" / name
                destination.parent.mkdir(exist_ok=True)
                if destination.exists() and destination.read_bytes() != content:
                    raise ValueError("threshold archive changed")
                if not destination.exists():
                    destination.write_bytes(content)
                receipt[name] = dict(sha256=sha(destination), bytes=len(content))
        result = dict(status="stopped_requires_verification", launch_sha=launch_sha,
            files=receipt, worker_stopped=True, training_admissible=False, gates_closed=False)
        destination = attempt / "receipt.json"
        if destination.exists() and json.loads(destination.read_text()) != result:
            raise ValueError("threshold receipt changed")
        if not destination.exists():
            destination.write_text(json.dumps(result, indent=2))
        return dict(status=result["status"], receipt_sha=sha(destination))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("launch", "collect"))
    parser.add_argument("attempt", type=Path, nargs="?")
    parser.add_argument("--launch-sha")
    args = parser.parse_args()
    print(json.dumps(launch() if args.command == "launch" else collect(args.attempt, args.launch_sha)))
