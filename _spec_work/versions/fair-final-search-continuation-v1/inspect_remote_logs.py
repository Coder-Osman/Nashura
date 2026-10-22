import json
from pathlib import Path
import shlex

import paramiko

from continuation_evidence import WORKSPACE
from run_native_probe import PYTHON


def main():
    row = next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line)
    fields = [value.strip() for value in row.split("|")]
    root = "/home/zhichun/fair-final-v1-20261019-search-episode/native-search-continuation-1792579051065329000"
    code = """
from pathlib import Path
import json, socket
root=Path("__ROOT__")
out={}
for name in ("worker.log","continue.log","engine-29751.log","engine-29752.log",
 "episode/progress.json","episode/continuation-failure.json","episode/segment-report.json"):
 p=root/name
 if p.exists():
  if p.suffix==".log": out[name]=p.read_text(errors="replace")[-5000:]
  else: out[name]=json.loads(p.read_text())
for port in (29751,29752):
 with socket.socket() as s:
  s.settimeout(1);out["port"+str(port)]=s.connect_ex(("127.0.0.1",port))==0
out["processes"]=[]
for p in Path("/proc").iterdir():
 if p.name.isdigit():
  try: value=(p/"cmdline").read_bytes().replace(b"\\0",b" ").decode()
  except (FileNotFoundError,PermissionError): continue
  if "native-search-continuation-1792579051065329000" in value:
   out["processes"].append(dict(pid=int(p.name),cmd=value[:700]))
print(json.dumps(out,ensure_ascii=False))
""".replace("__ROOT__", root)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", code]), timeout=30)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        print(json.dumps(json.loads(output), ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
