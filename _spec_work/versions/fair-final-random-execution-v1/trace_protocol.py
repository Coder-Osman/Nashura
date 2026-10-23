from collections import Counter
import hashlib
from pathlib import Path
import re


TARGETS = {
    0x121B118: "mt_initialize", 0x121B17C: "mt_clear", 0x121B188: "mt_generate",
    0x121B284: "xorshift_initialize", 0x121B2E4: "xorshift_seed",
    0x121B350: "xorshift_generate", 0x121B394: "xorshift_save", 0x121B3AC: "xorshift_restore",
}
PAGE = 4096
WINDOW_PAGES = 128
LOG_FLAGS = "exec,cpu,nochain"
REGISTER = re.compile(r"\bX(\d{2})=([0-9a-fA-F]{16})\b")
PC = re.compile(r"^\s*PC=([0-9a-fA-F]{16})\b")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def filter_spec(center):
    if type(center) is not int or not PAGE <= center < 2**63 or center % PAGE:
        raise ValueError("page-aligned positive guest library base required")
    first = center - WINDOW_PAGES // 2 * PAGE
    last = first + (WINDOW_PAGES - 1) * PAGE
    if first <= 0:
        raise ValueError("trace address window underflows")
    entries = [(base + min(TARGETS), base + max(TARGETS)) for base in range(first, last + PAGE, PAGE)]
    return dict(first_base=first, last_base=last,
        expression=",".join(f"{start:#x}..{end:#x}" for start, end in entries))


def check_base(center, actual):
    window = filter_spec(center)
    if type(actual) is not int or actual % PAGE or not window["first_base"] <= actual <= window["last_base"]:
        raise ValueError("actual guest library base escaped registered logging window")


def parse_cpu_log(path, *, base):
    if type(base) is not int or base <= 0 or base % PAGE:
        raise ValueError("exact guest library base required")
    current, registers, start = None, {}, None
    with Path(path).open("rb") as stream:
        while line := stream.readline():
            end = stream.tell()
            try:
                text = line.decode("ascii")
            except UnicodeDecodeError as error:
                raise ValueError("non-ASCII QEMU trace") from error
            match = PC.match(text)
            if match:
                if current is not None:
                    raise ValueError("incomplete CPU register dump")
                current, start = int(match[1], 16), end - len(line)
                registers = {}
            if current is None:
                continue
            for match in REGISTER.finditer(text):
                number = int(match[1])
                if number > 30 or number in registers:
                    raise ValueError("duplicate or invalid QEMU register")
                registers[number] = int(match[2], 16)
            if "PSTATE=" not in text:
                continue
            if set(registers) != set(range(31)):
                raise ValueError("incomplete CPU register identity")
            offset = current - base
            if offset in TARGETS:
                yield dict(file_offset=start, end_offset=end, function=TARGETS[offset],
                    pc=current, object=registers[0], argument=registers[1],
                    return_address=registers[30], caller_offset=registers[30] - base - 4)
            current, registers, start = None, {}, None
    if current is not None:
        raise ValueError("truncated final QEMU register dump")


def summarize(path, *, base, reset_offset, battle_offset, final_offset, global_object, manager_object):
    if (any(type(value) is not int for value in
            (reset_offset, battle_offset, final_offset, global_object, manager_object))
            or not 0 <= reset_offset <= battle_offset <= final_offset <= Path(path).stat().st_size
            or global_object <= 0 or manager_object <= 0 or global_object == manager_object):
        raise ValueError("trace phase or random object identity differs")
    records = []
    for entry in parse_cpu_log(path, base=base):
        if entry["end_offset"] > final_offset:
            continue
        entry["phase"] = ("startup" if entry["file_offset"] < reset_offset else
            "reset" if entry["file_offset"] < battle_offset else "battle")
        entry["object_kind"] = ("global_mt" if entry["object"] == global_object else
            "manager" if entry["object"] == manager_object else "other")
        records.append(entry)
    phases = {phase: dict(sorted(Counter(row["function"] for row in records
        if row["phase"] == phase).items())) for phase in ("startup", "reset", "battle")}
    if not phases["reset"].get("xorshift_seed") or not phases["reset"].get("mt_initialize"):
        raise ValueError("reset random-entry positive controls absent")
    return dict(records=records, phase_counts=phases, trace_sha256=sha(path),
        captured_entries=len(records), scope="observed entries of eight known random functions",
        other_algorithms_covered=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)
