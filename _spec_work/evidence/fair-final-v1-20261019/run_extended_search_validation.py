import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ROOT = Path(__file__).resolve().parent
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
JOBS = [dict(name=f"stage-{threshold}-seat-{owner}", threshold=threshold, owner=owner,
    ports=[29632 + index * 2, 29633 + index * 2])
    for index, (threshold, owner) in enumerate((threshold, owner) for threshold in (3600, 5400) for owner in (0, 1))]


def main():
    run_id = f"native-extended-search-{time.time_ns()}"
    output = ROOT / run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-extended-search/" + run_id
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
from contextlib import ExitStack
import hashlib,json,os,shutil,socket
root=Path({remote!r})
with ExitStack() as stack:
 for port in {[port for job in JOBS for port in job['ports']]!r}:
  check=stack.enter_context(socket.socket()); check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
if available<64*1024**3 or free<5*1024**3: raise RuntimeError('extended diagnostic resource threshold')
probe=Path({BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
engine_sha=hashlib.sha256(probe.read_bytes()).hexdigest()
if engine_sha!={ENGINE_SHA!r}: raise ValueError('engine identity differs')
root.mkdir(parents=True,exist_ok=False); (root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256=engine_sha,cpu_count=os.cpu_count(),
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
            paths.extend(SOURCE / "tools" / name for name in ("probe_fair_final_extended_search.py", "probe_fair_final_visibility.py"))
            for path in paths:
                relative = str(path.relative_to(SOURCE))
                content = path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote + "/project/" + relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote + "/project/" + relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("extended deployed source differs")
            with sftp.open(remote + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(identities, sort_keys=True))
            runner = f'''
import concurrent.futures,json,os,socket,subprocess,sys,time
from pathlib import Path
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
root=Path({remote!r})
jobs={JOBS!r}
environment=dict(os.environ,PYTHONUNBUFFERED='1',CR_PROBE={BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r},OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
def run_job(job):
 directory=root/job['name']; directory.mkdir()
 engines=[]; child=None; code=1; error=None
 try:
  for port in job['ports']:
   instance=directory/('engine-'+str(port))
   with (directory/('engine-'+str(port)+'.log')).open('a') as log:
    engine=subprocess.Popen([sys.executable,{BASE+'/isolated_engine.py'!r},'--project',{BASE+'/project'!r},'--port',str(port),'--instance',str(instance)],stdout=log,stderr=subprocess.STDOUT)
   engines.append((port,engine,instance))
   wait_for_engine(engine,instance,timeout=120)
  with (directory/'probe.log').open('a') as log:
   child=subprocess.Popen([sys.executable,str(root/'project/tools/probe_fair_final_extended_search.py'),'--port',str(job['ports'][0]),'--belief-port',str(job['ports'][1]),'--source',{BASE+'/candidate.pt'!r},'--puct-root',{BASE+'/project/research/puct-optimized'!r},'--threshold',str(job['threshold']),'--owner',str(job['owner']),'--output',str(directory/'report.json')],stdout=log,stderr=subprocess.STDOUT,env=environment)
   started=time.monotonic()
   while child.poll() is None:
    available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
    if available<32*1024**3: raise RuntimeError('memory reserve lost')
    if time.monotonic()-started>3600: raise TimeoutError('bounded extended search timeout')
    time.sleep(5)
   code=child.returncode
 except BaseException as failure: error=repr(failure)
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
   path=instance/'process.json'
   process=json.loads(path.read_text()) if path.exists() else {{'phase':'missing'}}
   alive={{key:process.get(key) is not None and Path('/proc/'+str(process[key])).exists() for key in ('pid','child_pid')}}
   with socket.socket() as check: listening=check.connect_ex(('127.0.0.1',port))==0
   results.append(dict(port=port,process=process,pids_alive=alive,listening=listening))
  cleanup=dict(exit_code=code,error=error,engines=results)
  (directory/'cleanup.json').write_text(json.dumps(cleanup,indent=2))
 return dict(job=job,exit_code=code,error=error,clean=len(results)==2 and all(not item['listening'] and not any(item['pids_alive'].values()) and item['process']['phase']=='stopped' for item in results))
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(run_job,jobs))
(root/'run-results.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results),flush=True)
sys.exit(int(any(item['exit_code'] or item['error'] or not item['clean'] for item in results)))
'''
            with sftp.open(remote + "/project/run.py", "w") as handle:
                handle.write(runner)
        attempt = dict(remote=remote, source_sha256=identities, jobs=JOBS, horizon="default-six-groups-200-ticks")
        (output / "attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", remote=remote, output=str(output), jobs=JOBS)), flush=True)
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, remote + "/project/run.py"]), timeout=4200)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output / "attempt.json").write_text(json.dumps(dict(attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            names = [name for name in sftp.listdir(remote) if name.endswith((".json", ".log"))]
            names.extend(("project/run.py", "project/probe-source-manifest.json"))
            for job in JOBS:
                names.extend(job["name"] + "/" + name for name in sftp.listdir(remote + "/" + job["name"])
                    if name.endswith((".json", ".log")))
            for name in names:
                destination = output / name
                destination.parent.mkdir(exist_ok=True, parents=True)
                sftp.get(remote + "/" + name, str(destination))
        for job in JOBS:
            print(job["name"], (output / job["name"] / "probe.log").read_text()[-7000:], flush=True)
        if code:
            raise SystemExit(code)
        reports = [dict(job=job, report=json.loads((output / job["name"] / "report.json").read_text()),
            cleanup=json.loads((output / job["name"] / "cleanup.json").read_text())) for job in JOBS]
        report = dict(schema="fair-final-extended-search-suite.v1", status="passed", training_admissible=False,
            source_sha256=identities, shards=reports, runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            artifacts={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()})
        (ROOT / "native-extended-search-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", roots=8, report=str(ROOT / "native-extended-search-report.json"))), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
