"""Audit bounded backup transactions without publishing private DEX instructions."""

import argparse
import json
from pathlib import Path
import re
import subprocess

from platform_preference_audit import selected_methods, summarize
from preference_method_audit import HEADER, INDEX, sha256


BACKUP_PROXY = "android.app.backup.IBackupManager$Stub$Proxy.dataChanged:(Ljava/lang/String;)V"
SERVICE_PROXY = "android.os.IServiceManager$Stub$Proxy.checkService:(Ljava/lang/String;)Landroid/os/IBinder;"
METHODS = (
    "android.app.backup.BackupManager.checkServiceBinder:()V",
    "android.app.backup.BackupManager.dataChanged:()V",
    "android.app.backup.BackupManager.dataChanged:(Ljava/lang/String;)V",
    BACKUP_PROXY, SERVICE_PROXY,
    "android.os.ServiceManager.getService:(Ljava/lang/String;)Landroid/os/IBinder;",
    "android.os.ServiceManagerProxy.getService:(Ljava/lang/String;)Landroid/os/IBinder;",
)
TRANSACT = "Landroid/os/IBinder;.transact:(ILandroid/os/Parcel;Landroid/os/Parcel;I)Z"
OBTAIN = "Landroid/os/Parcel;.obtain:()Landroid/os/Parcel;"
INVOKE = re.compile(r"invoke-[^ ]+ \{([^}]*?)\}, (L[^ ]+?;)\.([^ ]+)$")


def body(text):
    instructions, catches = [], []
    active = False
    pending = None
    for line in text.splitlines():
        if HEADER.fullmatch(line):
            active = True
        elif active and "|" in line:
            match = re.fullmatch(r"([0-9a-f]+): (.+)", INDEX.sub("", line.split("|", 1)[1]))
            if not match:
                raise ValueError("invalid instruction")
            instructions.append((int(match[1], 16), match[2]))
        elif match := re.fullmatch(r"\s*0x([0-9a-f]+) - 0x([0-9a-f]+)", line):
            pending = tuple(int(value, 16) for value in match.groups())
        elif match := re.fullmatch(r"\s*(L[^ ]+;|<any>) -> 0x([0-9a-f]+)", line):
            if pending is None:
                raise ValueError("catch without range")
            catches.append((*pending, match[1], int(match[2], 16)))
    locations = {location for location, _ in instructions}
    if len(locations) != len(instructions) or instructions != sorted(instructions):
        raise ValueError("duplicate or unordered instruction")
    for start, end, _, handler in catches:
        if start >= end or start not in locations or end not in locations or handler not in locations:
            raise ValueError("invalid catch boundary")
    return instructions, catches


def invocation(instruction):
    match = INVOKE.fullmatch(instruction)
    if not match:
        raise ValueError("unsupported invocation")
    registers = match[1].split(", ") if match[1] else []
    if any(not re.fullmatch(r"v\d+", register) for register in registers):
        raise ValueError("unsupported invocation registers")
    return registers, match[2] + "." + match[3]


def transaction(instructions):
    registers, pending, parcels = {}, None, 0
    calls = [instruction for _, instruction in instructions if TRANSACT in instruction]
    if len(calls) != 1:
        raise ValueError("expected exactly one transact")
    for _, instruction in instructions:
        if instruction.startswith("invoke-"):
            arguments, target = invocation(instruction)
            pending = None
            if target == TRANSACT:
                if len(arguments) != 5:
                    raise ValueError("transact argument count")
                remote, code, request, reply, flags = [registers.get(name) for name in arguments]
                if code != ("integer", 2) or flags != ("integer", 0):
                    raise ValueError("unproven transaction code or flags")
                if remote != ("remote",) or request != ("parcel", 1) or reply != ("parcel", 2):
                    raise ValueError("unproven remote or Parcel provenance")
                return {"code": 2, "flags": 0, "oneway": False,
                        "request_reply_distinct": True, "remote_source": "mRemote field",
                        "argument_provenance": "bounded straight-line register definitions",
                        "result_and_fallback_execution": "UNRESOLVED"}
            if target == OBTAIN and not arguments:
                parcels += 1
                pending = ("parcel", parcels)
            elif target not in {"Landroid/os/Parcel;.writeInterfaceToken:(Ljava/lang/String;)V",
                                "Landroid/os/Parcel;.writeString:(Ljava/lang/String;)V"}:
                raise ValueError("unreviewed call before transact")
        elif match := re.fullmatch(r"move-result-object (v\d+)", instruction):
            if pending is None:
                raise ValueError("unproven move-result")
            registers[match[1]], pending = pending, None
        elif match := re.fullmatch(r"const(?:/4|/16)? (v\d+), #int (-?\d+) // #[0-9a-f]+", instruction):
            registers[match[1]] = ("integer", int(match[2]))
            pending = None
        elif match := re.fullmatch(r"const-string(?:/jumbo)? (v\d+), .*", instruction):
            registers[match[1]] = ("string",)
            pending = None
        elif match := re.fullmatch(r"iget-object (v\d+), v\d+, L[^ ]+;\.mRemote:Landroid/os/IBinder;", instruction):
            registers[match[1]] = ("remote",)
            pending = None
        elif match := re.fullmatch(r"move(?:-object)?(?:/from16|/16)? (v\d+), (v\d+)", instruction):
            registers[match[1]] = registers.get(match[2])
            pending = None
        else:
            raise ValueError("unsupported pre-transact control flow or definition")
    raise ValueError("transact not reached")


