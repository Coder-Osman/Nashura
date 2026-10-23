from collections import Counter
from pathlib import Path
import re
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fair-final-random-execution-v1"))
from trace_protocol import TARGETS, check_base, filter_spec, sha


HEX = re.compile(r"[0-9a-f]{16}")


def hexadecimal(value):
    if not HEX.fullmatch(value):
        raise ValueError("plugin register encoding differs")
    return int(value, 16)


def validate_seal(path, seal):
    if (seal.get("schema") != "FairFinalSupervisedTraceSeal/v1"
            or seal.get("closure") != "owned_process_stopped_after_native_terminal"
            or seal.get("trace_sha256") != sha(path)
            or seal.get("trace_bytes") != Path(path).stat().st_size
            or type(seal.get("final_offset")) is not int
            or not 0 < seal["final_offset"] <= seal["trace_bytes"]
            or seal.get("training_admissible") is not False or seal.get("gates_closed") is not False):
        raise ValueError("plugin supervised seal identity differs")


def parse(path, *, base, center, seal=None):
    check_base(center, base)
    window = filter_spec(center)
    if seal is not None:
        validate_seal(path, seal)
    with Path(path).open("rb") as stream:
        header = stream.readline()
        versions = {f"FairFinalPluginEntries/v{version}\t{window['first_base']:016x}\t{window['last_base']:016x}\n".encode(): version
            for version in (1, 2)}
        versions[b"FairFinalPluginEntries/v3\n"] = 3
        if header not in versions:
            raise ValueError("plugin trace header identity differs")
        version = versions[header]
        if seal is not None and version < 2:
            raise ValueError("supervised seal requires explicit instruction trace")
        if version == 3 and stream.readline() != f"ARM\t{base:016x}\n".encode():
            raise ValueError("plugin actual process arming identity differs")
        sequence = 0
        while line := stream.readline():
            start, end = stream.tell() - len(line), stream.tell()
            try:
                fields = line.decode("ascii").removesuffix("\n").split("\t")
            except UnicodeDecodeError as error:
                raise ValueError("plugin trace must be ASCII") from error
            if not line.endswith(b"\n"):
                raise ValueError("plugin trace partial record")
            if fields[0] == "END":
                if fields != ["END", str(sequence)] or stream.read(1):
                    raise ValueError("plugin trace footer or trailing bytes differ")
                return
            if (len(fields) != (7 if version == 1 else 8) or fields[0] != "E" or fields[1] != str(sequence)
                    or not fields[2].isdigit() or str(int(fields[2])) != fields[2]
                    or not 0 <= int(fields[2]) < 4096 or sequence >= 1000000):
                raise ValueError("plugin entry sequence or vCPU differs")
            values = list(map(hexadecimal, fields[3:]))
            pc, obj, argument, return_address = values if version == 1 else [values[index] for index in (0, 2, 3, 4)]
            if version == 3 and pc - base not in TARGETS:
                raise ValueError("plugin armed entry outside actual process targets")
            if version < 3 and not any(window["first_base"] <= pc - offset <= window["last_base"]
                    and (pc - offset) % 4096 == 0 for offset in TARGETS):
                raise ValueError("plugin entry outside registered instruction set")
            offset = pc - base
            if offset in TARGETS:
                entry = dict(file_offset=start, end_offset=end, function=TARGETS[offset],
                    pc=pc, object=obj, argument=argument, return_address=return_address,
                    caller_offset=return_address - base - 4, vcpu=int(fields[2]))
                if version >= 2:
                    entry["sampled_pc"] = values[1]
                yield entry
            sequence += 1
        if seal is not None:
            return
        raise ValueError("plugin trace clean footer absent")


def summarize(path, *, base, center, reset_offset, battle_offset, final_offset,
        global_object, manager_object, seal=None):
    if (any(type(value) is not int for value in
            (reset_offset, battle_offset, final_offset, global_object, manager_object))
            or not 0 <= reset_offset <= battle_offset <= final_offset <= Path(path).stat().st_size
            or global_object <= 0 or manager_object <= 0 or global_object == manager_object):
        raise ValueError("plugin phase or object identity differs")
    records = []
    with Path(path).open("rb") as stream:
        for boundary in (reset_offset, battle_offset, final_offset):
            if boundary:
                stream.seek(boundary - 1)
                if stream.read(1) != b"\n":
                    raise ValueError("plugin phase splits a record")
    if seal is not None and seal.get("final_offset") != final_offset:
        raise ValueError("plugin supervised seal phase differs")
    for entry in parse(path, base=base, center=center, seal=seal):
        if entry["file_offset"] < final_offset < entry["end_offset"]:
            raise ValueError("plugin final phase splits a record")
        if entry["end_offset"] > final_offset:
            continue
        if any(entry["file_offset"] < boundary < entry["end_offset"]
                for boundary in (reset_offset, battle_offset)):
            raise ValueError("plugin phase splits a record")
        entry["phase"] = ("startup" if entry["file_offset"] < reset_offset else
            "reset" if entry["file_offset"] < battle_offset else "battle")
        entry["object_kind"] = ("global_mt" if entry["object"] == global_object else
            "manager" if entry["object"] == manager_object else "other")
        records.append(entry)
    counts = {phase: dict(sorted(Counter(row["function"] for row in records if row["phase"] == phase).items()))
        for phase in ("startup", "reset", "battle")}
    if not counts["reset"].get("xorshift_seed") or not counts["reset"].get("mt_initialize"):
        raise ValueError("plugin reset positive controls absent")
    return dict(schema="FairFinalPluginEntrySummary/v1", records=records, phase_counts=counts,
        trace_sha256=sha(path), captured_entries=len(records),
        scope="executed entry instructions of eight known functions within one measured trajectory",
        other_algorithms_covered=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)
