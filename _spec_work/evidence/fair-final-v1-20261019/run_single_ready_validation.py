import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ROOT = Path(__file__).resolve().parent
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
PORT = 29630


def main():
    run_id = f"native-single-ready-{time.time_ns()}"
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-single-ready/" + run_id
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
with socket.socket() as check: check.bind(('127.0.0.1',{PORT}))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<32*1024**3 or free<5*1024**3: raise RuntimeError('diagnostic resource threshold')
probe=Path({BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
engine_sha=hashlib.sha256(probe.read_bytes()).hexdigest()
if engine_sha!={ENGINE_SHA!r}: raise ValueError('engine identity differs')
root.mkdir(parents=True,exist_ok=False)
(root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256=engine_sha,port={PORT},
 adapter_sha256=hashlib.sha256(Path({BASE+'/project/research/puct-optimized/native_adapter.py'!r}).read_bytes()).hexdigest())
(root/'preflight.json').write_text(json.dumps(preflight,indent=2))
print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        print(stdout.read().decode(), flush=True)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        with client.open_sftp() as sftp:
            paths = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
            paths.append(SOURCE / "tools/probe_fair_final_single_ready.py")
            for path in paths:
                relative = str(path.relative_to(SOURCE))
                content = path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote + "/project/" + relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote + "/project/" + relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("deployed single-ready source differs")
            with sftp.open(remote + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            runner = f'''
import json,os,socket,subprocess,sys,time
from pathlib import Path
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
root=Path({remote!r})
instance=root/'engine'
engine=None
child=None
code=1
try:
 with (root/'engine.log').open('a') as log:
  engine=subprocess.Popen([sys.executable,{BASE+'/isolated_engine.py'!r},'--project',{BASE+'/project'!r},'--port',{str(PORT)!r},'--instance',str(instance)],stdout=log,stderr=subprocess.STDOUT)
 wait_for_engine(engine,instance,timeout=120)
 with (root/'probe.log').open('a') as log:
  child=subprocess.Popen([sys.executable,str(root/'project/tools/probe_fair_final_single_ready.py'),'--port',{str(PORT)!r},'--source',{BASE+'/candidate.pt'!r},'--puct-root',{BASE+'/project/research/puct-optimized'!r},'--output',str(root/'report.json')],stdout=log,stderr=subprocess.STDOUT,env=dict(os.environ,PYTHONUNBUFFERED='1',CR_PROBE={BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r}))
  try: code=child.wait(timeout=660)
  except subprocess.TimeoutExpired: raise TimeoutError('bounded single-ready diagnostic timeout')
finally:
 if child is not None and child.poll() is None:
  child.terminate()
  try: child.wait(timeout=15)
  except subprocess.TimeoutExpired: child.kill(); child.wait()
 if engine is not None:
  engine.terminate()
  try: engine.wait(timeout=25)
  except subprocess.TimeoutExpired: engine.kill(); engine.wait()
 process_path=instance/'process.json'
 process=json.loads(process_path.read_text()) if process_path.exists() else {{'phase':'missing'}}
 alive={{key:process.get(key) is not None and Path('/proc/'+str(process[key])).exists() for key in ('pid','child_pid')}}
 with socket.socket() as check: listening=check.connect_ex(('127.0.0.1',{PORT}))==0
 cleanup=dict(exit_code=code,port={PORT},process=process,pids_alive=alive,listening=listening)
 (root/'cleanup.json').write_text(json.dumps(cleanup,indent=2))
sys.exit(code if code else int(listening or any(alive.values()) or process['phase']!='stopped'))
'''
            with sftp.open(remote + "/project/run.py", "w") as handle:
                handle.write(runner)
        attempt = dict(remote=remote, source_sha256=identities, seats=[0, 1],
            modes=["staggered_wait", "early_public_act", "same_tick_public_act"])
        (output / "attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", remote=remote, output=str(output), port=PORT)), flush=True)
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, remote + "/project/run.py"]), timeout=840)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output / "attempt.json").write_text(json.dumps(dict(attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            names = [name for name in sftp.listdir(remote) if name.endswith((".json", ".log"))]
            names.extend(("project/run.py", "project/probe-source-manifest.json"))
            for name in names:
                sftp.get(remote + "/" + name, str(output / Path(name).name))
        print((output / "probe.log").read_text()[-10000:], flush=True)
        cleanup = json.loads((output / "cleanup.json").read_text())
        print(json.dumps(dict(cleanup={key: cleanup[key] for key in ("exit_code", "port", "pids_alive", "listening")},
            phase=cleanup["process"]["phase"])), flush=True)
        if code:
            raise SystemExit(code)
        if cleanup["exit_code"] or cleanup["listening"] or any(cleanup["pids_alive"].values()) or cleanup["process"]["phase"] != "stopped":
            raise ValueError("single-ready diagnostic cleanup incomplete")
        report = json.loads((output / "report.json").read_text())
        report["cleanup"] = cleanup
        report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["artifacts"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.iterdir() if path.is_file()}
        (ROOT / "native-single-ready-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", seats=len(report["records"]), seconds=report["seconds"])), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
