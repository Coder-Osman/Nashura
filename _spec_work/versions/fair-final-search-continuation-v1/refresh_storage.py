import json
from pathlib import Path
import shlex
import time

import paramiko

from continuation_evidence import EVIDENCE, WORKSPACE
from native_runner.training.fair_final.episode import atomic_json
from run_native_probe import PYTHON


def main():
    fields = [part.strip() for part in next(line for line in
        (WORKSPACE / "MACHINES.md").read_text().splitlines() if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = '''
import json,os,shutil,subprocess,time
from pathlib import Path
paths=[]
for name in ('/','/home/zhichun','/data','/mnt','/media'):
 if Path(name).exists():
  usage=shutil.disk_usage(name)
  paths.append(dict(path=name,device=os.stat(name).st_dev,total_bytes=usage.total,free_bytes=usage.free,
   minimum_capacity_pass=usage.free>=200*1024**3))
report=dict(schema='fair-final-center-storage-refresh.v1',host='172.16.48.35',timestamp=time.time(),
 read_only=True,minimum_center_free_bytes=200*1024**3,two_generation_bytes=None,filesystems=paths,
 formal_storage_gate_passed=False)
for name,args in (('mounts',['findmnt','-J','-o','TARGET,SOURCE,FSTYPE,SIZE,AVAIL']),
 ('blocks',['lsblk','-J','-b','-o','NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS'])):
 result=subprocess.run(args,capture_output=True,text=True,timeout=15)
 if result.returncode:raise RuntimeError(name+' probe failed: '+result.stderr)
 report[name]=json.loads(result.stdout)
print(json.dumps(report),flush=True)
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", probe]), timeout=40)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        report = json.loads(output)
        path = EVIDENCE / ("center-storage-refresh-" + str(time.time_ns()) + ".json")
        atomic_json(path, report)
        print(json.dumps(dict(report=str(path), filesystems=report["filesystems"]), indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
