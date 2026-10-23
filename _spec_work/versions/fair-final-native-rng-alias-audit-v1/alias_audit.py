import argparse
import hashlib
import json
from pathlib import Path

from tools.fair_final_prng_audit import LIBG_SHA
from tools.fair_final_random_inventory import direct_branches


MT_INITIALIZE = 0x121b118
EXPECTED_DIRECT = {
    0x7f5434, 0xd35f9c, 0xdf47e8, 0xf3f498, 0xfaa2a4, 0xfcdcf4,
    0xfd0134, 0xfd01b8, 0xfd16ac, 0xfdb040, 0xfdeea8, 0x1174854,
    0x1175f44, 0x1176750, 0x1176958, 0x1177e4c, 0x1179684, 0x117bb1c,
}
REGIONS = {
    "tail_entry": (0x7f5434, 4, "dc6cb71c7e04ee530f2d611939280a9975056f73708b64dae4f98391a8f13d77"),
    "time_seed_path": (0x1a1cb10, 36, "f7d36d2ebfecf92c9fc428b263b23b77c6281e0559b6f8cfc2ddd328273ba0cd"),
    "time_plt": (0x17e5260, 16, "34d00afc4dedbfcb5a1002fdaff9292848b94961a4fcbb8389b6bca009ad7f8e"),
}
TIME_PATH = [
    ("stp", "x29, x30, [sp, #-0x10]!"), ("mov", "x29, sp"),
    ("mov", "x0, xzr"), ("bl", "#0x17e5260"), ("mov", "x1, x0"),
    ("adrp", "x0, #0x19fc000"), ("add", "x0, x0, #0x368"),
    ("ldp", "x29, x30, [sp], #0x10"), ("b", "#0x7f5434"),
]


def tail_reference_closure(edges, target):
    if type(target) is not int or target < 0 or target % 4:
        raise ValueError("aligned nonnegative target required")
    incoming, locations = {}, set()
    for location, destination, opcode in edges:
        if (type(location) is not int or type(destination) is not int
                or location < 0 or location % 4 or destination % 4
                or opcode not in ("b", "bl") or location in locations):
            raise ValueError("invalid or duplicate direct branch")
        locations.add(location)
        incoming.setdefault(destination, []).append((location, opcode))
    pending, targets, found = [target], set(), {}
    while pending:
        destination = pending.pop()
        if destination in targets:
            continue
        targets.add(destination)
        for location, opcode in incoming.get(destination, []):
            found[location] = (destination, opcode)
            if opcode == "b":
                pending.append(location)
    return [dict(location=hex(location), target=hex(destination), opcode=opcode)
        for location, (destination, opcode) in sorted(found.items())]


def validate_time_path(instructions):
    if instructions != TIME_PATH:
        raise ValueError("time seed path instruction contract differs")


def build_report(path):
    import capstone
    import lief

    if hashlib.sha256(path.read_bytes()).hexdigest() != LIBG_SHA:
        raise ValueError("alias audit library identity differs")
    binary = lief.parse(str(path))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    regions = {}
    for name, (start, size, expected) in REGIONS.items():
        content = bytes(binary.get_content_from_virtual_address(start, size))
        if len(content) != size or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("alias audit region differs: " + name)
        instructions = [(item.mnemonic, item.op_str) for item in decoder.disasm(content, start)]
        if len(instructions) * 4 != size:
            raise ValueError("alias audit incomplete instruction coverage")
        regions[name] = dict(start=hex(start), size=size, sha256=expected,
            instructions=instructions)
    validate_time_path(regions["time_seed_path"]["instructions"])
    if regions["tail_entry"]["instructions"] != [("b", "#0x121b118")]:
        raise ValueError("alias entry no longer transfers directly to initializer")
    imports = [entry for entry in binary.relocations if entry.address == 0x18e96f0]
    if (len(imports) != 1 or imports[0].type != lief.ELF.Relocation.TYPE.AARCH64_JUMP_SLOT
            or not imports[0].has_symbol or imports[0].symbol.name != "time"):
        raise ValueError("time import identity differs")
    if regions["time_plt"]["instructions"] != [
            ("adrp", "x16, #0x18e9000"), ("ldr", "x17, [x16, #0x6f0]"),
            ("add", "x16, x16, #0x6f0"), ("br", "x17")]:
        raise ValueError("time PLT does not resolve to verified import slot")
    edges, segments = [], []
    global_mappings = []
    for segment in binary.segments:
        if segment.type != lief.ELF.Segment.TYPE.LOAD:
            continue
        if segment.virtual_address <= 0x19fc368 < segment.virtual_address + segment.virtual_size:
            global_mappings.append(dict(start=hex(segment.virtual_address),
                size=segment.virtual_size, writable=bool(int(segment.flags) & 2)))
        if int(segment.flags) & 1:
            content = bytes(segment.content)
            segments.append(dict(start=hex(segment.virtual_address), size=len(content),
                sha256=hashlib.sha256(content).hexdigest()))
            edges.extend(direct_branches(content, segment.virtual_address))
    if len(global_mappings) != 1 or not global_mappings[0]["writable"]:
        raise ValueError("global generator has no unique writable LOAD mapping")
    direct = {location for location, destination, opcode in edges if destination == MT_INITIALIZE}
    if direct != EXPECTED_DIRECT:
        raise ValueError("direct initializer reference census differs")
    closure = tail_reference_closure(edges, MT_INITIALIZE)
    extra = [row for row in closure if int(row["location"], 16) not in direct]
    if extra != [dict(location="0x1a1cb30", target="0x7f5434", opcode="b")]:
        raise ValueError("initializer tail reference closure differs")
    return dict(schema="fair-final-native-rng-alias-audit.v1",
        status="passed_static_limited_scope", libg_sha256=LIBG_SHA, regions=regions,
        executable_segments=segments, direct_count=len(direct), closure_count=len(closure),
        tail_reference_closure=closure, additional_references=extra,
        time_seed=dict(path_start="0x1a1cb10", import_slot="0x18e96f0",
            destination="0x19fc368", mapping=global_mappings[0],
            semantics="time(NULL) return passed as uint32 seed to MT initializer at global address",
            path_entry_reachable_proven=False, fixed_hog_usage_proven=False,
            snapshot_coverage_proven=False),
        scope="direct B/BL census extended through unconditional B entry chains; not a complete call graph",
        unresolved=["path entry and fallthrough reachability", "global object consumers and lifecycle",
            "snapshot inclusion of process global state", "indirect calls and other random algorithms",
            "remaining direct initializer semantics and fixed-deck reachability"],
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = build_report(arguments.library)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "direct_count", "closure_count")}))


if __name__ == "__main__":
    main()
