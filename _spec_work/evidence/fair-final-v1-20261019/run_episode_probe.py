import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


REMOTE = "/home/zhichun/fair-final-v1-20261019-episode"
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--attempt-id", type=int, default=1)
    parser.add_argument("--wait-only", action="store_true")
    args = parser.parse_args()
    row = next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines() if "| 172.16.48.35 |" in line)
    fields = [field.strip() for field in row.split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        preparation = f"""
from pathlib import Path
import shutil,socket
root=Path({REMOTE!r})
root.mkdir(exist_ok=True)
with socket.socket() as check: check.bind(('127.0.0.1',29620))
if shutil.disk_usage(root).free < 5*1024**3: raise RuntimeError('insufficient probe disk')
(root/'project').mkdir(exist_ok=True)
target=root/'project/native_runner'
if not target.exists(): shutil.copytree({BASE + '/project/native_runner'!r},target)
(target/'training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir(exist_ok=True)
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=45)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        with client.open_sftp() as sftp:
            files = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
            files.append(SOURCE / "tools/probe_fair_final_episode.py")
            for local in files:
                relative = str(local.relative_to(SOURCE))
                content = local.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                target = REMOTE + "/project/" + relative
                with sftp.open(target, "wb") as handle:
                    handle.write(content)
                with sftp.open(target, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("deployed source mismatch")
            with sftp.open(REMOTE + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            suffix = ["--wait-only"] if args.wait_only else []
            runner = f"""
import hashlib,json,os,subprocess,sys,time,tarfile
from pathlib import Path
root=Path({REMOTE!r})
probe=Path({BASE + '/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
if hashlib.sha256(probe.read_bytes()).hexdigest()!={ENGINE_SHA!r}: raise ValueError('engine identity changed')
environment=dict(os.environ,CR_PROBE=str(probe),PYTHONUNBUFFERED='1')
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
attempt=root/'data/attempts'/{args.match_id!r}/{str(args.attempt_id)!r}
command=[sys.executable,str(root/'project/tools/probe_fair_final_episode.py'),'--port','29620','--output',str(root/'data'),'--source',{BASE + '/candidate.pt'!r},'--engine-sha256',{ENGINE_SHA!r},'--match-id',{args.match_id!r},'--attempt-id',{str(args.attempt_id)!r},'--engine-seed',{'78101907' if args.wait_only else '78101906'!r},'--execution-seed','78101946','--wall-seconds','900']+{suffix!r}
with (root/'engine.log').open('a') as log:
 engine=subprocess.Popen([sys.executable,{BASE + '/isolated_engine.py'!r},'--project',{BASE + '/project'!r},'--port','29620','--instance',str(root/'engine')],stdout=log,stderr=subprocess.STDOUT)
 child=None
 try:
  wait_for_engine(engine,root/'engine',timeout=120)
  started=time.monotonic()
  with (root/'probe.log').open('a') as output:
   child=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,env=environment)
   while child.poll() is None:
    if time.monotonic()-started>960: raise TimeoutError('bounded episode process exceeded wall limit')
    progress=attempt/'progress.json'
    if progress.exists(): print(progress.read_text(),flush=True)
    else: print(json.dumps(dict(status='starting',seconds=time.monotonic()-started)),flush=True)
    time.sleep(10)
   result=child.returncode
 finally:
  if child is not None and child.poll() is None:
   child.terminate()
   try: child.wait(timeout=10)
   except subprocess.TimeoutExpired: child.kill(); child.wait()
  engine.terminate()
  try: engine.wait(timeout=25)
  except subprocess.TimeoutExpired: engine.kill(); engine.wait()
 process=json.loads((root/'engine/process.json').read_text())
 print(json.dumps(dict(exit_code=result,engine_phase=process['phase'],pid=process['pid'],child_pid=process.get('child_pid'))),flush=True)
 if attempt.exists():
  with tarfile.open(root/{('episode-' + args.match_id + '-' + str(args.attempt_id) + '.tar.gz')!r},'w:gz') as archive:
   archive.add(attempt,arcname='attempt')
   audit=root/'data/audit'/{args.match_id!r}/{str(args.attempt_id)!r}
   if audit.exists(): archive.add(audit,arcname='audit')
 sys.exit(result)
"""
            with sftp.open(REMOTE + "/project/run_episode.py", "w") as handle:
                handle.write(runner)
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-u", REMOTE + "/project/run_episode.py"]), timeout=1150)
        output = []
        for line in stdout:
            output.append(line)
            print(line, end="", flush=True)
        errors = stderr.read().decode()
        print(errors, flush=True)
        code = stdout.channel.recv_exit_status()
        evidence = Path(__file__).parent
        atomic_name = f"episode-attempt-{args.match_id}-{args.attempt_id}-{time.time_ns()}"
        (evidence / (atomic_name + ".json")).write_text(json.dumps(dict(remote=REMOTE, exit_code=code,
            source_sha256=identities, stdout="".join(output), stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            for remote_name, local_name in [("engine/process.json", atomic_name + "-process.json"),
                    ("probe.log", atomic_name + "-probe.log"),
                    (f"episode-{args.match_id}-{args.attempt_id}.tar.gz", atomic_name + ".tar.gz")]:
                try:
                    sftp.get(REMOTE + "/" + remote_name, str(evidence / local_name))
                except FileNotFoundError:
                    pass
        raise SystemExit(code)
    finally:
        client.close()


if __name__ == "__main__":
    main()
