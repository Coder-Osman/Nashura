import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from execution_budget import training_timeout


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"


def main():
    import argparse
    from build_payload import build
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--timeout-seconds", type=training_timeout, default=5400)
    args = parser.parse_args()
    old_launch = json.loads((args.parent / "launch.json").read_text())
    attempt = ("native-deterministic-round0-" if args.deterministic else "native-cached-round0-") + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir()
    remote = "/home/zhichun/" + attempt
    parent = old_launch["parent"]
    package, identity, changes = build(args.parent, deterministic=args.deterministic)
    (output / "payload.tar.gz").write_bytes(package)
    (output / "integration-changes.json").write_text(json.dumps(changes, indent=2))
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    try:
        prepare = f'''
import shutil
from pathlib import Path
root=Path({remote!r})
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
if memory<32 or shutil.disk_usage(root.parent).free<5*1024**3:raise ValueError('engineering resource floor failed')
root.mkdir(exist_ok=False)
'''
        _, out, err = client.exec_command(shlex.join([python, "-c", prepare]), timeout=30)
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/payload.tar.gz", "wb") as handle:
                handle.write(package)
        command = [python, "-B", "-u", remote + "/_spec_work/versions/fair-final-migration-selection-v1/train_round.py",
            "--engineering", "--source", "/home/zhichun/fair-teacher-r0-vs-m0-20261014/candidate.pt",
            "--private-plan", remote + "/private-plan.json", "--data-roots", parent + "/data",
            "--selection-config", remote + "/selection-config.json", "--loss-weights", remote + "/loss-weights.json",
            "--output", remote + "/round0.pt", "--seed", "20261022", "--batch-size", "256",
            "--micro-batch-size", "1", "--device", "cuda:0", "--max-updates", "1"]
        supervisor = f'''
import json,subprocess,time
from pathlib import Path
root=Path({remote!r})
started=time.time()
try:
 with (root/'training.log').open('x') as log:
  child=subprocess.Popen({command!r},stdout=log,stderr=subprocess.STDOUT,cwd=root)
  (root/'training-process.json').write_text(json.dumps(dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
  try:code=child.wait(timeout={args.timeout_seconds})
  except subprocess.TimeoutExpired:
   child.terminate()
   try:child.wait(timeout=30)
   except subprocess.TimeoutExpired:child.kill();child.wait()
   code=124
 (root/'completion.json').write_text(json.dumps(dict(exit_code=code,seconds=time.time()-started,training_admissible=False,formal_g2_pass=False)))
except Exception as error:
 (root/'supervisor-failure.json').write_text(json.dumps(dict(error=repr(error),training_admissible=False)))
 raise
'''
        deploy = f'''
import hashlib,json,subprocess,tarfile
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'payload.tar.gz').read_bytes()).hexdigest()!={hashlib.sha256(package).hexdigest()!r}:raise ValueError('payload differs')
with tarfile.open(root/'payload.tar.gz') as archive:archive.extractall(root,filter='data')
for name,expected in json.loads((root/'deployment.json').read_text()).items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('deployed source differs')
(root/'supervisor.py').write_text({supervisor!r})
with (root/'supervisor.log').open('x') as log:
 process=subprocess.Popen([{python!r},'-B','-u',str(root/'supervisor.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=process.pid,start_ticks=Path('/proc',str(process.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''
        _, out, err = client.exec_command(shlex.join([python, "-c", deploy]), timeout=30)
        raw, errors = out.read().decode(), err.read().decode()
        if out.channel.recv_exit_status():
            raise RuntimeError(errors)
        launch = dict(attempt=attempt, remote=remote, parent=parent, process=json.loads(raw), command=command,
            payload_sha256=hashlib.sha256(package).hexdigest(), supervisor_sha256=hashlib.sha256(supervisor.encode()).hexdigest(),
            source_sha256=identity, training_admissible=False, formal_g2_pass=False,
            timeout_seconds=args.timeout_seconds, parent_launch_sha256=hashlib.sha256((args.parent / "launch.json").read_bytes()).hexdigest(),
            integration_changes=changes, deterministic=args.deterministic)
        (output / "launch.json").write_text(json.dumps(launch, indent=2))
        print(json.dumps(dict(local=str(output), remote=remote, process=launch["process"])), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
