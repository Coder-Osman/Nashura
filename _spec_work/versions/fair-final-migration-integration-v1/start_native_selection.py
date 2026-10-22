import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"


def main():
    attempt = "native-selected-round0-" + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir()
    remote = "/home/zhichun/" + attempt
    parent = json.loads((EVIDENCE / "native-migration-1792548905478386000/launch.json").read_text())["remote"]
    files = {}
    source = WORKSPACE / "firstlight_CR"
    for path in (source / "native_runner").rglob("*.py"):
        if "tests" not in path.relative_to(source).parts:
            files["firstlight_CR/" + str(path.relative_to(source))] = path.read_bytes()
    for path in (source / "native_runner/data").rglob("*"):
        if path.is_file():
            files["firstlight_CR/" + str(path.relative_to(source))] = path.read_bytes()
    for name in ("fair_migration_supplement.py", "diagnose_fair_final_migration.py", "fair_migration_diagnostics.py"):
        files["firstlight_CR/tools/" + name] = (source / "tools" / name).read_bytes()
    for version in ("fair-final-migration-series-v1", "fair-final-migration-selection-v1", "fair-final-migration-integration-v1"):
        for path in (ROOT.parent / version).glob("*.py"):
            if not path.name.startswith("test_"):
                files["_spec_work/versions/" + version + "/" + path.name] = path.read_bytes()
    plan = json.loads((ROOT.parent / "fair-final-migration-series-v1/native-prefix-private-plan.json").read_text())
    plan["rounds"] = plan["rounds"][:1]
    files["private-plan.json"] = json.dumps(plan, sort_keys=True).encode()
    files["selection-config.json"] = json.dumps(dict(patience=2, min_delta=1e-6)).encode()
    files["loss-weights.json"] = json.dumps(dict(conditional_policy_kl=1., greedy_nll=.1, wdl_teacher_kl=1.)).encode()
    weights_source = (source / "native_runner/training/fair_migration.py").read_text()
    if "conditional_policy_kl" not in weights_source:
        raise ValueError("migration weights contract differs")
    identity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["deployment.json"] = json.dumps(identity, sort_keys=True).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    package = buffer.getvalue()
    (output / "payload.tar.gz").write_bytes(package)
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
  try:code=child.wait(timeout=1800)
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
            source_sha256=identity, training_admissible=False, formal_g2_pass=False)
        (output / "launch.json").write_text(json.dumps(launch, indent=2))
        print(json.dumps(dict(local=str(output), remote=remote, process=launch["process"])), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
