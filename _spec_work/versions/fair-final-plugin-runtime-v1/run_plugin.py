import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-random-execution-v1"))
from run_trace import connect, execute, upload_sources, PYTHON
from trace_protocol import sha


def runtime_sources():
    files = upload_sources()
    files["verify_trace.py"] = (ROOT.parent / "fair-final-random-execution-v1/verify_trace.py").read_bytes()
    for name in ("plugin_launcher.py", "plugin_probe.py", "plugin_worker.py", "plugin_protocol.py", "plugin_audit.py", "plugin_seal.py", "random_entries.c"):
        files[name] = (ROOT / name).read_bytes()
    return files


def download(client, remote, attempt, names, launch_sha):
    files = {}
    with client.open_sftp() as sftp:
        for name in names:
            try:
                source = sftp.open(remote + "/" + name, "rb")
            except FileNotFoundError:
                continue
            destination = attempt / "archive" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".partial")
            digest, size = hashlib.sha256(), 0
            with source, temporary.open("xb") as output:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > 256 * 1024**2:
                        raise ValueError("plugin archive member exceeds file bound")
                    digest.update(block)
                    output.write(block)
            if destination.exists() and sha(destination) != digest.hexdigest():
                raise ValueError("existing plugin archive changed")
            temporary.replace(destination)
            files[name] = dict(bytes=size, sha256=digest.hexdigest())
    receipt = dict(schema="FairFinalPluginDownload/v1", launch_sha256=launch_sha, files=files,
        worker_stopped=True, training_admissible=False, gates_closed=False)
    (attempt / "download.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return sha(attempt / "download.json")


def launch(build_attempt, build_launch_sha, ancestor, ancestor_sha, *, reserve=False, port=29787, arm=False):
    if port not in (29787, 29789):
        raise ValueError("plugin measurement port not preallocated")
    if sha(build_attempt / "launch.json") != build_launch_sha or sha(ancestor / "launch.json") != ancestor_sha:
        raise ValueError("plugin parent launch identities differ")
    builder = json.loads((build_attempt / "launch.json").read_text())
    parent = json.loads((ancestor / "launch.json").read_text())
    client = connect()
    attempt = ROOT / ("native-plugin-" + str(time.time_ns()))
    remote = "/home/zhichun/fair-final-plugin-runtime-v1/" + attempt.name
    try:
        build = json.loads(execute(client, f'''
import hashlib,json
from pathlib import Path
root=Path({builder['remote']!r})
if (root/'failure.json').exists():raise ValueError('runtime builder failed')
result=json.loads((root/'completion.json').read_text())
if result['builder_sha256']!={builder['builder_sha256']!r}:raise ValueError('builder identity differs')
if hashlib.sha256(Path(result['qemu']).read_bytes()).hexdigest()!=result['qemu_sha256']:raise ValueError('built QEMU digest differs')
print(json.dumps(result))
'''))
        files = runtime_sources()
        sources = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
        settings = dict(parent["settings"], root=remote, port=port, source_sha256=sources,
            plugin_qemu=build["qemu"], plugin_qemu_sha256=build["qemu_sha256"],
            plugin=remote + "/random_entries.so")
        if reserve:
            settings["reserved_va"] = 1024**4
        if arm:
            settings["arm_after_ready"] = True
        execute(client, f"from pathlib import Path; Path({remote!r}).mkdir(parents=True,exist_ok=False)")
        attempt.mkdir(exist_ok=False)
        registration = dict(schema="FairFinalPluginLaunch/v1", remote=remote,
            build_launch_sha256=build_launch_sha, ancestor_launch_sha256=ancestor_sha,
            builder=builder, build=build, frozen_runtime_sha256=parent["frozen_runtime_sha256"],
            source_sha256=sources, training_admissible=False, gates_closed=False)
        (attempt / "registration.json").write_text(json.dumps(registration, indent=2) + "\n")
        with client.open_sftp() as sftp:
            for name, content in files.items():
                with sftp.open(remote + "/" + name, "wb") as output:
                    output.write(content)
        compiled = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r});build=Path({builder['remote']!r})