def analyze(data, dex, methods):
    result = summarize(data, dex, methods)
    for name in methods:
        instructions, catches = body(selected_methods(data, (name,)))
        calls = []
        for location, instruction in instructions:
            if instruction.startswith("invoke-"):
                _, target = invocation(instruction)
                calls.append({"target": target, "caught_by": [kind for start, end, kind, _ in catches
                                                                 if start <= location < end]})
        result[name]["call_exception_coverage"] = calls
        result[name]["catch_types"] = sorted({kind for _, _, kind, _ in catches})
        if name in {BACKUP_PROXY, SERVICE_PROXY}:
            result[name]["transaction"] = transaction(instructions)
    return result


def audit(inputs, tool, tool_sha256, output):
    tool_bytes = tool.read_bytes()
    if sha256(tool_bytes) != tool_sha256:
        raise ValueError("dexdump identity mismatch")
    sources = [(path, digest, path.read_bytes()) for path, digest in inputs]
    if any(sha256(raw) != digest or not raw.startswith(b"dex\n") for _, digest, raw in sources):
        raise ValueError("DEX identity mismatch")
    output.mkdir(exist_ok=False)
    records, methods = [], {}
    for index, (path, digest, raw) in enumerate(sources):
        command = [str(tool), "-d", str(path)]
        process = subprocess.run(command, capture_output=True, timeout=90)
        prefix = output / f"dex-{index}"
        prefix.with_suffix(".stdout").write_bytes(process.stdout)
        prefix.with_suffix(".stderr").write_bytes(process.stderr)
        prefix.with_suffix(".command.json").write_text(json.dumps({"command": command,
            "exit_code": process.returncode}, indent=2) + "\n")
        if process.returncode:
            raise ValueError("dexdump failed; private diagnostics retained")
        available = []
        for line in process.stdout.splitlines():
            if b"|[" in line:
                try:
                    match = HEADER.fullmatch(line.decode("ascii"))
                except UnicodeDecodeError:
                    continue
                if match and match[1] in METHODS:
                    available.append(match[1])
        if len(set(available)) != len(available) or set(available) & set(methods):
            raise ValueError("duplicate selected method")
        methods.update(analyze(process.stdout, raw, tuple(available)))
        if path.read_bytes() != raw:
            raise ValueError("DEX changed during audit")
        records.append({"dex_sha256": digest, "stdout_sha256": sha256(process.stdout),
                        "stderr_sha256": sha256(process.stderr), "exit_code": process.returncode})
    if set(methods) != set(METHODS):
        raise ValueError("selected method missing")
    if tool.read_bytes() != tool_bytes or any(path.read_bytes() != raw for path, _, raw in sources):
        raise ValueError("input changed during audit")
    report = {"schema": "nashura.backup-method-audit.v1", "status": "STATIC_FOUND",
              "inputs": records, "dexdump_sha256": tool_sha256, "inputs_unchanged": True,
              "methods": methods, "runtime_parity": "NOT_RUN",
              "backup_service_delivery": "UNRESOLVED", "catalog_ready_pass": False}
    with (output / "report.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs=2, action="append", required=True, metavar=("DEX", "SHA256"))
    parser.add_argument("--dexdump", type=Path, required=True)
    parser.add_argument("--dexdump-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit([(Path(path), digest) for path, digest in args.input], args.dexdump, args.dexdump_sha256, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
