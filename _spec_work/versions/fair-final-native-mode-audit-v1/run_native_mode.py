import argparse
import hashlib
import importlib.util
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


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frozen_general_attempt", type=Path)
    args = parser.parse_args()
    parent = json.loads((args.frozen_general_attempt / "attempt.json").read_text())
    expected = run_native_general.refill_runner.source_manifest()
    expected.update({name: sha(GENERAL / name) for name in ("general_provider.py", "probe_native_general.py")})
    if parent["source_sha256"] != expected or parent["engine_sha256"] != run_native_general.validate_phase_engine_build(
            json.loads((run_native_general.refill_runner.BUILD / "build-manifest.json").read_text())):
        raise ValueError("mode audit requires the exact frozen general diagnostic runtime")
    output = ROOT / ("native-mode-" + str(time.time_ns()))
    output.mkdir()
    remote = "/home/zhichun/fair-final-native-mode-audit-v1/" + output.name
    project = parent["remote"] + "/project"
    python = run_native_general.PYTHON
    settings = dict(root=remote, project=project, base_project=run_native_general.BASE + "/project",
        puct_root=run_native_general.BASE + "/project/research/puct-optimized",
        port=29773, probe=parent["remote"] + "/libcrprobe.so", engine_sha256=parent["engine_sha256"])
    files = {name: (ROOT / name).read_bytes() for name in ("audit_mode.py", "probe_mode.py", "mode_worker.py")}
    files["settings.json"] = json.dumps(settings, sort_keys=True).encode()
    identities = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        def execute(code):
            _, stdout, stderr = client.exec_command(shlex.join([python, "-c", code]), timeout=60)
            raw, errors = stdout.read().decode(), stderr.read().decode()
            if stdout.channel.recv_exit_status():
                raise RuntimeError(errors)
            return raw
        execute(f'''
import hashlib,shutil,socket
from pathlib import Path
root=Path({remote!r});project=Path({project!r})
with socket.socket() as check:check.bind(('127.0.0.1',29773))
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if memory<32*1024**3 or shutil.disk_usage(root.parent.parent).free<5*1024**3:raise ValueError('mode audit resources insufficient')
for name,expected in {expected!r}.items():
 if hashlib.sha256((project/name).read_bytes()).hexdigest()!=expected:raise ValueError('frozen mode runtime identity differs')
root.mkdir(parents=True,exist_ok=False)
''')
        with client.open_sftp() as sftp:
            for name, content in files.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(content)
        process = json.loads(execute(f'''
import hashlib,json,os,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {identities!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('mode audit upload differs')
environment=dict(os.environ,PYTHONPATH={project!r})
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{python!r},'-u',str(root/'mode_worker.py'),str(root/'settings.json')],cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''))
        launch = dict(schema="fair-final-native-mode-launch.v1", remote=remote, process=process,
            settings=settings, uploaded_sha256=identities, frozen_runtime_sha256=expected,
            parent_attempt_sha256=sha(args.frozen_general_attempt / "attempt.json"),
            runner_sha256=sha(Path(__file__)), training_admissible=False, gates_closed=False)
        (output / "launch.json").write_text(json.dumps(launch, indent=2))
        print(json.dumps(dict(output=str(output), remote=remote, process=process)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
