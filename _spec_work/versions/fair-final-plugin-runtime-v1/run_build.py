import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-random-execution-v1"))
from run_trace import connect, execute, PYTHON


def launch():
    attempt = ROOT / ("build-" + str(time.time_ns()))
    remote = "/home/zhichun/fair-final-plugin-runtime-v1/" + attempt.name
    source = (ROOT / "build_runtime.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    client = connect()
    try:
        execute(client, f"from pathlib import Path; Path({remote!r}).mkdir(parents=True,exist_ok=False)")
        attempt.mkdir(exist_ok=False)
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/build_runtime.py", "wb") as output:
                output.write(source)
        process = json.loads(execute(client, f'''
import hashlib,json,subprocess
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'build_runtime.py').read_bytes()).hexdigest()!={digest!r}:raise ValueError('builder upload digest differs')
with (root/'worker.log').open('xb') as log:
 child=subprocess.Popen([{PYTHON!r},'-B','-u',str(root/'build_runtime.py'),str(root)],cwd=root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        record = dict(remote=remote, process=process, builder_sha256=digest,
            training_admissible=False, gates_closed=False)
        (attempt / "launch.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(dict(attempt=str(attempt), **record)))
    finally:
        client.close()


def poll(attempt):
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
 if fields[19]!={record['process']['start_ticks']!r}:raise ValueError('builder PID reused')
 live=fields[0]!='Z'
files={{}}
for name in ('status.json','build-evidence.json','completion.json','failure.json'):
 if (root/name).exists():files[name]=json.loads((root/name).read_text())
tails={{name:(root/name).read_text(errors='replace')[-4000:] for name in ('worker.log','configure.log','build.log') if (root/name).exists()}}
print(json.dumps(dict(live=live,timestamp=time.time(),files=files,tails=tails)))
'''))
        (attempt / ("poll-" + str(time.time_ns()) + ".json")).write_text(json.dumps(state, indent=2) + "\n")
        print(json.dumps(state))
        return state
    finally:
        client.close()


def collect(attempt):
    state = poll(attempt)
    if state["live"]:
        raise ValueError("live runtime builder must not be archived as stopped")
    from run_plugin import download, sha
    record = json.loads((attempt / "launch.json").read_text())
    names = ["build_runtime.py", "worker.log", "status.json", "build-evidence.json", "completion.json", "failure.json",
        "configure.log", "build.log", "source-download.log", "qemu-help.txt"]
    names += [name for index in range(8) for name in ("package-" + str(index) + ".txt",
        "download-" + str(index) + ".log", "unpack-" + str(index) + ".log")]
    client = connect()
    try:
        receipt = download(client, record["remote"], attempt, names, sha(attempt / "launch.json"))
        print(json.dumps(dict(status="builder_archived", receipt_sha256=receipt)))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("launch", "poll", "collect"))
    parser.add_argument("attempt", type=Path, nargs="?")
    args = parser.parse_args()
    if args.command == "launch":
        launch()
    elif args.attempt is None:
        parser.error("poll requires an attempt")
    elif args.command == "poll":
        poll(args.attempt)
    else:
        collect(args.attempt)
