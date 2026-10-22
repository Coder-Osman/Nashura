import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ROOT = Path(__file__).resolve().parent
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
PORTS = (29628, 29629)


def main():
    run_id = f"native-history-search-{time.time_ns()}"
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-history-search/" + run_id
    fields = [field.strip() for field in next(line for line in (WORKSPACE/"MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f'''
from pathlib import Path
import shutil,socket
root=Path({remote!r})
for port in {PORTS!r}:
 with socket.socket() as check: check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if available<32*1024**3 or shutil.disk_usage('/home/zhichun').free<5*1024**3: raise RuntimeError('probe resource threshold')
root.mkdir(parents=True,exist_ok=False)
(root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir()
print(dict(memory_available=available,free_bytes=shutil.disk_usage(root).free),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        print(stdout.read().decode(), flush=True)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        with client.open_sftp() as sftp:
            paths = list((SOURCE/"native_runner/training/fair_final").glob("*.py"))
            paths.extend(SOURCE/"tools"/name for name in ("probe_fair_final_history_search.py", "probe_fair_final_visibility.py"))
            for path in paths:
                relative = str(path.relative_to(SOURCE))
                content = path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote+"/project/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/project/"+relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("deployed history source differs")
            with sftp.open(remote+"/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            runner = f'''
import hashlib,json,os,socket,subprocess,sys,time
from pathlib import Path
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
root=Path({remote!r})
probe=Path({BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
if hashlib.sha256(probe.read_bytes()).hexdigest()!={ENGINE_SHA!r}: raise ValueError('engine identity differs')
environment=dict(os.environ,CR_PROBE=str(probe),PYTHONUNBUFFERED='1')
engines=[]
child=None
code=1
try:
 for port in {PORTS!r}:
  instance=root/('engine-'+str(port))
  with (root/('engine-'+str(port)+'.log')).open('a') as log:
   engine=subprocess.Popen([sys.executable,{BASE+'/isolated_engine.py'!r},'--project',{BASE+'/project'!r},'--port',str(port),'--instance',str(instance)],stdout=log,stderr=subprocess.STDOUT)
  engines.append((port,engine,instance))
  wait_for_engine(engine,instance,timeout=120)
 with (root/'probe.log').open('a') as log:
  child=subprocess.Popen([sys.executable,str(root/'project/tools/probe_fair_final_history_search.py'),'--port',{str(PORTS[0])!r},'--belief-port',{str(PORTS[1])!r},'--source',{BASE+'/candidate.pt'!r},'--puct-root',{BASE+'/project/research/puct-optimized'!r},'--output',str(root/'report.json')],stdout=log,stderr=subprocess.STDOUT,env=environment)
  started=time.monotonic()
  while child.poll() is None:
   if time.monotonic()-started>1800: raise TimeoutError('bounded native history timeout')
   time.sleep(5)
  code=child.returncode
finally:
 if child is not None and child.poll() is None:
  child.terminate()
  try: child.wait(timeout=15)
  except subprocess.TimeoutExpired: child.kill(); child.wait()
 results=[]
 for port,engine,instance in engines:
  engine.terminate()
  try: engine.wait(timeout=25)
  except subprocess.TimeoutExpired: engine.kill(); engine.wait()
  process_path=instance/'process.json'
  process=json.loads(process_path.read_text()) if process_path.exists() else {{'phase':'missing'}}
  alive={{key: process.get(key) is not None and Path('/proc/'+str(process[key])).exists() for key in ('pid','child_pid')}}
  with socket.socket() as check: listening=check.connect_ex(('127.0.0.1',port))==0
  results.append(dict(port=port,process=process,pids_alive=alive,listening=listening))
 (root/'cleanup.json').write_text(json.dumps(dict(exit_code=code,engines=results),indent=2))
sys.exit(code if code else int(any(entry['listening'] or any(entry['pids_alive'].values()) or entry['process']['phase']!='stopped' for entry in results)))
'''
            with sftp.open(remote+"/project/run.py", "w") as handle:
                handle.write(runner)
        print(json.dumps(dict(status="running", remote=remote, output=str(output), ports=PORTS)), flush=True)
        atomic_attempt = dict(remote=remote, source_sha256=identities, root_threshold=1200, seats=[0, 1], modes=[False, True])
        (output/"attempt.json").write_text(json.dumps(atomic_attempt, indent=2))
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, remote+"/project/run.py"]), timeout=2100)
        collected = stdout.read().decode()
        errors = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output/"attempt.json").write_text(json.dumps(dict(atomic_attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            names = [name for name in sftp.listdir(remote) if name.endswith((".json", ".log"))]
            names.extend(("project/run.py", "project/probe-source-manifest.json"))
            for name in names:
                sftp.get(remote+"/"+name, str(output/Path(name).name))
        print((output/"probe.log").read_text()[-18000:], flush=True)
        cleanup = json.loads((output/"cleanup.json").read_text())
        print(json.dumps(dict(cleanup=[dict(port=entry["port"], phase=entry["process"]["phase"],
            pids_alive=entry["pids_alive"], listening=entry["listening"]) for entry in cleanup["engines"]])), flush=True)
        if code:
            raise SystemExit(code)
        if (len(cleanup["engines"]) != 2 or any(entry["listening"] or any(entry["pids_alive"].values())
                or entry["process"]["phase"] != "stopped" for entry in cleanup["engines"])):
            raise ValueError("history probe engine cleanup is incomplete")
        report = json.loads((output/"report.json").read_text())
        report["cleanup"] = json.loads((output/"cleanup.json").read_text())
        report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["artifacts"] = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.iterdir() if path.is_file()}
        (ROOT/"native-history-search-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", checks=len(report["records"]), seconds=report["seconds"])), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
