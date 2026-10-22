import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

import paramiko

from build_payload import build, MIGRATION, ROOT


WORKSPACE = ROOT.parents[2]
PROBE = "probe_native_batch.py"


def package_probe(parent):
    package, _, changes = build(parent)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    with tarfile.open(Path(parent) / "payload.tar.gz") as archive:
        files["reference_migration.py"] = archive.extractfile(MIGRATION).read()
    files[PROBE] = (ROOT / PROBE).read_bytes()
    for name in (PROBE, "reference_migration.py"):
        changes[name] = dict(before=None, after=hashlib.sha256(files[name]).hexdigest())
    identity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != "deployment.json"}
    files["deployment.json"] = json.dumps(identity, sort_keys=True).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            if name.endswith(".py"):
                compile(data, name, "exec")
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue(), identity, changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=14400)
    args = parser.parse_args()
    if not 1800 <= args.timeout_seconds <= 28800:
        parser.error("bounded timeout must be between 1800 and 28800 seconds")
    parent_launch = json.loads((args.parent / "launch.json").read_text())
    package, identity, changes = package_probe(args.parent)
    attempt = "native-batched-migration-" + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir()
    (output / "payload.tar.gz").write_bytes(package)
    (output / "integration-changes.json").write_text(json.dumps(changes, indent=2))
    remote = "/home/zhichun/" + attempt
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    source = "/home/zhichun/fair-teacher-r0-vs-m0-20261014/candidate.pt"
    data_root = parent_launch["parent"] + "/data"
    training = [python, "-B", "-u", remote + "/_spec_work/versions/fair-final-migration-selection-v1/train_round.py",
        "--engineering", "--source", source, "--private-plan", remote + "/private-plan.json",
        "--data-roots", data_root, "--selection-config", remote + "/selection-config.json",
        "--loss-weights", remote + "/loss-weights.json", "--seed", "20261022", "--batch-size", "256",
        "--micro-batch-size", "1", "--device", "cuda:0"]
    probe = [python, "-B", "-u", remote + "/" + PROBE, "--root", remote, "--data-root", data_root,
        "--source", source, "--output", remote + "/benchmark"]
    resume_probe = [python, "-B", "-u", remote + "/_spec_work/versions/fair-final-migration-integration-v1/probe_native_resume.py",
        "--root", remote, "--data-root", data_root, "--source", source, "--output", remote + "/comparison"]
    supervisor = f'''
import hashlib,json,shutil,subprocess,time
from pathlib import Path
root=Path({remote!r})
started=time.monotonic()
phases=[]
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def run(phase,command):
 remaining={args.timeout_seconds}-(time.monotonic()-started)
 if remaining<=0:raise TimeoutError('bounded experiment exhausted')
 with (root/(phase+'.log')).open('x') as log:
  child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=root)
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
 run('benchmark',{probe!r})
 if json.loads((root/'benchmark/report.json').read_text())['status']!='passed_limited_scope':raise ValueError('benchmark gate failed')
 run('training',{training!r}+['--output',str(root/'round0.pt'),'--max-updates','1'])
 run('reference',{resume_probe!r}+['--checkpoint',str(root/'round0.pt'),'--checkpoint-sha',digest(root/'round0.pt'),'--mode','reference'])
 run('resumed',{resume_probe!r}+['--checkpoint',str(root/'comparison/midpoint.pt'),'--checkpoint-sha',digest(root/'comparison/midpoint.pt'),
  '--mode','resumed','--reference-sha',digest(root/'comparison/reference.json')])
 shutil.copyfile(root/'comparison/reference.pt',root/'selected.pt')
 run('selection',{training!r}+['--output',str(root/'selected.pt'),'--resume-sha',digest(root/'selected.pt')])
 (root/'completion.json').write_text(json.dumps(dict(exit_code=0,seconds=time.monotonic()-started,training_admissible=False,formal_g2_pass=False)))
except Exception as error:
 (root/'supervisor-failure.json').write_text(json.dumps(dict(error=repr(error),seconds=time.monotonic()-started,training_admissible=False)))
 raise
'''
    try:
        prepare = f'''
import shutil
from pathlib import Path
root=Path({remote!r})
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
if memory<32 or shutil.disk_usage(root.parent).free<5*1024**3:raise ValueError('engineering resource floor failed')
root.mkdir(exist_ok=False)
'''
        def execute(code):
            _, out, err = client.exec_command(shlex.join([python, "-c", code]), timeout=30)
            raw, errors = out.read().decode(), err.read().decode()
            if out.channel.recv_exit_status():
                raise RuntimeError(errors)
            return raw
        execute(prepare)
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/payload.tar.gz", "wb") as handle:
                handle.write(package)
        deploy = f'''
import hashlib,json,subprocess,tarfile
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'payload.tar.gz').read_bytes()).hexdigest()!={hashlib.sha256(package).hexdigest()!r}:raise ValueError('payload differs')
with tarfile.open(root/'payload.tar.gz') as archive:archive.extractall(root,filter='data')
for name,expected in json.loads((root/'deployment.json').read_text()).items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('deployed source differs')
(root/'supervisor.py').write_text({supervisor!r})
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{python!r},'-B','-u',str(root/'supervisor.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''
        process = json.loads(execute(deploy))
        launch = dict(attempt=attempt, remote=remote, parent=parent_launch["parent"], process=process,
            source_sha256=identity, payload_sha256=hashlib.sha256(package).hexdigest(),
            supervisor_sha256=hashlib.sha256(supervisor.encode()).hexdigest(), integration_changes=changes,
            timeout_seconds=args.timeout_seconds, training_admissible=False, formal_g2_pass=False,
            parent_launch_sha256=hashlib.sha256((args.parent / "launch.json").read_bytes()).hexdigest())
        (output / "launch.json").write_text(json.dumps(launch, indent=2))
        print(json.dumps(dict(local=str(output), remote=remote, process=process)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
