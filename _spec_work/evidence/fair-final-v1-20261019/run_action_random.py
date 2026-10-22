import hashlib
import json
from pathlib import Path
import shlex
import stat
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ROOT = Path(__file__).resolve().parent
PORT = 29649
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def main():
    run_id = f"native-action-random-{time.time_ns()}"
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-action-random/" + run_id
    fields = [field.strip() for field in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f'''
from pathlib import Path
import hashlib,json,shutil,socket
root=Path({remote!r})
with socket.socket() as check:check.bind(('127.0.0.1',{PORT}))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<32*1024**3 or free<5*1024**3:raise RuntimeError('action random resources insufficient')
probe=Path({BASE+'/project/research/standalone-resident-p18-stepatomic/libresident.so'!r})
engine_sha=hashlib.sha256(probe.read_bytes()).hexdigest()
if engine_sha!={ENGINE_SHA!r}:raise ValueError('action random engine identity differs')
root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True);(root/'project/tools').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256=engine_sha)
(root/'preflight.json').write_text(json.dumps(preflight,indent=2));print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        print(stdout.read().decode(), flush=True)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        worker = ROOT / "action_random_worker.py"
        with client.open_sftp() as sftp:
            paths = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
            paths.extend(SOURCE / "tools" / name for name in ("fair_final_prng_audit.py",
                "fair_final_random_census.py", "probe_fair_final_random_census.py",
                "fair_final_action_random.py", "probe_fair_final_action_random.py"))
            for path in paths:
                relative, content = str(path.relative_to(SOURCE)), path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote + "/project/" + relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote + "/project/" + relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("action random uploaded source differs")
            with sftp.open(remote + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            with sftp.open(remote + "/project/run.py", "wb") as handle:
                handle.write(worker.read_bytes())
            with sftp.open(remote + "/project/run.py", "rb") as handle:
                if handle.read() != worker.read_bytes():
                    raise ValueError("action random worker upload differs")
            with sftp.open(remote + "/run-manifest.json", "w") as handle:
                handle.write(json.dumps(dict(root=remote, base=BASE, port=PORT)))
        attempt = dict(remote=remote, port=PORT, source_sha256=identities,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest())
        (output / "attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", output=str(output), remote=remote)), flush=True)
        command = "cd " + shlex.quote(remote + "/project") + " && " + shlex.join(
            [PYTHON, "run.py", remote + "/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=1500)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output / "attempt.json").write_text(json.dumps(dict(attempt, exit_code=code,
            stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            def collect(directory, relative=""):
                for entry in sftp.listdir_attr(directory):
                    name = relative + entry.filename
                    if stat.S_ISDIR(entry.st_mode):
                        if entry.filename == "engine":
                            collect(directory + "/" + entry.filename, name + "/")
                    elif entry.filename.endswith((".json", ".log")):
                        destination = output / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        sftp.get(directory + "/" + entry.filename, str(destination))
            collect(remote)
            (output / "project").mkdir()
            for name in ("run.py", "probe-source-manifest.json"):
                sftp.get(remote + "/project/" + name, str(output / "project" / name))
        print((output / "probe.log").read_text()[-6000:], flush=True)
        if code:
            print(errors, flush=True)
            raise SystemExit(code)
        report = dict(schema="fair-final-action-random-suite.v1", status="passed", training_admissible=False,
            **attempt, report=json.loads((output / "report.json").read_text()),
            cleanup=json.loads((output / "cleanup.json").read_text()),
            artifacts={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()})
        target = ROOT / "native-action-random-report.json"
        target.write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", report=str(target))), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()

