"""Bind a manual journal/scheduler review to exact, privately retained DEX evidence."""

import argparse
import json
import os
from pathlib import Path

from platform_preference_audit import selected_methods
from preference_method_audit import method_evidence, sha256


JOURNAL = "com.android.server.backup.DataChangedJournal."
SERVICE = "com.android.server.backup.UserBackupManagerService."
JOB = "com.android.server.backup.KeyValueBackupJob."
REVIEWED = {
    JOURNAL + "<init>:(Ljava/io/File;)V":
        "74c3fbf35636a0206a1ad7757d312d8fa1c1b73e9ad58d6e84525968789d5c7a",
    JOURNAL + "newJournal:(Ljava/io/File;)Lcom/android/server/backup/DataChangedJournal;":
        "7e09c126ce8ba353cacbdea5d343d335c25223dad59085c5f7c722ac429e441b",
    JOURNAL + "addPackage:(Ljava/lang/String;)V":
        "a239350ca1a2e5fbd4f36c443f6fb5c5fb6caff65124dc9cb7dd6eda189b79eb",
    SERVICE + "writeToJournalLocked:(Ljava/lang/String;)V":
        "c2623709294f9e408811711737f53a667890360debd75b76b4db7ba7878e62f2",
    JOB + "schedule:(ILandroid/content/Context;JLcom/android/server/backup/BackupManagerConstants;)V":
        "58ee0c6dfa7cd43ea3e9fd2d1df9a2e5aa56cb18d9d5841f5e626f7186339831",
    JOB + "schedule:(ILandroid/content/Context;Lcom/android/server/backup/BackupManagerConstants;)V":
        "df4ccc8cc7d77efde81584e60974963f814316398e94e8460fbca16d01dad4d4",
}


def review(dump, dex):
    methods = tuple(REVIEWED)
    evidence = method_evidence(selected_methods(dump, methods), methods, dex)
    if any(evidence[name]["sha256"] != digest for name, digest in REVIEWED.items()):
        raise ValueError("method differs from manually reviewed journal contract")
    return {
        "status": "STATIC_FOUND", "methods": evidence,
        "basis": "manual control-flow review of exact normalized methods and catch tables",
        "automatic_semantic_inference": False,
        "journal": {
            "constructor": "Object constructor, then retain supplied File as mFile",
            "new": "File.createTempFile with journal prefix, null suffix and supplied directory",
            "append": "RandomAccessFile rws; length; seek to length; writeUTF(package); close",
            "append_failure": "close on protected failure; suppress secondary close exception; rethrow",
            "service_write": "create and retain journal if null, then addPackage",
            "service_ioexception": "log error, clear mJournal, return normally",
            "other_exception": "not caught by the service IOException handler",
            "normal_return_proves_durability": False,
        },
        "scheduler": {
            "wrapper": "three-argument overload delegates with delay zero",
            "deduplication": "class monitor; existing per-user scheduled flag returns before job submission",
            "constants": ["interval", "fuzz", "network type", "charging"],
            "delay": "positive explicit delay, otherwise interval plus Random.nextInt((int)fuzz)",
            "job": "minimum latency, network, charging, deadline and transient userId extras",
            "deadline_ms": 86400000,
            "submit": "Context jobscheduler service; JobScheduler.schedule(JobInfo)",
            "submission_result": "integer return ignored",
            "after_normal_submission_return": "set per-user scheduled flag and wall-clock next time",
            "exceptions": "monitor cleanup and rethrow; later bookkeeping not guaranteed",
            "normal_return_proves_acceptance_or_delivery": False,
        },
        "unresolved": ["actual historical queue branch", "journal write runtime coverage",
                       "RandomAccessFile and file creation transitive semantics",
                       "fsync and crash durability", "actual scheduler acceptance and delivery",
                       "Linux Backup binding", "full configuration exception domain"],
        "runtime_parity": "NOT_RUN", "catalog_ready_pass": False,
    }


def audit(dump_path, expected_dump_sha, dex_path, expected_dex_sha, output_path):
    dump, dex = dump_path.read_bytes(), dex_path.read_bytes()
    if sha256(dump) != expected_dump_sha:
        raise ValueError("DEX dump identity mismatch")
    if sha256(dex) != expected_dex_sha or not dex.startswith(b"dex\n"):
        raise ValueError("DEX identity mismatch")
    result = {"schema": "nashura.backup-journal-contract.v1",
              "dump_sha256": expected_dump_sha, "dex_sha256": expected_dex_sha,
              "review": review(dump, dex)}
    if dump_path.read_bytes() != dump or dex_path.read_bytes() != dex:
        raise ValueError("review input changed")
    result["inputs_unchanged"] = True
    with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dump", "dex", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("dump-sha256", "dex-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    audit(args.dump, args.dump_sha256, args.dex, args.dex_sha256, args.output)


if __name__ == "__main__":
    main()
