import json
from pathlib import Path
import shlex
import hashlib
import time

import paramiko


WORKSPACE = Path(__file__).resolve().parents[3]
SOURCE = WORKSPACE / "firstlight_CR"
REMOTE = "/home/zhichun/fair-final-v1-20261019-p1"
BASE = "/home/zhichun/fair-teacher-r0-vs-m0-20261014"
PYTHON = "/home/zhichun/firstlight_CR/.venv/bin/python"


def main():
    row = next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines() if "| 172.16.48.35 |" in line)
    fields = [field.strip() for field in row.split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        preparation = f"""
from pathlib import Path
import shutil
root=Path({REMOTE!r})
root.mkdir(exist_ok=True)
(root/'project').mkdir(exist_ok=True)
target=root/'project/native_runner'
if not target.exists(): shutil.copytree({BASE + '/project/native_runner'!r},target)
(target/'training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir(exist_ok=True)
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=30)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        with client.open_sftp() as sftp:
            files = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
            files.append(SOURCE / "tools/probe_fair_final_timing.py")
            identities = {}
            for local in files:
                relative = str(local.relative_to(SOURCE))
                content = local.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                target = REMOTE + "/project/" + relative
                with sftp.open(target, "wb") as handle:
                    handle.write(content)
                with sftp.open(target, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("deployed probe source identity differs")
            with sftp.open(REMOTE + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            runner = f"""
import hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path({REMOTE!r})
identities=json.loads((root/'project/probe-source-manifest.json').read_text())
for relative,expected_source in identities.items():
 if hashlib.sha256((root/'project'/relative).read_bytes()).hexdigest()!=expected_source: raise ValueError('deployed source changed')
probe=Path({BASE + '/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
expected='0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807'
if hashlib.sha256(probe.read_bytes()).hexdigest()!=expected: raise ValueError('probe identity changed')
environment=dict(os.environ,CR_PROBE=str(probe))
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
with (root/'engine.log').open('a') as log:
 engine=subprocess.Popen([sys.executable,{BASE + '/isolated_engine.py'!r},'--project',{BASE + '/project'!r},'--port','29619','--instance',str(root/'engine')],stdout=log,stderr=subprocess.STDOUT)
 try:
  wait_for_engine(engine,root/'engine',timeout=120)
  result=subprocess.run([sys.executable,str(root/'project/tools/probe_fair_final_timing.py'),'--port','29619','--output',str(root/'native-timing-report.json'),'--puct-root',{BASE + '/project/research/puct-optimized'!r},'--source',{BASE + '/candidate.pt'!r}],timeout=180,env=environment)
  sys.exit(result.returncode)
 finally:
  engine.terminate()
  try: engine.wait(timeout=25)
  except subprocess.TimeoutExpired: engine.kill(); engine.wait()
"""
            with sftp.open(REMOTE + "/project/run.py", "w") as handle:
                handle.write(runner)
        command = shlex.join([PYTHON, REMOTE + "/project/run.py"])
        _, stdout, stderr = client.exec_command(command, timeout=360)
        output = stdout.read().decode()
        errors = stderr.read().decode()
        print(output)
        print(errors)
        code = stdout.channel.recv_exit_status()
        log = dict(remote=REMOTE, time=time.time(), exit_code=code, stdout=output, stderr=errors,
            source_sha256=identities)
        Path(__file__).with_name(f"native-probe-attempt-{time.time_ns()}.json").write_text(json.dumps(log, indent=2))
        print(json.dumps(dict(remote=REMOTE, exit_code=code)))
        with client.open_sftp() as sftp:
            sftp.get(REMOTE + "/engine/process.json", str(Path(__file__).with_name("native-probe-process.json")))
            if code == 0:
                sftp.get(REMOTE + "/native-timing-report.json", str(Path(__file__).with_name("native-timing-report.json")))
        raise SystemExit(code)
    finally:
        client.close()


if __name__ == "__main__":
    main()
