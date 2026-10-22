import argparse
import json
from pathlib import Path
import shlex
import time

import paramiko

from build_payload import build, digest, ROOT, WORKSPACE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=21600)
    parser.add_argument("--device-index", type=int, default=1)
    args = parser.parse_args()
    if not 1800 <= args.timeout_seconds <= 28800 or not 0 <= args.device_index < 8:
        parser.error("bounded timeout or device index differs")
    parent = json.loads((args.parent / "launch.json").read_text())
    package, identity, changes = build(args.parent)
    attempt = "native-active-migration-" + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir()
    (output / "payload.tar.gz").write_bytes(package)
    (output / "integration-changes.json").write_text(json.dumps(changes, indent=2))
    remote = "/home/zhichun/" + attempt
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    source = "/home/zhichun/fair-teacher-r0-vs-m0-20261014/candidate.pt"
    data_root = parent["parent"] + "/data"
    common = ["--root", remote, "--data-root", data_root, "--source", source]
    benchmark = [python, "-B", "-u", remote + "/probe_active_batch.py", *common,
        "--output", remote + "/benchmark"]
    resume = [python, "-B", "-u", remote + "/probe_active_resume.py", *common]
    supervisor = f'''
import hashlib,json,os,subprocess,time
from pathlib import Path
root=Path({remote!r})
started=time.monotonic()
phases=[]
environment=dict(os.environ,CUDA_VISIBLE_DEVICES={str(args.device_index)!r},PYTHONDONTWRITEBYTECODE='1')
def run(phase,command):
 remaining={args.timeout_seconds}-(time.monotonic()-started)
 if remaining<=0:raise TimeoutError('bounded active verification exhausted')
 with (root/(phase+'.log')).open('x') as log:
  child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=root,env=environment)
  process=dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])
  (root/'status.json').write_text(json.dumps(dict(phase=phase,process=process,command=command,training_admissible=False)))
  try:code=child.wait(timeout=remaining)
  except subprocess.TimeoutExpired:
   child.terminate()
   try:child.wait(timeout=30)
   except subprocess.TimeoutExpired:child.kill();child.wait()
   code=124
 phases.append(dict(phase=phase,exit_code=code,process=process))
 (root/'phase-results.json').write_text(json.dumps(phases))
 if code:raise RuntimeError(phase+' failed: '+str(code))
try:
 run('benchmark',{benchmark!r})
 if json.loads((root/'benchmark/report.json').read_text())['status']!='passed_limited_scope':raise ValueError('benchmark failed')
 run('reference',{resume!r}+['--mode','reference'])
 reference_sha=hashlib.sha256((root/'active-comparison/reference.json').read_bytes()).hexdigest()
 for stage in ('frozen','trainable'):
  run(stage,{resume!r}+['--mode',stage,'--reference-sha',reference_sha])
 (root/'completion.json').write_text(json.dumps(dict(exit_code=0,seconds=time.monotonic()-started,training_admissible=False,formal_g2_pass=False)))
except Exception as error:
 (root/'supervisor-failure.json').write_text(json.dumps(dict(error=repr(error),seconds=time.monotonic()-started,training_admissible=False)))
 raise
'''
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        def execute(code):
            _, out, err = client.exec_command(shlex.join([python, "-c", code]), timeout=60)
            raw, errors = out.read().decode(), err.read().decode()
            if out.channel.recv_exit_status():
                raise RuntimeError(errors)
            return raw

        preparation = f'''
import json,shutil,subprocess
from pathlib import Path
root=Path({remote!r})
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
if memory<32 or shutil.disk_usage(root.parent).free<5*1024**3:raise ValueError('engineering resource floor failed')
gpu=subprocess.check_output(['nvidia-smi','-i',{str(args.device_index)!r},'--query-gpu=index,uuid,name,memory.used','--format=csv,noheader,nounits'],text=True).strip()
if int(gpu.rsplit(',',1)[1].strip())>1024:raise ValueError('selected GPU is occupied')
root.mkdir(exist_ok=False)
(root/'resources.json').write_text(json.dumps(dict(available_gib=memory,free_gib=shutil.disk_usage(root.parent).free/1024**3,gpu=gpu)))
'''
        execute(preparation)
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/payload.tar.gz", "wb") as handle:
                handle.write(package)
        deploy = f'''
import hashlib,json,subprocess,tarfile
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'payload.tar.gz').read_bytes()).hexdigest()!={digest(package)!r}:raise ValueError('payload differs')
with tarfile.open(root/'payload.tar.gz') as archive:archive.extractall(root,filter='data')
for name,expected in json.loads((root/'deployment.json').read_text()).items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('deployed source differs')
(root/'supervisor.py').write_text({supervisor!r})
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{python!r},'-B','-u',str(root/'supervisor.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''
        process = json.loads(execute(deploy))
        launch = dict(attempt=attempt, remote=remote, parent=parent["parent"], process=process,
            source_sha256=identity, payload_sha256=digest(package), integration_changes=changes,
            uploaded_sha256={"payload.tar.gz": digest(package)},
            supervisor_sha256=digest(supervisor.encode()), device_index=args.device_index,
            timeout_seconds=args.timeout_seconds, training_admissible=False, formal_g2_pass=False,
            parent_launch_sha256=digest((args.parent / "launch.json").read_bytes()))
        (output / "launch.json").write_text(json.dumps(launch, indent=2))
        print(json.dumps(dict(local=str(output), remote=remote, process=process)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
