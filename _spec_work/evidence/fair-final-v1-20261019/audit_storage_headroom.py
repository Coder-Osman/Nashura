import json
from pathlib import Path
import shlex
import time

import paramiko


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]


def main():
    fields = [part.strip() for part in next(line for line in
        (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    probe = r'''
import json, os, shutil, subprocess, time
from pathlib import Path
blocks = []
for disk in sorted(Path('/sys/block').iterdir()):
 if not disk.name.startswith(('sd', 'nvme', 'vd')): continue
 sectors = int((disk/'size').read_text())
 partitions = []
 for child in sorted(disk.iterdir()):
  if not (child/'partition').exists(): continue
  start, size = int((child/'start').read_text()), int((child/'size').read_text())
  partitions.append(dict(name=child.name,start_sector=start,sectors=size,end_sector_exclusive=start+size))
 last = max((part['end_sector_exclusive'] for part in partitions),default=0)
 blocks.append(dict(name=disk.name,sectors=sectors,sector_unit_bytes=512,
  partitions=partitions,unpartitioned_tail_bytes=max(0,sectors-last)*512))
usage = shutil.disk_usage('/home/zhichun')
roots = [Path('/home/zhichun/fair-final-v1-20261019-search-episode/native-search-episode-1792552718166807000'),
 Path('/home/zhichun/fair-final-history-v2/native-history-v2-refill-1792559847329216000')]
attempts = []
for root in roots:
 files = [path for path in root.rglob('*') if path.is_file() and not path.is_symlink()]
 logical = sum(path.stat().st_size for path in files)
 allocated = sum(path.stat().st_blocks*512 for path in files)
 data = [path for path in files if 'project' not in path.relative_to(root).parts and path.suffix in ('.json','.jsonl','.log')]
 attempts.append(dict(path=str(root),files=len(files),logical_bytes=logical,allocated_bytes=allocated,
  evidence_logical_bytes=sum(path.stat().st_size for path in data),complete_game_verified=False))
mount = subprocess.run(['findmnt','-J','-T','/home/zhichun','-o','TARGET,SOURCE,FSTYPE,OPTIONS'],
 capture_output=True,text=True,timeout=10)
if mount.returncode: raise RuntimeError('persistent mount audit failed')
print(json.dumps(dict(schema='fair-final-storage-headroom.v1',timestamp=time.time(),read_only=True,
 host='172.16.48.35',minimum_free_bytes=200*1024**3,free_bytes=usage.free,
 deficit_bytes=max(0,200*1024**3-usage.free),two_generation_bytes=None,
 formal_storage_gate_passed=False,blocks=blocks,mount=json.loads(mount.stdout),attempts=attempts)))
'''
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        _, output, errors = client.exec_command(shlex.join([
            "/home/zhichun/firstlight_CR/.venv/bin/python", "-c", probe]), timeout=30)
        encoded = output.read().decode()
        if output.channel.recv_exit_status():
            raise RuntimeError(errors.read().decode())
        report = json.loads(encoded)
        destination = ROOT / ("storage-headroom-" + str(time.time_ns()) + ".json")
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(dict(report=str(destination),free_gib=report["free_bytes"]/1024**3,
            deficit_gib=report["deficit_bytes"]/1024**3,blocks=report["blocks"],
            attempts=report["attempts"])), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
