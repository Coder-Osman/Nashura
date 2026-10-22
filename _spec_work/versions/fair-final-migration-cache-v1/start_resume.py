import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

import paramiko

from execution_budget import phase_timeout


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--phase-timeout-seconds", type=phase_timeout, default=10800)
    args = parser.parse_args()
    launch = json.loads((args.parent / "launch.json").read_text())
    payload = (args.parent / "payload.tar.gz").read_bytes()
    if hashlib.sha256(payload).hexdigest() != launch["payload_sha256"]:
        raise ValueError("frozen parent deployment payload differs")
    attempt = "native-cached-resume-" + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir()
    remote = "/home/zhichun/" + attempt
    probe_name = "_spec_work/versions/fair-final-migration-integration-v1/probe_native_resume.py"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        manifest = json.loads(archive.extractfile("deployment.json").read())
        if manifest != launch["source_sha256"]:
            raise ValueError("parent deployment manifest differs")
        if probe_name in manifest:
            probe = archive.extractfile(probe_name).read()
            if hashlib.sha256(probe).hexdigest() != manifest[probe_name]:
                raise ValueError("parent resume probe differs")
        else:
            probe = (ROOT.parent / "fair-final-migration-integration-v1/probe_native_resume.py").read_bytes()
    files = {"payload.tar.gz": payload, "resume_worker.py": (ROOT / "resume_worker.py").read_bytes(),
        probe_name: probe}
    settings = dict(parent=launch["remote"], parent_process=launch["process"], data_root=launch["parent"]+"/data",
        source=launch["command"][launch["command"].index("--source")+1],
        payload_sha256={name:hashlib.sha256(data).hexdigest() for name,data in files.items()},
        training_admissible=False, formal_g2_pass=False,
        parent_wait_seconds=int(launch["timeout_seconds"]) + 300,
        phase_timeout_seconds=args.phase_timeout_seconds)
    files["resume-settings.json"] = json.dumps(settings, sort_keys=True).encode()
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        preparation = f'''
from pathlib import Path
import shutil
root=Path({remote!r})
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
if memory<32 or shutil.disk_usage(root.parent).free<5*1024**3:raise ValueError('engineering resource floor failed')
root.mkdir(exist_ok=False)
(root/'_spec_work/versions/fair-final-migration-integration-v1').mkdir(parents=True)
'''
        _, out, err = client.exec_command(shlex.join([python, "-c", preparation]), timeout=30)
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        with client.open_sftp() as sftp:
            for name, data in files.items():
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(data)
        expected = {name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
        deploy = f'''
import hashlib,json,subprocess,tarfile
from pathlib import Path
root=Path({remote!r})
for name,expected in {expected!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('resume payload transfer differs')
with tarfile.open(root/'payload.tar.gz') as archive:archive.extractall(root,filter='data')
with (root/'worker.log').open('x') as log:
 process=subprocess.Popen([{python!r},'-B','-u',str(root/'resume_worker.py'),str(root)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=process.pid,start_ticks=Path('/proc',str(process.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''
        _, out, err = client.exec_command(shlex.join([python, "-c", deploy]), timeout=30)
        raw, errors = out.read().decode(), err.read().decode()
        if out.channel.recv_exit_status():
            raise RuntimeError(errors)
        result = dict(attempt=attempt, remote=remote, process=json.loads(raw),
            settings=settings, uploaded_sha256=expected, parent_launch_sha256=hashlib.sha256((args.parent/"launch.json").read_bytes()).hexdigest())
        (output/"launch.json").write_text(json.dumps(result,indent=2))
        for name in ("resume_worker.py", "resume-settings.json"):
            (output/name).write_bytes(files[name])
        (output/"probe_native_resume.py").write_bytes(files[probe_name])
        print(json.dumps(dict(local=str(output),remote=remote,process=result["process"])),flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
