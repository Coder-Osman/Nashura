import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evidence/fair-final-v1-20261019"))

import paramiko

ARCHIVE_LOGS = ("worker.log", "prepare.log", "resume.log", "continue.log")


def publish_download_receipt(attempt, *, remote, files):
    from native_runner.training.fair_final.storage import durable_write, json_bytes
    path = Path(attempt) / "download-receipt.json"
    identity = dict(remote=remote, files=files, training_admissible=False)
    if path.exists():
        previous = json.loads(path.read_bytes())
        if (set(previous) != {"timestamp", *identity}
                or previous["training_admissible"] is not False
                or any(previous[key] != value for key, value in identity.items())):
            raise ValueError("pinned download receipt differs; preserve the existing archive")
        return previous
    receipt = dict(timestamp=time.time(), **identity)
    durable_write(path, json_bytes(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--source-root", type=Path,
        default=Path(__file__).resolve().parents[3] / "firstlight_CR")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from run_native_probe import PYTHON, WORKSPACE
    from verify_search_episode import atomic_json, read, sha
    from native_runner.training.fair_final.storage import durable_write
    from verify_continuation import validate_continuation
    launch = read(args.attempt / "launch.json")
    fields = [part.strip() for part in next(line for line in
        (WORKSPACE / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    probe = f'''
import hashlib,json
from pathlib import Path
root=Path({launch['remote']!r})
completion=root/'completion.json'
if not completion.exists():
 print(json.dumps(dict(status='running',complete=False,training_admissible=False)),flush=True)
else:
 names=set({list(launch['payload_sha256'])!r})
 names.update(str(path.relative_to(root)) for path in (root/'episode').rglob('*') if path.is_file())
 names.update(name for name in {('completion.json', 'phase-results.json', *ARCHIVE_LOGS)!r} if (root/name).exists())
 for port in {launch['settings']['ports']!r}:
  names.update(name for name in (f'engine-{{port}}.log',f'engine-{{port}}/process.json') if (root/name).exists())
 files={{name:dict(sha256=hashlib.sha256((root/name).read_bytes()).hexdigest(),bytes=(root/name).stat().st_size) for name in sorted(names)}}
 print(json.dumps(dict(status='terminated',completion=json.loads(completion.read_text()),files=files)),flush=True)
'''
    try:
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", probe]), timeout=30)
        output, errors = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors)
        inventory = json.loads(output)
        if inventory["status"] == "running":
            print(json.dumps(inventory), flush=True)
            return
        destination = args.attempt / "evidence"
        destination.mkdir(exist_ok=True)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(30)
            for relative, expected in inventory["files"].items():
                path = destination / relative
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    raise ValueError("search episode evidence path escapes attempt")
                if path.exists():
                    if path.stat().st_size != expected["bytes"] or sha(path) != expected["sha256"]:
                        raise ValueError("previously downloaded search evidence differs: " + relative)
                    continue
                with sftp.open(launch["remote"] + "/" + relative, "rb") as handle:
                    data = handle.read()
                if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                    raise ValueError("search episode download checksum differs: " + relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                durable_write(path, data)
        publish_download_receipt(args.attempt, remote=launch["remote"], files=inventory["files"])
        if inventory["completion"].get("checkpointed") is True and inventory["completion"]["complete"] is False:
            report = dict(status="checkpointed_archive_only", training_admissible=False, gates_closed=False,
                native_terminal_verified=False, completion=inventory["completion"])
            atomic_json(args.attempt / "segment-archive.json", report)
            print(json.dumps({key: value for key, value in report.items() if key != "completion"}), flush=True)
            return
        try:
            report = validate_continuation(args.attempt, source=args.source_root)
        except Exception as error:
            atomic_json(args.attempt / "verification-failure.json", dict(status="failed", error=repr(error),
                training_admissible=False))
            raise
        atomic_json(args.attempt / "verification.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()

