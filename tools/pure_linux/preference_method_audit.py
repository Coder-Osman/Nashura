"""Compare bounded DEX method evidence without publishing instructions or constants."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess


METHODS = (
    "c.a.<init>:(Lcom/supercell/titan/GameApp;Ljava/lang/String;Ljava/lang/String;)V",
    "c.a.h:(Ljava/lang/String;Ljavax/crypto/Cipher;)Ljava/lang/String;",
    "c.a.j:(Ljava/lang/String;)Ljava/lang/String;",
    "c.a.n:(Ljava/lang/String;Ljava/lang/String;)V",
    "com.supercell.titan.ApplicationUtilBase.getKeyValue:(Ljava/lang/String;)Ljava/lang/String;",
    "com.supercell.titan.ApplicationUtilBase.storeKeyValue:(Ljava/lang/String;Ljava/lang/String;)V",
    "com.supercell.titan.ApplicationUtilBase.removeKeyValue:(Ljava/lang/String;)V",
    "com.supercell.titan.GameApp.requestBackup:()V",
)
HEADER = re.compile(r"^[0-9a-f]+:\s*\|\[[0-9a-f]+\] (\S+)$")
INDEX = re.compile(r" // (?:method|field|string|type)@[0-9a-f]+$")
METADATA = ("access", "registers", "ins", "outs", "insns size")
INITIALIZER = "com.supercell.titan.GameApp.onCreate:(Landroid/os/Bundle;)V"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def array_payload(line, dex):
    match = re.fullmatch(r"([0-9a-f]+):[^|]*\|[0-9a-f]+: array-data \((\d+) units\)", line)
    if not match or dex is None:
        raise ValueError("array payload requires matching DEX bytes")
    offset, units = int(match[1], 16), int(match[2])
    payload = dex[offset:offset + units * 2]
    if len(payload) != units * 2 or len(payload) < 8:
        raise ValueError("truncated array payload")
    identifier, width, count = struct.unpack_from("<HHI", payload)
    if identifier != 0x0300 or width not in {1, 2, 4, 8} or (8 + width * count + 1) // 2 != units:
        raise ValueError("invalid array payload")
    return sha256(payload)


def method_evidence(text, methods=METHODS, dex=None):
    lines = text.splitlines()
    found = {}
    for index, line in enumerate(lines):
        header = HEADER.fullmatch(line)
        if not header or header[1] not in methods:
            continue
        name = header[1]
        if name in found:
            raise ValueError("duplicate selected method")
        metadata = {}
        for preceding in reversed(lines[max(0, index - 12):index]):
            if preceding.lstrip().startswith("#"):
                break
            for field in METADATA:
                match = re.fullmatch(r"\s*" + re.escape(field) + r"\s*:\s*(.+)", preceding)
                if match:
                    if field in metadata:
                        raise ValueError("duplicate method metadata")
                    metadata[field] = match[1]
        if set(metadata) != set(METADATA):
            raise ValueError("incomplete method metadata")
        body = []
        catching = False
        complete = False
        instructions = 0
        for following in lines[index + 1:index + 4097]:
            if following.strip() == "positions     :":
                complete = catching
                break
            if HEADER.fullmatch(following) or following.lstrip().startswith("#"):
                raise ValueError("unterminated method evidence")
            if "|" in following and not catching:
                instruction = following.split("|", 1)[1]
                if not re.match(r"^[0-9a-f]+: ", instruction):
                    raise ValueError("invalid instruction line")
                if re.search(r"(?:packed|sparse)-switch-data", instruction):
                    raise ValueError("switch payload coverage not implemented")
                body.append(INDEX.sub("", instruction))
                if "array-data (" in instruction:
                    body.append("payload-sha256:" + array_payload(following, dex))
                instructions += 1
            elif following.strip().startswith("catches"):
                catching = True
                body.append(following.strip())
            elif catching and following.strip():
                body.append(following.strip())
            else:
                raise ValueError("unrecognized method evidence")
        if not complete or not instructions:
            raise ValueError("incomplete method body or catch table")
        normalized = json.dumps({"method": name, "metadata": metadata, "body": body},
                                sort_keys=True, separators=(",", ":")).encode()
        found[name] = {"sha256": sha256(normalized), "instructions": instructions,
                       "catch_table_present": catching}
    if set(found) != set(methods):
        raise ValueError("selected method missing")
    return found


def initialization_prefix(text, dex):
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines)
              if (match := HEADER.fullmatch(line)) and match[1] == INITIALIZER]
    if len(starts) != 1:
        raise ValueError("initializer missing or ambiguous")
    instructions = []
    payloads = {}
    complete = False
    for line in lines[starts[0] + 1:]:
        if line.strip().startswith("catches"):
            complete = True
            break
        if "|" not in line:
            raise ValueError("invalid initializer body")
        instruction = INDEX.sub("", line.split("|", 1)[1])
        if "array-data (" in instruction:
            payloads[int(instruction.split(":", 1)[0], 16)] = array_payload(line, dex)
        instructions.append(instruction)
    if not complete:
        raise ValueError("incomplete initializer")
    prefix = []
    for instruction in instructions:
        if "fill-array-data " in instruction:
            match = re.fullmatch(r"([0-9a-f]+: fill-array-data v\d+, )([0-9a-f]+) // [+-][0-9a-f]+", instruction)
            if not match or int(match[2], 16) not in payloads:
                raise ValueError("unresolved initializer array")
            instruction = match[1] + "payload-sha256:" + payloads[int(match[2], 16)]
        prefix.append(instruction)
        if re.search(r"iput-object .*Lcom/supercell/titan/GameApp;\.p:Lc/a;$", instruction):
            encoded = json.dumps(prefix, separators=(",", ":")).encode()
            return {"sha256": sha256(encoded), "instructions": len(prefix),
                    "payload_sha256": sorted(payloads.values()),
                    "scope": "linear initializer prefix through p assignment, including array bytes",
                    "runtime_parity": "NOT_RUN"}
    raise ValueError("initializer p assignment missing")


def compare(left, right):
    if set(left) != set(right):
        raise ValueError("method sets differ")
    rows = [{"method": name, "left": left[name], "right": right[name],
             "equal": left[name] == right[name]} for name in left]
    return {"status": "STATIC_FOUND", "methods": rows,
            "selected_methods_equal": all(row["equal"] for row in rows),
            "scope": "instructions, literal operands, register metadata and catch tables",
            "whole_dex_identity_equal": False,
            "runtime_parity": "NOT_RUN",
            "uncovered": ["referenced fields and initialization data", "transitive callees",
                          "Android implementation", "persistence and backup side effects"]}


def run_dump(tool, path, expected, methods):
    before = path.read_bytes()
    if sha256(before) != expected or not before.startswith(b"dex\n"):
        raise ValueError("DEX identity mismatch")
    result = subprocess.run([str(tool), "-d", str(path)], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError("dexdump failed; raw diagnostics remain private")
    if path.read_bytes() != before:
        raise ValueError("DEX changed during observation")
    text = result.stdout.decode("utf-8")
    return method_evidence(text, methods, before), {
        "dex_sha256": expected, "dump_sha256": sha256(result.stdout),
        "stderr_sha256": sha256(result.stderr), "exit_code": result.returncode,
        "input_unchanged": True}, initialization_prefix(text, before) if INITIALIZER in methods else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dexdump", type=Path, required=True)
    parser.add_argument("--dexdump-sha256", required=True)
    for side in ("left", "right"):
        parser.add_argument("--" + side, type=Path, required=True)
        parser.add_argument("--" + side + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-initializer", action="store_true")
    args = parser.parse_args()
    tool_bytes = args.dexdump.read_bytes()
    if sha256(tool_bytes) != args.dexdump_sha256:
        raise ValueError("dexdump identity mismatch")
    if args.output.exists():
        raise ValueError("output already exists")
    methods = METHODS + ((INITIALIZER,)
                         if args.include_initializer else ())
    left, left_input, left_prefix = run_dump(args.dexdump, args.left, args.left_sha256, methods)
    right, right_input, right_prefix = run_dump(args.dexdump, args.right, args.right_sha256, methods)
    if args.dexdump.read_bytes() != tool_bytes:
        raise ValueError("dexdump changed during observation")
    result = compare(left, right)
    result.update(schema="nashura.preference-method-audit.v2", inputs={
        "left": left_input, "right": right_input, "dexdump_sha256": args.dexdump_sha256})
    result["whole_dex_identity_equal"] = args.left_sha256 == args.right_sha256
    if args.include_initializer:
        result["initialization_prefix"] = {"left": left_prefix, "right": right_prefix,
                                           "equal": left_prefix == right_prefix}
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return 0 if result["selected_methods_equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
