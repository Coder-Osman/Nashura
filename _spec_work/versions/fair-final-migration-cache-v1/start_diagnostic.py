import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko


ROOT = Path(__file__).resolve().parent


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--deterministic-samples", action="store_true")
    args = parser.parse_args()
    parent = ROOT / "native-cached-round0-1792632378773664000"
    launch = json.loads((parent / "launch.json").read_text())
    report = json.loads((parent / "native-evidence/round0.report.json").read_text())
    attempt = ROOT / ("native-resume-diagnostic-" + str(time.time_ns()))
    attempt.mkdir()
    remote = "/home/zhichun/" + attempt.name
    settings = dict(parent=launch["remote"], checkpoint_sha=report["checkpoint_sha"],
        deployment=launch["source_sha256"], data_root=launch["parent"] + "/data",
        source=launch["command"][launch["command"].index("--source") + 1],
        deterministic=args.deterministic_samples, samples_only=args.deterministic_samples,
        training_admissible=False, formal_g2_pass=False)
    files = {"diagnose_resume.py": (ROOT / "diagnose_resume.py").read_bytes(),
        "settings.json": json.dumps(settings, sort_keys=True).encode()}
    supervisor = '''import json,os,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parent
started=time.monotonic()
with (root/'diagnostic.log').open('x') as log:
 environment=dict(os.environ)
 if json.loads((root/'settings.json').read_text()).get('deterministic',False):environment['CUBLAS_WORKSPACE_CONFIG']=':4096:8'
 child=subprocess.Popen([__import__('sys').executable,'-B','-u',str(root/'diagnose_resume.py'),'--settings',str(root/'settings.json')],stdout=log,stderr=subprocess.STDOUT,env=environment)
 try:code=child.wait(timeout=5400)
 except subprocess.TimeoutExpired:
  child.terminate()
  try:child.wait(timeout=30)
  except subprocess.TimeoutExpired:child.kill();child.wait()
  code=124
(root/'completion.json').write_text(json.dumps(dict(exit_code=code,seconds=time.monotonic()-started,training_admissible=False,formal_g2_pass=False)))
'''
    files["supervisor.py"] = supervisor.encode()
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    fields = [part.strip() for part in next(line for line in (ROOT.parents[2] / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    python = "/home/zhichun/firstlight_CR/.venv/bin/python"
    try:
        with client.open_sftp() as sftp:
            sftp.mkdir(remote)
            for name, data in files.items():
                (attempt / name).write_bytes(data)
                with sftp.open(remote + "/" + name, "wb") as handle:
                    handle.write(data)
        command = f'''import hashlib,json,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {expected!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('diagnostic payload differs')
with (root/'supervisor.log').open('x') as log:
 process=subprocess.Popen([{python!r},'-B','-u',str(root/'supervisor.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps(dict(pid=process.pid,start_ticks=Path('/proc',str(process.pid),'stat').read_text().rsplit(')',1)[1].split()[19])))
'''
        _, out, err = client.exec_command(shlex.join([python, "-c", command]), timeout=30)
        raw, errors = out.read().decode(), err.read().decode()
        if out.channel.recv_exit_status():
            raise RuntimeError(errors)
        result = dict(remote=remote, process=json.loads(raw), uploaded_sha256=expected,
            source_sha256={}, training_admissible=False, formal_g2_pass=False)
        (attempt / "launch.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(dict(local=str(attempt), **result)), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
