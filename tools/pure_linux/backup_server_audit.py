"""Summarize bounded Android backup-service DEX call evidence."""

import argparse
import json
from pathlib import Path
import re
import subprocess

from platform_preference_audit import selected_methods
from preference_method_audit import HEADER, INDEX, sha256


SERVER_METHODS = (
    "com.android.server.backup.BackupManagerService.dataChanged:(ILjava/lang/String;)V",
    "com.android.server.backup.BackupManagerService.dataChanged:(Ljava/lang/String;)V",
    "com.android.server.backup.BackupManagerService.dataChangedForUser:(ILjava/lang/String;)V",
    "com.android.server.backup.BackupManagerService.getServiceForUserIfCallerHasPermission:(ILjava/lang/String;)Lcom/android/server/backup/UserBackupManagerService;",
    "com.android.server.backup.BackupManagerService.isUserReadyForBackup:(I)Z",
    "com.android.server.backup.UserBackupManagerService.dataChanged:(Ljava/lang/String;)V",
    "com.android.server.backup.UserBackupManagerService.dataChangedImpl:(Ljava/lang/String;)V",
    "com.android.server.backup.UserBackupManagerService.dataChangedImpl:(Ljava/lang/String;Ljava/util/HashSet;)V",
    "com.android.server.backup.UserBackupManagerService.dataChangedTargets:(Ljava/lang/String;)Ljava/util/HashSet;",
    "com.android.server.backup.UserBackupManagerService.writeToJournalLocked:(Ljava/lang/String;)V",
    "com.android.server.backup.DataChangedJournal.addPackage:(Ljava/lang/String;)V",
)
CALL = re.compile(r"invoke-[^ ]+ \{[^}]*\}, (L[^ ]+?;)\.([^ ]+)$")
CONTROL = ("if-", "goto", "packed-switch", "sparse-switch")


def summarize(text, methods=SERVER_METHODS):
    selected = selected_methods(text.encode("utf-8"), methods)
    result = {}
    current = None
    for line in selected.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            current = header[1]
            result[current] = {"direct_calls": [], "control_ops": [], "instruction_count": 0}
            continue
        if current is None or "|" not in line:
            continue
        instruction = INDEX.sub("", line.split("|", 1)[1].strip())
        if not re.match(r"^[0-9a-f]+: ", instruction):
            raise ValueError("malformed selected instruction")
        opcode = instruction.split(": ", 1)[1].split(" ", 1)[0]
        row = result[current]
        row["instruction_count"] += 1
        call = CALL.fullmatch(instruction.split(": ", 1)[1])
        if call:
            row["direct_calls"].append(call[1] + "." + call[2])
        elif opcode.startswith(CONTROL):
            row["control_ops"].append(opcode)
        elif opcode.startswith("invoke-"):
            raise ValueError("unsupported invoke format")
    if set(result) != set(methods) or any(row["instruction_count"] == 0 for row in result.values()):
        raise ValueError("selected server method missing or empty")
    for row in result.values():
        row["direct_calls"] = sorted(set(row["direct_calls"]))
        row["control_ops"] = sorted(row["control_ops"])
    return result


def audit(dex, expected_dex_sha256, dexdump, expected_tool_sha256, output):
    dex_bytes, tool_bytes = dex.read_bytes(), dexdump.read_bytes()
    if sha256(dex_bytes) != expected_dex_sha256 or not dex_bytes.startswith(b"dex\n"):
        raise ValueError("services DEX identity mismatch")
    if sha256(tool_bytes) != expected_tool_sha256:
        raise ValueError("dexdump identity mismatch")
    output.mkdir(exist_ok=False)
    command = [str(dexdump), "-d", str(dex)]
    process = subprocess.run(command, capture_output=True, timeout=90)
    (output / "dexdump.stdout").write_bytes(process.stdout)
    (output / "dexdump.stderr").write_bytes(process.stderr)
    (output / "command.json").write_text(json.dumps({"command": command,
        "exit_code": process.returncode}, indent=2) + "\n")
    if process.returncode:
        raise ValueError("dexdump failed; private diagnostics retained")
    report = {"schema": "nashura.backup-server-audit.v1", "status": "STATIC_FOUND",
              "dex_sha256": expected_dex_sha256, "dexdump_sha256": expected_tool_sha256,
              "dump_sha256": sha256(process.stdout), "stderr_sha256": sha256(process.stderr),
              "exit_code": process.returncode, "methods": summarize(process.stdout.decode("utf-8")),
              "scope": "allowlisted method direct-call operands and branch-opcode counts",
              "service_delivery": "RUNTIME_UNOBSERVED", "catalog_ready_pass": False}
    if dex.read_bytes() != dex_bytes or dexdump.read_bytes() != tool_bytes:
        raise ValueError("audit input changed")
    report["inputs_unchanged"] = True
    with (output / "report.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dex", type=Path, required=True)
    parser.add_argument("--dex-sha256", required=True)
    parser.add_argument("--dexdump", type=Path, required=True)
    parser.add_argument("--dexdump-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.dex, args.dex_sha256, args.dexdump, args.dexdump_sha256, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
