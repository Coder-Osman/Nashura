import argparse
import json
from pathlib import Path
import shlex
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evidence/fair-final-v1-20261019"))

import paramiko

from run_native_probe import PYTHON, WORKSPACE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    launch = json.loads((args.attempt / "launch.json").read_text())
    fields = [part.strip() for part in next(line for line in
        (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = f'''
import json,os,shutil,socket,time
from pathlib import Path
root=Path({launch['remote']!r})
report=dict(timestamp=time.time(),remote=str(root),worker_pid={launch['process']['pid']!r},files={{}},ports={{}})
process=Path('/proc')/str(report['worker_pid'])
report['worker_alive']=process.exists()
if process.exists():
 actual_start=(process/'stat').read_text().rsplit(')',1)[1].split()[19]
 expected_start={launch['process']['start_ticks']!r}
 report['worker_alive']=actual_start==expected_start
 report['worker_pid_reused']=not report['worker_alive']
 if not report['worker_alive']:raise RuntimeError('continuation worker PID was reused')
if process.exists():
 report['worker_command']=(process/'cmdline').read_bytes().replace(b'\\0',b' ').decode()
report['processes']=[]
pending=[report['worker_pid']]
while pending:
 pid=pending.pop()
 directory=Path('/proc')/str(pid)
 try:
  stat=(directory/'stat').read_text().rsplit(')',1)[1].split()
  command=(directory/'cmdline').read_bytes().replace(b'\\0',b' ').decode()
  children=[int(value) for value in (directory/'task'/str(pid)/'children').read_text().split()]
  report['processes'].append(dict(pid=pid,state=stat[0],cpu_seconds=(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),
   rss_gib=int(stat[21])*os.sysconf('SC_PAGE_SIZE')/1024**3,command=command[:700]))
  pending.extend(children)
 except FileNotFoundError: pass
for name in ('worker.log','continue.log','phase-results.json','completion.json',
 'episode/recovery-continuation-report.json','episode/segment-report.json','episode/continuation-failure.json',
 'episode/progress.json','episode/prepare-report.json','episode/restart-receipt.json',
 'episode/recovery-search-report.json','episode/prepare-failure.json','episode/resume-failure.json'):
 path=root/name
 if path.exists():
  if path.suffix=='.log':
   with path.open('rb') as handle:
    handle.seek(max(0,path.stat().st_size-5000))
    value=handle.read().decode(errors='replace')
  else: value=json.loads(path.read_text())
  report['files'][name]=value
complete=root/'episode/complete-report.json'
if complete.exists():
 payload=json.loads(complete.read_text())
 report['complete_summary']={{key:value for key,value in payload.items() if key not in ('roots','evidence','source_sha256')}}
for port in {launch['settings']['ports']!r}:
 with socket.socket() as connection:
  connection.settimeout(1)
  report['ports'][str(port)]=connection.connect_ex(('127.0.0.1',port))==0
report['free_gib']=shutil.disk_usage(root).free/1024**3
report['available_gib']=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
print(json.dumps(report,sort_keys=True),flush=True)
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", probe]), timeout=30)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        report = json.loads(output)
        destination = args.attempt / ("status-" + str(time.time_ns()) + ".json")
        destination.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()

