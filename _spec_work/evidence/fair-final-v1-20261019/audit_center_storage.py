import json
from pathlib import Path
import shlex
import time

import paramiko


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    fields = [part.strip() for part in next(line for line in
        (workspace / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = r'''
import json,os,shutil,subprocess,time
from pathlib import Path
def run(arguments, timeout=45):
 try:
  result=subprocess.run(arguments,capture_output=True,text=True,timeout=timeout)
  return dict(arguments=arguments,exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr)
 except subprocess.TimeoutExpired:
  return dict(arguments=arguments,timed_out=True)
report=dict(schema="fair-final-center-storage-audit.v1",host="172.16.48.35",timestamp=time.time(),
 read_only=True,minimum_center_free_gib=200,
 mounts=run(["findmnt","-J","-o","TARGET,SOURCE,FSTYPE,SIZE,AVAIL"]),
 blocks=run(["lsblk","-J","-b","-o","NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS"]),directories=[])
for path in sorted(Path("/home/zhichun").iterdir()):
 if path.is_dir() and not path.is_symlink() and not path.name.startswith("."):
  report["directories"].append(dict(path=str(path),usage=run(["du","-sx","--block-size=1",str(path)],30)))
report["filesystems"]=[]
for path in ("/","/home/zhichun","/data","/mnt","/media"):
 if os.path.isdir(path):
  usage=shutil.disk_usage(path)
  report["filesystems"].append(dict(path=path,device=os.stat(path).st_dev,total_gib=usage.total/1024**3,
   used_gib=usage.used/1024**3,free_gib=usage.free/1024**3,center_capacity_pass=usage.free>=200*1024**3))
print(json.dumps(report,sort_keys=True))
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join([
            "/home/zhichun/firstlight_CR/.venv/bin/python", "-u", "-c", probe]), timeout=600)
        encoded, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        if code:
            raise RuntimeError(f"storage probe exit {code}: {errors}")
        report = json.loads(encoded)
        destination = root / ("center-storage-" + str(time.time_ns()) + ".json")
        destination.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(json.dumps(dict(report=str(destination),filesystems=report["filesystems"],
            directories=len(report["directories"])),ensure_ascii=False),flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
