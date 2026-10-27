"""Audit a resolved initializer graph; raw instructions stay in memory only."""

import argparse
from collections import deque
import json
from pathlib import Path
import re
import subprocess

from preference_method_audit import (
    HEADER, INDEX, INITIALIZER, METHODS, METADATA, array_payload, compare,
    method_evidence, sha256,
)


BOUNDARY = re.compile(r"iput-object .*Lcom/supercell/titan/GameApp;\.p:Lc/a;$")
BRANCH = re.compile(r"(if-\S+ .*, |goto(?:/16|/32)? )([0-9a-f]+) // ([+-][0-9a-f]+)$")
ARRAY = re.compile(r"(fill-array-data v\d+, )([0-9a-f]+) // ([+-][0-9a-f]+)$")


def initializer_graph(text, dex):
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines)
              if (match := HEADER.fullmatch(line)) and match[1] == INITIALIZER]
    if len(starts) != 1:
        raise ValueError("initializer missing or ambiguous")
    start = starts[0]
    metadata = {}
    for line in lines[max(0, start - 12):start]:
        for field in METADATA:
            match = re.fullmatch(r"\s*" + re.escape(field) + r"\s*:\s*(.+)", line)
            if match:
                if field in metadata:
                    raise ValueError("duplicate metadata")
                metadata[field] = match[1]
    if set(metadata) != set(METADATA):
        raise ValueError("incomplete metadata")
    instructions, payloads = {}, {}
    complete = False
    for line in lines[start + 1:]:
        if line.strip().startswith("catches"):
            if line.strip() != "catches       : (none)":
                raise ValueError("initializer exception edges not implemented")
            complete = True
            break
        if "|" not in line:
            raise ValueError("invalid instruction")
        match = re.fullmatch(r"([0-9a-f]+): (.+)", INDEX.sub("", line.split("|", 1)[1]))
        if not match:
            raise ValueError("invalid instruction")
        address, instruction = int(match[1], 16), match[2]
        if address in instructions or address in payloads:
            raise ValueError("duplicate instruction")
        if "switch" in instruction:
            raise ValueError("switch graph not implemented")
        if instruction.startswith("array-data "):
            payloads[address] = array_payload(line, dex)
        else:
            instructions[address] = instruction
    boundaries = [address for address, instruction in instructions.items() if BOUNDARY.fullmatch(instruction)]
    if not complete or len(boundaries) != 1 or 0 not in instructions:
        raise ValueError("incomplete initializer or ambiguous boundary")
    addresses = sorted(instructions)
    next_address = dict(zip(addresses, addresses[1:]))
    boundary = boundaries[0]
    identifiers = {0: 0}
    pending = deque([0])
    nodes = []
    branch_count = 0
    used_payloads = set()
    terminals = {"initialized": 0, "return": 0, "throw": 0}
    while pending:
        address = pending.popleft()
        instruction = instructions[address]
        edges = []
        terminal = None
        if address == boundary:
            terminal = "initialized"
        elif instruction.startswith("return"):
            terminal = "return"
        elif instruction.startswith("throw "):
            terminal = "throw"
        elif instruction.startswith(("if-", "goto")):
            match = BRANCH.fullmatch(instruction)
            if not match or int(match[2], 16) != address + int(match[3], 16):
                raise ValueError("invalid branch displacement")
            target = int(match[2], 16)
            if instruction.startswith("if-"):
                if address not in next_address:
                    raise ValueError("missing fallthrough")
                edges.append(("fallthrough", next_address[address]))
            edges.append(("branch", target))
            instruction = match[1] + "RESOLVED_EDGE"
            branch_count += 1
        else:
            if address not in next_address:
                raise ValueError("missing fallthrough")
            edges.append(("fallthrough", next_address[address]))
        if instruction.startswith("fill-array-data "):
            match = ARRAY.fullmatch(instruction)
            if not match or int(match[2], 16) != address + int(match[3], 16):
                raise ValueError("invalid payload displacement")
            target = int(match[2], 16)
            if target not in payloads:
                raise ValueError("unresolved payload")
            instruction = match[1] + "PAYLOAD_SHA256:" + payloads[target]
            used_payloads.add(payloads[target])
        resolved = []
        for kind, target in edges:
            if target not in instructions:
                raise ValueError("edge is not an instruction boundary")
            if target not in identifiers:
                identifiers[target] = len(identifiers)
                pending.append(target)
            resolved.append([kind, identifiers[target]])
        if terminal:
            terminals[terminal] += 1
        nodes.append({"instruction": instruction, "edges": resolved, "terminal": terminal})
    if not terminals["initialized"]:
        raise ValueError("initialization boundary unreachable")
    graph = {"metadata": {field: metadata[field] for field in METADATA if field != "insns size"},
             "nodes": nodes}
    encoded = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": sha256(encoded), "reachable_nodes": len(nodes),
            "resolved_branches": branch_count, "terminals": terminals,
            "payload_sha256": sorted(used_payloads), "status": "STATIC_FOUND",
            "scope": "entry to p assignment plus complete reachable bypass paths; no catch handlers",
            "runtime_parity": "NOT_RUN"}


def observe(tool, path, expected):
    before = path.read_bytes()
    if sha256(before) != expected or not before.startswith(b"dex\n"):
        raise ValueError("DEX identity mismatch")
    result = subprocess.run([str(tool), "-d", str(path)], capture_output=True, timeout=90)
    if result.returncode or path.read_bytes() != before:
        raise ValueError("dump failed or input changed")
    text = result.stdout.decode("utf-8")
    helpers = method_evidence(text, METHODS, before)
    full = method_evidence(text, (INITIALIZER,), before)[INITIALIZER]
    graph = initializer_graph(text, before)
    return {"dex_sha256": expected, "dump_sha256": sha256(result.stdout),
            "stderr_sha256": sha256(result.stderr), "exit_code": result.returncode,
            "input_unchanged": True}, helpers, full, graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dexdump", type=Path, required=True)
    parser.add_argument("--dexdump-sha256", required=True)
    for side in ("left", "right"):
        parser.add_argument("--" + side, type=Path, required=True)
        parser.add_argument("--" + side + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tool_bytes = args.dexdump.read_bytes()
    if sha256(tool_bytes) != args.dexdump_sha256 or args.output.exists():
        raise ValueError("tool mismatch or output already exists")
    left_input, left_helpers, left_full, left_graph = observe(args.dexdump, args.left, args.left_sha256)
    right_input, right_helpers, right_full, right_graph = observe(args.dexdump, args.right, args.right_sha256)
    if args.dexdump.read_bytes() != tool_bytes:
        raise ValueError("tool changed")
    helper_comparison = compare(left_helpers, right_helpers)
    result = {"schema": "nashura.preference-initializer-graph.v1",
              "inputs": {"left": left_input, "right": right_input,
                         "dexdump_sha256": args.dexdump_sha256},
              "helpers": helper_comparison, "full_initializer_equal": left_full == right_full,
              "initializer_graph": {"left": left_graph, "right": right_graph,
                                    "equal": left_graph == right_graph},
              "whole_initialization_parity": "UNRESOLVED",
              "uncovered": ["runtime device and package inputs", "transitive external callees",
                            "cold-start continuation after p assignment", "persistence and backup"]}
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return 0 if helper_comparison["selected_methods_equal"] and left_graph == right_graph else 2


if __name__ == "__main__":
    raise SystemExit(main())
