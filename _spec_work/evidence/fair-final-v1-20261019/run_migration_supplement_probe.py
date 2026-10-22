import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko


def main():
    evidence = Path(__file__).resolve().parent
    workspace = evidence.parents[2]
    previous = evidence / "native-migration-1792548905478386000"
    parent = json.loads((previous / "launch.json").read_text())["remote"]
    attempt = "native-migration-supplement-" + str(time.time_ns())
    output = evidence / attempt
    output.mkdir()
    remote = parent + "/" + attempt
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    base = "/home/zhichun/fair-teacher-r0-vs-m0-20261014"
    checkpoint_sha = "bc8fd51cc2f7b483d39597643e2f4883fcb9a0b0875f19a5d0091f4c0131ca5a"
    engine_sha = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
    files = {"tools/" + name: workspace / "firstlight_CR/tools" / name for name in (
        "fair_migration_supplement.py", "diagnose_fair_final_migration.py", "fair_migration_diagnostics.py")}
    identities = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    worker = evidence / "migration_supplement_probe_worker.py"
    launch = dict(attempt=attempt, remote=remote, parent=parent, source_sha256=identities,
        worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(), checkpoint_sha256=checkpoint_sha,
        engine_sha256=engine_sha, engineering_only=True, training_admissible=False, port=29721)
    (output / "launch.json").write_text(json.dumps(launch, sort_keys=True, indent=2))
    fields = [value.strip() for value in next(line for line in (workspace / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        with client.open_sftp() as sftp:
            sftp.mkdir(remote)
            for name, path in files.items():
                destination = parent + "/project/" + name
                try:
                    with sftp.open(destination, "rb") as existing:
                        if hashlib.sha256(existing.read()).hexdigest() != identities[name]:
                            raise ValueError("existing supplementary source differs; use a separate project")
                except FileNotFoundError:
                    sftp.put(str(path), destination)
            sftp.put(str(worker), remote + "/worker.py")
        command = [python, "-u", remote + "/worker.py", "--root", remote, "--parent", parent,
            "--base", base, "--checkpoint-sha", checkpoint_sha, "--engine-sha", engine_sha]
        script = f'''
import hashlib,json,os,shutil,socket,subprocess,time
from pathlib import Path
root=Path({remote!r})
project=Path({parent!r})/'project'
manifest=json.loads((project/'source-manifest.json').read_text())
manifest.update({identities!r})
for name,expected in manifest.items():
 if hashlib.sha256((project/name).read_bytes()).hexdigest()!=expected: raise ValueError('frozen supplementary source differs')
if hashlib.sha256((root/'worker.py').read_bytes()).hexdigest()!={launch['worker_sha256']!r}: raise ValueError('worker source differs')
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
disk=shutil.disk_usage(root).free/1024**3
if memory<32 or disk<5: raise ValueError('engineering probe resource floor failed')
with socket.socket() as check: check.bind(('127.0.0.1',29721))
(root/'deployed-source.json').write_text(json.dumps(manifest,sort_keys=True,indent=2))
print(json.dumps(dict(attempt={attempt!r},status='started',available_gib=memory,free_gib=disk,formal_resource_gate=False)),flush=True)
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen({command!r},stdout=log,stderr=subprocess.STDOUT)
 (root/'worker-process.json').write_text(json.dumps(dict(pid=child.pid,command={command!r})))
 try: code=child.wait(timeout=2760)
 except subprocess.TimeoutExpired:
  child.terminate()
  try: child.wait(timeout=45)
  except subprocess.TimeoutExpired:
   child.kill();child.wait()
  code=124
(root/'completion.json').write_text(json.dumps(dict(exit_code=code,time=time.time(),training_admissible=False)))
print(json.dumps(dict(attempt={attempt!r},exit_code=code)),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([python, "-u", "-c", script]), timeout=2850)
        for line in stdout:
            print(line.rstrip(), flush=True)
        errors = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        if errors:
            print(errors, flush=True)
        with client.open_sftp() as sftp:
            def collect(directory, destination):
                destination.mkdir(exist_ok=True)
                for entry in sftp.listdir_attr(directory):
                    import stat
                    if stat.S_ISDIR(entry.st_mode):
                        if entry.filename.startswith("engine-"):
                            engine_dir = destination / entry.filename
                            engine_dir.mkdir(exist_ok=True)
                            sftp.get(directory + "/" + entry.filename + "/process.json", str(engine_dir / "process.json"))
                        else:
                            collect(directory + "/" + entry.filename, destination / entry.filename)
                    else:
                        sftp.get(directory + "/" + entry.filename, str(destination / entry.filename))
            collect(remote, output)
        if code or json.loads((output / "completion.json").read_text())["exit_code"]:
            raise RuntimeError((output / "worker.log").read_text()[-4000:])
        report = json.loads((output / "report.json").read_text())
        print(json.dumps(dict(status=report["status"],attempt=attempt,
            rows=sum(item["rows"] for item in report["reports"]),training_admissible=False)),flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
