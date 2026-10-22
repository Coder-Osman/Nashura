import argparse
import json
from pathlib import Path
import shlex
import time

import paramiko


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    launch = json.loads((args.attempt / "launch.json").read_text())
    workspace = Path(__file__).resolve().parents[3]
    fields = [part.strip() for part in next(line for line in (workspace / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = f'''
import hashlib,json,os,time
from pathlib import Path
root=Path({launch['remote']!r})
report=dict(timestamp=time.time(),remote=str(root),files={{}},processes={{}},checkpoints={{}})
processes=dict(worker={launch['process']!r})
for name in ('status.json','completion.json','phase-results.json','parent-receipt.json',
 'training-process.json','supervisor-failure.json','round0.report.json'):
 path=root/name
 if path.exists():report['files'][name]=json.loads(path.read_text())
if 'training-process.json' in report['files']:processes['training']=report['files']['training-process.json']
status=report['files'].get('status.json',{{}})
if 'process' in status:processes['phase']=status['process']
for label,process in processes.items():
 path=Path('/proc')/str(process['pid'])/'stat'
 current=dict(pid=process['pid'],alive=False)
 if path.exists():
  values=path.read_text().rsplit(')',1)[1].split()
  current.update(alive=values[19]==process['start_ticks'] and values[0]!='Z',
   start_ticks_match=values[19]==process['start_ticks'],state=values[0],
   cpu_seconds=(int(values[11])+int(values[12]))/os.sysconf('SC_CLK_TCK'),
   rss_gib=int(values[21])*os.sysconf('SC_PAGE_SIZE')/1024**3)
 report['processes'][label]=current
for name in ('training.log','supervisor.log','worker.log','reference.log','resumed.log'):
 path=root/name
 if path.exists():
  with path.open('rb') as handle:
   handle.seek(max(0,path.stat().st_size-3000))
   report['files'][name]=handle.read().decode(errors='replace')
for name in ('round0.pt','comparison/midpoint.pt','comparison/reference.pt','comparison/resumed.pt'):
 path=root/name
 if path.exists():report['checkpoints'][name]=dict(bytes=path.stat().st_size)
report['frozen_deployment_matches']=True
for name,expected in json.loads((root/'deployment.json').read_text()).items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:
  report['frozen_deployment_matches']=False
  report.setdefault('changed_files',[]).append(name)
print(json.dumps(report))
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join(["python3", "-c", probe]), timeout=30)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        report = json.loads(output)
        (args.attempt / ("status-" + str(time.time_ns()) + ".json")).write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
