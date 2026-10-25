from collections import Counter
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-live-command-v1"))
from live_audit import validate_live
from target_identity import validate_targets as validate_live_targets


TARGETS = {0xD2F838: "command_dispatch", 0xD2F83C: "command_dispatch_return"}
OPCODES = {
    0xD2F820: "880340f9", 0xD2F824: "611240f9", 0xD2F828: "e0031caa",
    0xD2F82C: "62008052", 0xD2F830: "e3031f2a", 0xD2F834: "080d40f9",
    0xD2F838: "00013fd6", 0xD2F83C: "e0010034",
}
SCOPE = "seventeen instruction points with explicit X8 virtual call targets in one complete trajectory"


def replace_once(source, before, after, count=1):
    if source.count(before) != count:
        raise ValueError("dispatch parent source shape differs: " + before[:70])
    return source.replace(before, after)


def protocol_source(source):
    source = replace_once(source, 'versions[b"FairFinalPluginEntries/v3\\n"] = 3',
        'versions[b"FairFinalPluginEntries/v4\\n"] = 4')
    source = replace_once(source, "if header not in versions:",
        "if header not in versions or versions[header] != 4:")
    source = replace_once(source, "version == 3", "version == 4", count=2)
    source = replace_once(source, "(7 if version == 1 else 8)", "9")
    source = replace_once(source, 'entry["sampled_pc"] = values[1]',
        'entry["sampled_pc"] = values[1]\n                    entry["target"] = values[5]')
    source += ("\nTARGETS.update(" + repr(TARGETS) + ")\n"
        "_live_summarize = summarize\n"
        "def summarize(*args, **kwargs):\n"
        "    result = _live_summarize(*args, **kwargs)\n"
        "    result['schema'] = 'FairFinalDispatchEntrySummary/v1'\n"
        f"    result['scope'] = {SCOPE!r}\n"
        "    return result\n")
    return source


def generated_sources(parent):
    files = dict(parent)
    source = files["random_entries.c"].decode()
    offsets = sorted((0xCFCF50, 0xCFD088, 0xD2E63C, 0xD309A8, 0xD30A00,
        0xD30AC4, 0xD30CCC, 0x121B118, 0x121B17C, 0x121B188, 0x121B284,
        0x121B2E4, 0x121B350, 0x121B394, 0x121B3AC))
    before = ", ".join(f"0x{offset:X}" for offset in offsets)
    after = ", ".join(f"0x{offset:X}" for offset in sorted({*offsets, *TARGETS}))
    source = replace_once(source, before, after)
    source = replace_once(source, "target_index < 15", "target_index < 17", count=2)
    source = replace_once(source, "offsets[14]", "offsets[16]")
    source = replace_once(source, "registers[4]", "registers[5]")
    source = replace_once(source, '{"pc", "x0", "x1", "x30"}',
        '{"pc", "x0", "x1", "x30", "x8"}')
    source = replace_once(source, "register_index < 4", "register_index < 5", count=3)
    source = replace_once(source, "values[4] = {0}", "values[5] = {0}")
    source = replace_once(source, '"\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\n", sequence, cpu_index,',
        '"\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\n", sequence, cpu_index,')
    source = replace_once(source, "expected_pc, values[0], values[1], values[2], values[3])",
        "expected_pc, values[0], values[1], values[2], values[3], values[4])")
    source = replace_once(source, "FairFinalPluginEntries/v3", "FairFinalPluginEntries/v4")
    files["random_entries.c"] = source.encode()
    files["plugin_protocol.py"] = protocol_source(files["plugin_protocol.py"].decode()).encode()
    return files


def verifier_source(source):
    source = replace_once(source, "FairFinalPluginEntries/v3", "FairFinalPluginEntries/v4", count=1)
    return replace_once(source, "requires explicit v3 trace", "requires explicit v4 trace")


def validate_targets(library):
    result = validate_live_targets(library)
    import lief

    binary = lief.parse(str(library))
    for offset, expected in OPCODES.items():
        if bytes(binary.get_content_from_virtual_address(offset, 4)).hex() != expected:
            raise ValueError("dispatch instruction bytes differ")
    result["dispatch_instruction_bytes"] = {hex(offset): value for offset, value in OPCODES.items()}
    return result


def validate_dispatch(summary, report):
    if summary.get("schema") != "FairFinalDispatchEntrySummary/v1":
        raise ValueError("dispatch summary schema differs")
    live = validate_live(summary, report)
    states, targets, codes = {}, Counter(), Counter()
    dispatches, matched, completed = 0, 0, 0
    for row in summary["records"]:
        name = row["function"]
        if name not in {*TARGETS.values(), "ct86_enter", "ct86_return"}:
            continue
        if row["phase"] != "battle" or type(row.get("target")) is not int:
            raise ValueError("dispatch phase or X8 register absent")
        state = states.setdefault(row["vcpu"], dict(call=None, entered=False, result=None))
        if name == "command_dispatch":
            if state["call"] is not None or row["object"] <= 0 or row["target"] <= 0:
                raise ValueError("dispatch overlapping or invalid call")
            base = row["pc"] - 0xD2F838
            if base <= 0 or base % 4096:
                raise ValueError("dispatch base differs")
            state.update(call=row, entered=False, result=None)
            dispatches += 1
            targets[row["target"] - base] += 1
        elif name == "ct86_enter":
            call = state["call"]
            if (call is None or state["entered"] or row["pc"] != call["target"]
                    or row["object"] != call["object"] or row["argument"] != call["argument"]
                    or row["return_address"] != call["pc"] + 4):
                raise ValueError("dispatch target, object, argument or LR differs")
            state["entered"] = True
            matched += 1
        elif name == "ct86_return":
            if state["call"] is None or not state["entered"] or state["result"] is not None:
                raise ValueError("dispatch executor result without matching entry")
            state["result"] = row["object"] & 0xFFFFFFFF
        else:
            call = state["call"]
            if call is None or row["pc"] != call["pc"] + 4:
                raise ValueError("dispatch return without matching call")
            result = row["object"] & 0xFFFFFFFF
            is_ct86 = call["target"] == call["pc"] - 0xD2F838 + 0xD30A00
            if is_ct86 and (not state["entered"] or state["result"] is None
                    or state["result"] != result):
                raise ValueError("dispatch return differs from ct86 result")
            if state["entered"] and not is_ct86:
                raise ValueError("dispatch executor entered from another target")
            if is_ct86:
                completed += 1
                codes[result] += 1
            state.update(call=None, entered=False, result=None)
    if any(state["call"] is not None for state in states.values()):
        raise ValueError("dispatch trace ends inside call")
    if not completed or matched != completed or completed != live["executor_calls"]:
        raise ValueError("dispatch complete ct86 positive control absent")
    live["virtual_dispatch_callsite_proven"] = True
    return dict(schema="FairFinalDispatchCommandEvidence/v1", status="verified_limited_scope",
        live_command=live, dispatch_calls=dispatches, paired_ct86_calls=completed,
        target_offsets={hex(offset): count for offset, count in sorted(targets.items())},
        ct86_return_codes={str(code): count for code, count in sorted(codes.items())},
        virtual_dispatch_callsite_proven=True, per_reservation_object_mapping_proven=False,
        all_legal_actions_or_rejection_branches_covered=False,
        training_admissible=False, gates_closed=False)
