import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


def main():
    evidence = Path(__file__).resolve().parent
    attempt = "native-search-episode-" + str(time.time_ns())
    output = evidence / attempt
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-search-episode/" + attempt
    files = {str(path.relative_to(SOURCE)):path for path in (SOURCE / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(SOURCE).parts}
    files["tools/probe_fair_final_search_episode.py"] = SOURCE / "tools/probe_fair_final_search_episode.py"
    identities = {name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in files.items()}
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload,mode="w:gz") as archive:
        for name,path in files.items():
            archive.add(path,arcname=name,recursive=False)
    probe = evidence / "phase-snapshot-engine-1792485763481586000/libcrprobe.so"
    settings = dict(base=BASE,ports=[29731,29732],engine_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
        wall_seconds=21600,training_admissible=False,actors=1)
    fields = [value.strip() for value in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35",username=fields[3],password=fields[4],timeout=12)
    try:
        preparation = f'''
from pathlib import Path
from contextlib import ExitStack
import json,shutil,socket
root=Path({remote!r})
with ExitStack() as stack:
 for port in {settings['ports']!r}:
  check=stack.enter_context(socket.socket());check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if available<32*1024**3 or shutil.disk_usage('/home/zhichun').free<5*1024**3: raise RuntimeError('engineering preflight failed')
root.mkdir(parents=True,exist_ok=False)
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner',ignore=shutil.ignore_patterns('__pycache__','tests','*.py'))
(root/'project/tools').mkdir()
print(json.dumps(dict(available_gib=available/1024**3,free_gib=shutil.disk_usage(root).free/1024**3)),flush=True)
'''
        _,stdout,stderr = client.exec_command(shlex.join([PYTHON,"-c",preparation]),timeout=60)
        resources,errors = stdout.read().decode(),stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        uploads = {"source.tar.gz":payload.getvalue(),"settings.json":json.dumps(settings).encode(),
            "project/probe-source-manifest.json":json.dumps(identities,sort_keys=True).encode(),
            "project/prior.json":(evidence / "public-deck-prior-engineering.json").read_bytes(),
            "project/phase_snapshot_launcher.py":(evidence / "phase_snapshot_launcher.py").read_bytes(),
            "project/worker.py":(evidence / "search_episode_worker.py").read_bytes(),
            "libcrprobe.so":probe.read_bytes()}
        with client.open_sftp() as sftp:
            for name,content in uploads.items():
                with sftp.open(remote+"/"+name,"wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/"+name,"rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != hashlib.sha256(content).hexdigest():
                        raise ValueError("search episode uploaded payload differs")
        deployment = f'''
import json,subprocess,tarfile
from pathlib import Path
root=Path({remote!r})
with tarfile.open(root/'source.tar.gz') as archive: archive.extractall(root/'project',filter='data')
with (root/'worker.log').open('x') as log:
 process=subprocess.Popen([{PYTHON!r},'-u',str(root/'project/worker.py'),str(root)],cwd=root/'project',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=process.pid,remote=str(root))),flush=True)
'''
        _,stdout,stderr = client.exec_command(shlex.join([PYTHON,"-c",deployment]),timeout=60)
        launched,errors = stdout.read().decode(),stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        report = dict(attempt=attempt,remote=remote,source_sha256=identities,settings=settings,
            payload_sha256={name:hashlib.sha256(content).hexdigest() for name,content in uploads.items()},
            process=json.loads(launched),resources=json.loads(resources))
        (output / "launch.json").write_text(json.dumps(report,indent=2,sort_keys=True))
        print(json.dumps(dict(local=str(output),**report["process"],resources=report["resources"])),flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
