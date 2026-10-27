"""Summarize actual Android linker constructor messages; static slots are not coverage."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


CALL = re.compile(r"\[ (Calling|Done calling) c-tor function @ (0x[0-9a-f]+) for '([^']+)' \]")
ARRAY = re.compile(r"\[ Calling DT_INIT_ARRAY \(size (\d+)\) @ (0x[0-9a-f]+) for '([^']+)' \]")
IFUNC = re.compile(r"Called ifunc_resolver@(0x[0-9a-f]+)\. The result is (0x[0-9a-f]+)")


def summarize(stream):
    checksum = hashlib.sha256()
    modules = {}
    pending = Counter()
    unexpected_returns = 0
    ifunc_calls = 0
    ifunc_resolvers = Counter()
    lines = 0
    warnings = 0
    for raw in stream:
        checksum.update(raw)
        lines += 1
        line = raw.decode("utf-8", errors="replace")
        warnings += int("Warning:" in line or "WARNING:" in line)
        match = ARRAY.search(line)
        if match:
            count, address, name = match.groups()
            row = modules.setdefault(name, {"array_sizes": [], "entered": 0, "returned": 0})
            row["array_sizes"].append(int(count))
        match = CALL.search(line)
        if match:
            action, address, name = match.groups()
            row = modules.setdefault(name, {"array_sizes": [], "entered": 0, "returned": 0})
            key = (name, address)
            if action == "Calling":
                row["entered"] += 1
                pending[key] += 1
            else:
                row["returned"] += 1
                if pending[key]:
                    pending[key] -= 1
                else:
                    unexpected_returns += 1
        match = IFUNC.search(line)
        if match:
            ifunc_calls += 1
            ifunc_resolvers[match.group(1)] += 1
    output = []
    for name, row in sorted(modules.items()):
        output.append({"module": Path(name).name, **row,
                       "unreturned": sum(count for (module, address), count in pending.items() if module == name)})
    return {"log_sha256": checksum.hexdigest(), "lines": lines, "modules": output,
            "unexpected_returns": unexpected_returns, "warnings": warnings,
            "ifunc_calls": ifunc_calls, "unique_ifunc_resolvers": len(ifunc_resolvers),
            "coverage_limit": "Linker call/return messages only; no proof of internal platform API coverage."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    with arguments.log.open("rb") as stream:
        result = summarize(stream)
    with arguments.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
