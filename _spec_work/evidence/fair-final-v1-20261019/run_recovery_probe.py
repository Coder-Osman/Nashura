import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ROOT = Path(__file__).resolve().parent
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def main():
    run_id = f"native-recovery-{time.time_ns()}"
    remote = "/home/zhichun/fair-final-v1-20261019-recovery/" + run_id
    output = ROOT / run_id
    output.mkdir()
    row = next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines() if "| 172.16.48.35 |" in line)
    fields = [field.strip() for field in row.split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f"""
from pathlib import Path
import shutil,socket
root=Path({remote!r})
root.mkdir(parents=True,exist_ok=False)
with socket.socket() as check: check.bind(('127.0.0.1',29621))
if shutil.disk_usage(root).free < 5*1024**3: raise RuntimeError('insufficient probe disk')
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if available < 32*1024**3: raise RuntimeError('insufficient available memory')
(root/'project').mkdir()
shutil.copytree({BASE + '/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir()
print({{'free_bytes':shutil.disk_usage(root).free,'memory_available':available}},flush=True)
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        print(stdout.read().decode(), flush=True)
        identities = {}
        with client.open_sftp() as sftp:
            sources = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
            sources.append(SOURCE / "tools/probe_fair_final_recovery.py")
            for path in sources:
                relative = str(path.relative_to(SOURCE))
                content = path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                target = remote + "/project/" + relative
                with sftp.open(target, "wb") as handle:
                    handle.write(content)
                with sftp.open(target, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("deployed recovery source mismatch")
            with sftp.open(remote + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            runner = f"""
import hashlib,json,os,socket,subprocess,sys,time,tarfile
from pathlib import Path
root=Path({remote!r})
probe=Path({BASE + '/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
if hashlib.sha256(probe.read_bytes()).hexdigest()!={ENGINE_SHA!r}: raise ValueError('engine identity changed')
environment=dict(os.environ,CR_PROBE=str(probe),PYTHONUNBUFFERED='1')
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
results=[]
for mode in ('baseline','recover'):
 with socket.socket() as check: check.bind(('127.0.0.1',29621))
 instance=root/('engine-'+mode)
 with (root/(mode+'-engine.log')).open('a') as log:
  engine=subprocess.Popen([sys.executable,{BASE + '/isolated_engine.py'!r},'--project',{BASE + '/project'!r},'--port','29621','--instance',str(instance)],stdout=log,stderr=subprocess.STDOUT)
  child=None
  try:
   wait_for_engine(engine,instance,timeout=120)
   command=[sys.executable,str(root/'project/tools/probe_fair_final_recovery.py'),'--mode',mode,'--port','29621','--output',str(root/'data'),'--source',{BASE + '/candidate.pt'!r},'--engine-sha256',{ENGINE_SHA!r}]
   with (root/(mode+'-probe.log')).open('a') as output:
    child=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,env=environment)
    started=time.monotonic()
    while child.poll() is None:
     if time.monotonic()-started>960: raise TimeoutError('bounded recovery worker exceeded wall limit')
     progress=root/'data'/(mode+'-progress.json')
     if progress.exists(): print(progress.read_text(),flush=True)
     time.sleep(10)
    code=child.returncode
  finally:
   if child is not None and child.poll() is None:
    child.terminate()
    try: child.wait(timeout=10)
    except subprocess.TimeoutExpired: child.kill(); child.wait()
   engine.terminate()
   try: engine.wait(timeout=25)
   except subprocess.TimeoutExpired: engine.kill(); engine.wait()
  process=json.loads((instance/'process.json').read_text())
  alive={{}}
  for key in ('pid','child_pid'):
   pid=process.get(key)
   alive[key]=pid is not None and Path('/proc/'+str(pid)).exists()
  with socket.socket() as check: listening=check.connect_ex(('127.0.0.1',29621))==0
  results.append(dict(mode=mode,exit_code=code,process=process,pids_alive=alive,listening=listening))
  print(json.dumps(results[-1]),flush=True)
  (root/'results.json').write_text(json.dumps(results,indent=2))
  if code or listening or any(alive.values()): break
with tarfile.open(root/'evidence.tar.gz','w:gz') as archive:
 for name in ('data','results.json','baseline-probe.log','recover-probe.log'):
  if (root/name).exists(): archive.add(root/name,arcname=name)
sys.exit(0 if len(results)==2 and all(item['exit_code']==0 and not item['listening'] and not any(item['pids_alive'].values()) for item in results) else 1)
"""
            with sftp.open(remote + "/project/run.py", "w") as handle:
                handle.write(runner)
        print(json.dumps(dict(remote=remote, output=str(output), status="running")), flush=True)
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, remote + "/project/run.py"]), timeout=2200)
        lines = []
        for line in stdout:
            print(line.rstrip(), flush=True)
            lines.append(line)
        errors = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        print(errors, flush=True)
        (output / "attempt.json").write_text(json.dumps(dict(remote=remote, exit_code=code,
            source_sha256=identities, stdout="".join(lines), stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            sftp.get(remote + "/evidence.tar.gz", str(output / "evidence.tar.gz"))
            sftp.get(remote + "/results.json", str(output / "results.json"))
            if code == 0:
                for name in ("baseline.json", "recover.json", "recovery-receipt.json"):
                    sftp.get(remote + "/data/" + name, str(output / name))
        if code:
            raise SystemExit(code)
        baseline = json.loads((output / "baseline.json").read_text())
        recovered = json.loads((output / "recover.json").read_text())
        if baseline["trajectory"] != recovered["trajectory"]:
            raise ValueError("downloaded native trajectories differ")
        artifacts = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.iterdir() if path.is_file()}
        report = dict(schema="fair-final-native-recovery-evidence.v1", status="passed",
            training_admissible=False, exact_continuation=True, source_sha256=identities, artifacts=artifacts,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            resume_tick=recovered["trajectory"]["boundary"]["tick"],
            terminal=recovered["trajectory"]["terminal"], replay_seconds=recovered["replay_seconds"],
            suffix_boundaries=recovered["suffix_boundaries"], total_decisions=recovered["total_decisions"],
            cleanup=json.loads((output / "results.json").read_text()),
            exclusions=["O(1) disk snapshot import", "real FULL128 search persistence", "training strength"])
        (ROOT / "native-recovery-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({key: value for key, value in report.items() if key not in
            ("source_sha256", "artifacts", "cleanup")}), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
