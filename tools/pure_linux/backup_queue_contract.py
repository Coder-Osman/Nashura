"""Bind a manually reviewed backup queue branch contract to exact DEX evidence."""

import argparse
import json
from pathlib import Path
import subprocess

from platform_preference_audit import selected_methods
from preference_method_audit import method_evidence, sha256


METHOD = "com.android.server.backup.UserBackupManagerService.dataChangedImpl:(Ljava/lang/String;Ljava/util/HashSet;)V"
REVIEWED_METHOD_SHA256 = "4dfda6c5a81f6bc233b7f994e2f98a59836c777123b3e72dcc2883702ac0f587"


def review(dump, dex):
    text = selected_methods(dump, (METHOD,))
    evidence = method_evidence(text, (METHOD,), dex)[METHOD]
    if evidence["sha256"] != REVIEWED_METHOD_SHA256:
        raise ValueError("method differs from manually reviewed queue contract")
    return {
        "method": METHOD, "method_evidence": evidence,
        "status": "STATIC_FOUND", "basis": "manual review of exact normalized method",
        "automatic_semantic_inference": False,
        "normal_return_cases": [
            {"targets": "null", "member": None, "previous_request": None,
             "queue_put": False, "journal_call": False, "schedule_call": False},
            {"targets": "nonnull", "member": False, "previous_request": None,
             "queue_put": False, "journal_call": False, "schedule_call": True},
            {"targets": "nonnull", "member": True, "previous_request": "null",
             "queue_put": True, "journal_call": True, "schedule_call": True},
            {"targets": "nonnull", "member": True, "previous_request": "nonnull",
             "queue_put": True, "journal_call": False, "schedule_call": True},
        ],
        "queue_lock": "mQueueLock covers membership, put and conditional journal call",
        "schedule": "after releasing queue monitor on nonnull-target normal paths",
        "exceptions": "monitor cleanup and rethrow; schedule not guaranteed after exception",
        "journal_callee_semantics": "UNRESOLVED",
        "job_delivery": "UNRESOLVED", "historical_branch": "UNRESOLVED",
        "runtime_parity": "NOT_RUN", "catalog_ready_pass": False,
    }


def audit(dex, expected_dex_sha256, dexdump, expected_tool_sha256, output):
    source, tool = dex.read_bytes(), dexdump.read_bytes()
    if sha256(source) != expected_dex_sha256 or not source.startswith(b"dex\n"):
        raise ValueError("services DEX identity mismatch")
    if sha256(tool) != expected_tool_sha256:
        raise ValueError("dexdump identity mismatch")
    output.mkdir(mode=0o700, exist_ok=False)
    command = [str(dexdump), "-d", str(dex)]
    result = subprocess.run(command, capture_output=True, timeout=90)
    (output / "dexdump.stdout").write_bytes(result.stdout)
    (output / "dexdump.stderr").write_bytes(result.stderr)
    (output / "command.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode}, indent=2) + "\n")
    if result.returncode:
        raise ValueError("dexdump failed; private diagnostics retained")
    report = {"schema": "nashura.backup-queue-contract.v1",
              "dex_sha256": expected_dex_sha256, "dexdump_sha256": expected_tool_sha256,
              "dump_sha256": sha256(result.stdout), "stderr_sha256": sha256(result.stderr),
              "exit_code": result.returncode, "review": review(result.stdout, source)}
    if dex.read_bytes() != source or dexdump.read_bytes() != tool:
        raise ValueError("audit input changed")
    report["inputs_unchanged"] = True
    with (output / "report.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dex", "dexdump", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("dex-sha256", "dexdump-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    audit(args.dex, args.dex_sha256, args.dexdump, args.dexdump_sha256, args.output)


if __name__ == "__main__":
    main()