for name,expected in {sources!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('plugin source upload differs')
dependencies=build/'dependencies';library=dependencies/'usr/lib/x86_64-linux-gnu'
command=['gcc','-shared','-fPIC','-O2','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-fvisibility=hidden',
 '-I'+str(build/'qemu-10.0.13/include/qemu'),'-I'+str(dependencies/'usr/include/glib-2.0'),
 '-I'+str(library/'glib-2.0/include'),str(root/'random_entries.c'),'-L'+str(library),'-lglib-2.0','-o',str(root/'random_entries.so')]
with (root/'compile.log').open('xb') as log:subprocess.run(command,env=dict(os.environ,LD_LIBRARY_PATH=str(library)),stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
digest=hashlib.sha256((root/'random_entries.so').read_bytes()).hexdigest()
evidence=dict(command=command,plugin_sha256=digest,source_sha256={sources['random_entries.c']!r},header_sha256={build['plugin_header_sha256']!r})
(root/'compile.json').write_text(json.dumps(evidence,indent=2))
print(json.dumps(evidence))
'''))
        settings["plugin_sha256"] = compiled["plugin_sha256"]
        settings_content = json.dumps(settings, sort_keys=True).encode()
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/settings.json", "wb") as output:
                output.write(settings_content)
        process = json.loads(execute(client, f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'settings.json').read_bytes()).hexdigest()!={hashlib.sha256(settings_content).hexdigest()!r}:raise ValueError('plugin settings upload differs')
environment=dict(os.environ,PYTHONPATH={settings['project']!r},PYTHONDONTWRITEBYTECODE='1')
with (root/'worker.log').open('xb') as log:
 child=subprocess.Popen([{PYTHON!r},'-B','-u',str(root/'plugin_worker.py'),str(root/'settings.json')],cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        record = dict(registration, settings=settings, process=process, compile=compiled,
            settings_sha256=hashlib.sha256(settings_content).hexdigest())
        (attempt / "launch.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(dict(attempt=str(attempt), launch_sha256=sha(attempt / "launch.json"), process=process)))
    finally:
        client.close()


def collect(attempt, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("plugin pinned launch identity differs")
    record = json.loads((attempt / "launch.json").read_text())
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import json,time
from pathlib import Path
root=Path({record['remote']!r});process=Path('/proc')/str({record['process']['pid']!r});live=False
if process.exists():
 fields=(process/'stat').read_text().rsplit(')',1)[1].split()
 if fields[19]!={record['process']['start_ticks']!r}:raise ValueError('plugin worker PID reused')
 live=fields[0]!='Z'
files={{}}
for name in ('completion.json','failure.json',*(variant+'/'+name for variant in ('baseline','upgraded','plugin') for name in ('progress.json','phase-completion.json','failure.json','engine/process.json'))):
 if (root/name).exists():files[name]=json.loads((root/name).read_text())
print(json.dumps(dict(live=live,timestamp=time.time(),files=files)))
'''))
        (attempt / ("poll-" + str(time.time_ns()) + ".json")).write_text(json.dumps(state, indent=2) + "\n")
        if state["live"]:
            print(json.dumps(dict(status="running", timestamp=state["timestamp"], progress={
                key: value for key, value in state["files"].items() if key.endswith(("progress.json", "failure.json", "phase-completion.json"))})))
            return
        names = [*record["source_sha256"], "settings.json", "worker.log", "compile.json", "compile.log", "completion.json", "failure.json"]
        for variant in ("baseline", "upgraded", "plugin"):
            names += [variant + "/" + name for name in ("plugin-settings.json", "report.json", "measurements.json",
                "failure.json", "progress.json", "cleanup.json", "engine-owner.json", "engine.log", "probe.log",
                "probe-process.json", "qemu.log", "arm-base.txt", "trace-summary.json", "trace-seal.json", "address-evidence.json", "engine/process.json", "phase-completion.json")]
        receipt_sha = download(client, record["remote"], attempt, names, launch_sha)
        print(json.dumps(dict(status="archived_requires_independent_verification", receipt_sha256=receipt_sha)))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("build", type=Path)
    start.add_argument("--build-launch-sha", required=True)
    start.add_argument("--ancestor", type=Path, required=True)
    start.add_argument("--ancestor-sha", required=True)
    start.add_argument("--reserve-guest-va", action="store_true")
    start.add_argument("--arm-after-ready", action="store_true")
    start.add_argument("--port", type=int, default=29787)
    archive = commands.add_parser("collect")
    archive.add_argument("attempt", type=Path)
    archive.add_argument("--launch-sha", required=True)
    args = parser.parse_args()
    if args.command == "launch":
        launch(args.build, args.build_launch_sha, args.ancestor, args.ancestor_sha,
            reserve=args.reserve_guest_va, port=args.port, arm=args.arm_after_ready)
    else:
        collect(args.attempt, args.launch_sha)
