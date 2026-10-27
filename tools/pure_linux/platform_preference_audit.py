"""Hash bounded platform DEX methods; keep complete dumps in a private directory."""

import argparse
import json
from pathlib import Path
import re
import subprocess

from preference_method_audit import HEADER, method_evidence, sha256


METHODS = (
    "android.app.backup.BackupManager.checkServiceBinder:()V",
    "android.app.backup.BackupManager.dataChanged:(Ljava/lang/String;)V",
    "android.app.backup.BackupManager.dataChanged:()V",
    "android.app.SharedPreferencesImpl$EditorImpl.commitToMemory:()Landroid/app/SharedPreferencesImpl$MemoryCommitResult;",
    "android.app.SharedPreferencesImpl$EditorImpl.notifyListeners:(Landroid/app/SharedPreferencesImpl$MemoryCommitResult;)V",
    "android.app.SharedPreferencesImpl$EditorImpl.commit:()Z",
    "android.app.SharedPreferencesImpl.createFileOutputStream:(Ljava/io/File;)Ljava/io/FileOutputStream;",
    "android.app.SharedPreferencesImpl.enqueueDiskWrite:(Landroid/app/SharedPreferencesImpl$MemoryCommitResult;Ljava/lang/Runnable;)V",
    "android.app.SharedPreferencesImpl.loadFromDisk:()V",
    "android.app.SharedPreferencesImpl.writeToFile:(Landroid/app/SharedPreferencesImpl$MemoryCommitResult;Z)V",
)


def selected_methods(data, methods=METHODS):
    lines = data.splitlines()
    selected = []
    for index, line in enumerate(lines):
        try:
            header = HEADER.fullmatch(line.decode("ascii"))
        except UnicodeDecodeError:
            continue
        if not header or header[1] not in methods:
            continue
        end = next((position for position in range(index + 1, min(len(lines), index + 4098))
                    if lines[position].strip() == b"positions     :"), None)
        if end is None:
            raise ValueError("unterminated selected platform method")
        start = next((position for position in range(index - 1, max(-1, index - 13), -1)
                      if lines[position].lstrip().startswith(b"#")), None)
        if start is None:
            raise ValueError("selected method metadata boundary missing")
        selected.append(b"\n".join(lines[start:end + 1]).decode("utf-8"))
    text = "\n".join(selected)
    if "\ufffd" in text:
        raise ValueError("replacement character in selected method")
    return text


def summarize(data, dex, methods=METHODS):
    text = selected_methods(data, methods)
    evidence = method_evidence(text, methods, dex)
    calls = {name: set() for name in methods}
    current = None
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            current = header[1] if header[1] in calls else None
        if current and (match := re.search(r"invoke-[^ ]+ .*?, (L[^ ]+?;)\.([^ ]+)", line)):
            calls[current].add(match[1] + "." + match[2])
    return {name: {**row, "direct_calls": sorted(calls[name]), "status": "STATIC_FOUND"}
            for name, row in evidence.items()}


def audit(dex_path, dex_sha, tool_path, tool_sha, output):
    dex, tool = dex_path.read_bytes(), tool_path.read_bytes()
    if sha256(dex) != dex_sha or not dex.startswith(b"dex\n") or sha256(tool) != tool_sha:
        raise ValueError("platform DEX or dexdump identity mismatch")
    output.mkdir(exist_ok=False)
    command = [str(tool_path), "-d", str(dex_path)]
    result = subprocess.run(command, capture_output=True, timeout=90)
    for name, data in (("stdout", result.stdout), ("stderr", result.stderr)):
        (output / ("dexdump." + name)).write_bytes(data)
    (output / "command.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2) + "\n")
    if result.returncode:
        raise ValueError("dexdump failed; diagnostics retained privately")
    if dex_path.read_bytes() != dex or tool_path.read_bytes() != tool:
        raise ValueError("platform input changed during audit")
    methods = summarize(result.stdout, dex)
    report = {"schema": "nashura.platform-preference-audit.v1", "status": "STATIC_FOUND",
              "dex_sha256": dex_sha, "dexdump_sha256": tool_sha, "inputs_unchanged": True,
              "dump_stdout_sha256": sha256(result.stdout), "dump_stderr_sha256": sha256(result.stderr),
              "exit_code": result.returncode, "methods": methods,
              "scope": "selected method bodies, register metadata, catch tables and direct invoke operands",
              "runtime_parity": "NOT_RUN", "backup_service_binding": "UNRESOLVED",
              "transitive_callees": "UNRESOLVED", "catalog_ready_pass": False}
    with (output / "report.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dex", "dexdump", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("dex-sha256", "dexdump-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    audit(args.dex, args.dex_sha256, args.dexdump, args.dexdump_sha256, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
