import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from native_runner.training.fair_final.storage import ensure_immutable, json_bytes


SLOTS = (
    ("clean-screen", "/home/zhichun/fair-clean-student-loop-20261013", "screen", 200),
    ("clean-confirmation", "/home/zhichun/fair-clean-student-loop-20261013", "confirmation", 1000),
    ("round1-screen", "/home/zhichun/fair-continuous-20261014/round-000001", "screen", 200),
    ("round2-screen", "/home/zhichun/fair-continuous-20261014/round-000002", "screen", 200),
)
CODE_PATH = "project/native_runner/training/fair_search/evaluate_policy.py"


def requested_files():
    result = {}
    for slot, root, stage, _ in SLOTS:
        for suffix in ("manifest", "results", "done"):
            result[f"{slot}-{suffix}.json"] = f"{root}/{stage}-{suffix}.json"
        result[slot + "-evaluate_policy.py"] = root + "/" + CODE_PATH
    return result


def collect(workspace, output):
    workspace, output = Path(workspace), Path(output)
    fields = [part.strip() for part in next(line for line in
        (workspace / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    requested = requested_files()
    probe = """import hashlib,json
from pathlib import Path
paths=PATHS
print(json.dumps({name:dict(sha256=hashlib.sha256(Path(remote).read_bytes()).hexdigest(),
 bytes=Path(remote).stat().st_size) for name,remote in paths.items()}))
""".replace("PATHS", repr(requested))
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)

    def metadata():
        _, stdout, stderr = client.exec_command(shlex.join(["python3", "-c", probe]), timeout=30)
        raw, errors = stdout.read(), stderr.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors.decode(errors="replace"))
        return json.loads(raw)

    try:
        before = metadata()
        payloads = {}
        with client.open_sftp() as connection:
            for name, remote in requested.items():
                with connection.open(remote, "rb") as handle:
                    raw = handle.read()
                if before[name] != dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)):
                    raise ValueError("historical source changed during readback: " + name)
                payloads[name] = raw
        if metadata() != before:
            raise ValueError("historical source changed before readback completed")
    finally:
        client.close()
    output.mkdir(parents=True, exist_ok=True)
    for name, raw in payloads.items():
        ensure_immutable(output / name, raw)
    records = [dict(local=name, remote=remote, **before[name]) for name, remote in requested.items()]
    receipt_path = output / "receipt.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        if receipt["files"] != records:
            raise ValueError("historical readback receipt differs")
    else:
        receipt = dict(schema="FairFinalNestedSeedReadback/v1", host="172.16.48.35",
            captured_at=time.time(), files=records, read_only=True,
            remote_before_after_equal=True, historical_preregistration_proven=False,
            native_terminal_reverified=False, training_admissible=False, formal_g2_pass=False)
        ensure_immutable(receipt_path, json_bytes(receipt))
    return dict(files=len(records), bytes=sum(item["bytes"] for item in records),
        receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(collect(**vars(parser.parse_args()))))


if __name__ == "__main__":
    main()
