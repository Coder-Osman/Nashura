import hashlib
import json
from pathlib import Path
import shlex
import sys
import time

import paramiko
import torch

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
sys.path[:0] = [str(WORKSPACE / "firstlight_CR"),
    str(WORKSPACE / "_spec_work/versions/fair-final-migration-series-v1")]

from migration_series import ReadOnlyMigrationStore


def remote_json(client, code):
    _, stdout, stderr = client.exec_command(shlex.join(["python3", "-c", code]), timeout=60)
    output, errors = stdout.read().decode(), stderr.read().decode()
    if stdout.channel.recv_exit_status():
        raise RuntimeError(errors)
    return json.loads(output)


def main():
    torch.set_num_threads(1)
    source = ROOT / "native-migration-1792548905478386000/evidence/data"
    manifest = json.loads((source / "dataset.json").read_text())
    store = ReadOnlyMigrationStore(source, manifest=manifest)
    rows = 0
    for match_id in manifest["matches"]:
        episode, _ = store.load(match_id)
        rows += len(episode["rows"])
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("engineering source contains symlink")
        if path.is_file():
            data = path.read_bytes()
            files[path.relative_to(source).as_posix()] = dict(
                sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    attempt = "alternative-center-transfer-" + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir(exist_ok=False)
    remote = "/mnt/sdb/dqn-lab/cr-fair-final-centers/" + attempt
    transfer = dict(schema="FairFinalEngineeringCenterTransfer/v1", files=files,
        episodes=len(manifest["matches"]), rows=rows,
        training_admissible=False, formal_g2_pass=False)
    manifest_bytes = json.dumps(transfer, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    (output / "input-manifest.json").write_bytes(manifest_bytes)
    script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    fields = [part.strip() for part in next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.39 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.39", username=fields[3], password=fields[4], timeout=12)
    started = time.monotonic()
    try:
        prepare = f'''
import json,os,shutil,subprocess
from pathlib import Path
base=Path('/mnt/sdb/dqn-lab')
for path in (base,*base.parents):
 if path.is_symlink():raise ValueError('storage path contains symlink')
mount=json.loads(subprocess.check_output(['findmnt','--json','--target',str(base)]))
volume=mount['filesystems'][0]
if volume['target']!='/mnt/sdb' or volume['fstype']!='ext4':raise ValueError('unexpected persistent volume')
required=200*1024**3+{sum(value['bytes'] for value in files.values())!r}
if shutil.disk_usage(base).free<required:raise ValueError('center 200GiB free floor failed')
namespace=base/'cr-fair-final-centers'
if namespace.is_symlink():raise ValueError('center namespace is a symlink')
namespace.mkdir(exist_ok=True)
if (namespace/'AGENTS.md').exists():raise ValueError('center namespace instructions need review')
root=Path({remote!r})
root.mkdir(exist_ok=False)
(root/'data').mkdir()
if root.stat().st_dev!=base.stat().st_dev:raise ValueError('center volume changed')
print(json.dumps(dict(root=str(root),mount=mount,free_bytes=shutil.disk_usage(root).free,uid=os.getuid(),device=root.stat().st_dev)))
'''
        (output / "prepare-code.py").write_text(prepare)
        prepared = remote_json(client, prepare)
        (output / "prepare.json").write_text(json.dumps(prepared, indent=2))
        with client.open_sftp() as sftp:
            with sftp.open(remote + "/input-manifest.json", "wx") as handle:
                handle.write(manifest_bytes)
            directories = {"."}
            for name, expected in files.items():
                data = (source / name).read_bytes()
                if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                    raise ValueError("source changed during upload")
                parent = Path(name).parent
                for directory in (*reversed(parent.parents), parent):
                    relative = directory.as_posix()
                    if relative not in directories:
                        sftp.mkdir(remote + "/data/" + relative)
                        directories.add(relative)
                with sftp.open(remote + "/data/" + name, "wx") as handle:
                    handle.write(data)
        verify = f'''
import hashlib,json,os,shutil
from pathlib import Path
root=Path({remote!r})
raw=(root/'input-manifest.json').read_bytes()
if hashlib.sha256(raw).hexdigest()!={digest!r}:raise ValueError('transfer manifest differs')
manifest=json.loads(raw)
actual={{path.relative_to(root/'data').as_posix() for path in (root/'data').rglob('*') if path.is_file()}}
if actual!=set(manifest['files']):raise ValueError('transfer file set differs')
for name,expected in manifest['files'].items():
 path=root/'data'/name
 if path.is_symlink():raise ValueError('received symlink')
 with path.open('rb+') as handle:
  os.fsync(handle.fileno())
  checksum=hashlib.sha256()
  while True:
   chunk=handle.read(1024*1024)
   if not chunk:break
   checksum.update(chunk)
  digest=checksum.hexdigest()
 if path.stat().st_size!=expected['bytes'] or digest!=expected['sha256']:raise ValueError('received file identity differs')
for path in sorted((path for path in (root/'data').rglob('*') if path.is_dir()),key=lambda value:len(value.parts),reverse=True)+[root/'data',root]:
 descriptor=os.open(path,os.O_RDONLY)
 try:os.fsync(descriptor)
 finally:os.close(descriptor)
free=shutil.disk_usage(root).free
if free<200*1024**3:raise ValueError('center free floor failed after transfer')
print(json.dumps(dict(status='byte_transfer_verified',files=len(actual),bytes=sum(value['bytes'] for value in manifest['files'].values()),free_bytes=free,device=root.stat().st_dev)))
'''
        (output / "verify-code.py").write_text(verify)
        verified = remote_json(client, verify)
        with client.open_sftp() as sftp:
            for name, expected in files.items():
                with sftp.open(remote + "/data/" + name, "rb") as handle:
                    data = handle.read()
                if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                    raise ValueError("roundtrip file identity differs")
        report = dict(verified, remote=remote, manifest_sha256=digest,
            script_sha256=script_sha, episodes=transfer["episodes"], rows=rows,
            source_semantics_validated=True, roundtrip_all_files=True,
            seconds=time.monotonic() - started,
            two_generation_capacity_verified=False, active_center_switched=False,
            training_admissible=False, formal_g2_pass=False, gates_closed=False)
        content = json.dumps(report, sort_keys=True, indent=2).encode()
        publish = f'''
import json,os
from pathlib import Path
root=Path({remote!r})
with (root/'transfer-complete.pending').open('xb') as handle:
 handle.write({content!r})
 handle.flush()
 os.fsync(handle.fileno())
os.link(root/'transfer-complete.pending',root/'transfer-complete.json')
descriptor=os.open(root,os.O_RDONLY)
try:os.fsync(descriptor)
finally:os.close(descriptor)
print(json.dumps(dict(published=True)))
'''
        (output / "publish-code.py").write_text(publish)
        remote_json(client, publish)
        (output / "transfer-complete.json").write_bytes(content)
        print(content.decode(), flush=True)
    except Exception as error:
        (output / "failure.json").write_text(json.dumps(dict(error=repr(error), remote=remote,
            training_admissible=False, gates_closed=False), indent=2))
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
