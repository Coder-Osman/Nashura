"""Run a bounded Linux native-queue interrupted-publication experiment."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess

from native_backup_consume import consume
from native_backup_recover import recover


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--linker", type=Path, required=True)
    parser.add_argument("--libs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        raise ValueError("ARM64 Linux required")
    args.output.mkdir(mode=0o700, exist_ok=False)
    spool = args.output / "spool"
    spool.mkdir(mode=0o700)
    (spool / ".preference.lock").touch(mode=0o600)
    for slot in range(2):
        file = spool / f"store-{slot}.xml"
        file.write_bytes(f'<map><string name="slot">{slot}</string></map>'.encode())
        file.chmod(0o660)
    report = {"schema": "nashura.native-backup-crash-experiment.v1", "machine": platform.machine(),
              "kernel": platform.release(), "probe_sha256": sha(args.probe),
              "linker_sha256": sha(args.linker), "libc_sha256": sha(args.libs / "libc.so"), "status": "FAILED",
              "android_backup_parity": "NOT_VERIFIED", "catalog_ready_pass": False}
    environment = {name: value for name, value in os.environ.items() if not name.startswith("LD_")}
    environment["LD_LIBRARY_PATH"] = str(args.libs)
    try:
        crashed = subprocess.run([str(args.linker), str(args.probe), str(spool), "crash"],
                                 env=environment, capture_output=True, timeout=10)
        report["crash_exit"] = crashed.returncode
        (args.output / "crash.stdout").write_bytes(crashed.stdout)
        (args.output / "crash.stderr").write_bytes(crashed.stderr)
        if crashed.returncode != 71:
            raise ValueError("controlled interruption did not reach link boundary")
        pending = spool / ".backup-pending"
        staged = spool / ".backup-request.tmp"
        before = pending.stat()
        report["same_inode_before"] = os.path.samestat(before, staged.stat())
        report["link_count_before"] = before.st_nlink
        report["pending_sha256_before"] = sha(pending)
        try:
            consume(spool)
        except ValueError as error:
            report["consume_before"] = str(error)
        else:
            raise ValueError("consumer accepted interrupted publication")
        if not report["same_inode_before"] or before.st_nlink != 2:
            raise ValueError("unexpected interrupted publication state")
        result = recover(spool)
        report["recovery"] = result["status"]
        report["pending_sha256_after"] = sha(pending)
        report["link_count_after"] = pending.stat().st_nlink
        if report["pending_sha256_after"] != report["pending_sha256_before"] or staged.exists():
            raise ValueError("recovery changed request or retained staging")
        resumed = subprocess.run([str(args.linker), str(args.probe), str(spool), "request"],
                                 env=environment, capture_output=True, timeout=10)
        (args.output / "resume.stdout").write_bytes(resumed.stdout)
        (args.output / "resume.stderr").write_bytes(resumed.stderr)
        report["resume_exit"] = resumed.returncode
        if resumed.returncode != 0 or resumed.stdout != b"request_result=0 created=0 close_result=0\n":
            raise ValueError("recovered request was not coalesced")
        receipt = consume(spool)
        if receipt["request_id"] != result["request_id"] or pending.exists():
            raise ValueError("consumer receipt or acknowledgement differs")
        report["consumer_scope"] = receipt["scope"]
        report["status"] = "PASS_LIMITED"
    finally:
        with (args.output / "result.json").open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(report["status"])


if __name__ == "__main__":
    main()
