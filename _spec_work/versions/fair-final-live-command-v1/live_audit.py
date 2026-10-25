from collections import Counter
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-plugin-runtime-v1"))
import plugin_protocol


SCOPE = "fifteen registered instruction points in one complete trajectory"
PREARM_OFFSETS = (0x121B118, 0x121B17C, 0x121B188, 0x121B284,
    0x121B2E4, 0x121B350, 0x121B394, 0x121B3AC)
EXTRA_TARGETS = {
    0xCFCF50: "position_enter",
    0xCFD088: "position_return",
    0xD2E63C: "ct86_factory_branch",
    0xD309A8: "ct86_constructor",
    0xD30A00: "ct86_enter",
    0xD30AC4: "ct86_return",
    0xD30CCC: "ct86_after_position",
}


def generated_sources(parent):
    files = dict(parent)
    source = files["random_entries.c"].decode()
    old = "    0x121B118, 0x121B17C, 0x121B188, 0x121B284,\n    0x121B2E4, 0x121B350, 0x121B394, 0x121B3AC"
    offsets = sorted({*EXTRA_TARGETS, *plugin_protocol.TARGETS})
    replacement = ", ".join(f"0x{offset:X}" for offset in offsets)
    transforms = [(old, "    " + replacement, 1),
        ("target_index < 8", f"target_index < {len(offsets)}", 2),
        ("offsets[7]", f"offsets[{len(offsets) - 1}]", 1)]
    for before, after, expected in transforms:
        if source.count(before) != expected:
            raise ValueError("live plugin parent source shape differs")
        source = source.replace(before, after)
    before = "    if (arm_path != NULL && base != 0 && !target_address(expected_pc, base)) {"
    if source.count(before) != 1:
        raise ValueError("live plugin prearm source shape differs")
    conditions = " && ".join(f"(expected_pc & 4095) != 0x{offset & 4095:X}"
        for offset in PREARM_OFFSETS)
    source = source.replace(before,
        f"    if (arm_path != NULL && base == 0 && {conditions}) {{\n"
        "        return;\n"
        "    }\n" + before)
    files["random_entries.c"] = source.encode()
    launcher = files["plugin_launcher.py"].decode()
    before = "    specification.loader.exec_module(service)"
    if launcher.count(before) != 1:
        raise ValueError("live launcher parent source shape differs")
    launcher = launcher.replace(before,
        "    from trace_launcher import service_source\n"
        "    updated, _ = service_source(source)\n"
        "    exec(compile(updated, str(source), 'exec'), service.__dict__)")
    files["plugin_launcher.py"] = launcher.encode()
    worker = files["plugin_worker.py"].decode()
    before = "wait_for_engine(engine, instance, timeout=180)"
    if worker.count(before) != 1:
        raise ValueError("live worker parent source shape differs")
    files["plugin_worker.py"] = worker.replace(before,
        "wait_for_engine(engine, instance, timeout=660)").encode()
    files["plugin_protocol.py"] += ("\n\nTARGETS.update(" + repr(EXTRA_TARGETS) + ")\n"
        "_parent_summarize = summarize\n"
        "def summarize(*args, **kwargs):\n"
        "    result = _parent_summarize(*args, **kwargs)\n"
        f"    result['scope'] = {SCOPE!r}\n"
        "    return result\n").encode()
    return files


def validate_live(summary, report):
    states, constructed, codes, helper_codes, callers = {}, set(), Counter(), Counter(), Counter()
    factories, constructors, helpers, matched_objects, after_calls = 0, 0, 0, 0, 0
    ct86_helpers, factory_constructors = 0, 0
    for row in summary["records"]:
        name = row["function"]
        if name not in EXTRA_TARGETS.values():
            continue
        if row["phase"] != "battle":
            raise ValueError("live command observed outside battle phase")
        state = states.setdefault(row["vcpu"], dict(executor=None, helpers=[], after=None))
        if name == "ct86_factory_branch":
            factories += 1
        elif name == "ct86_constructor":
            if row["object"] == 0:
                raise ValueError("live constructor null object")
            constructed.add(row["object"])
            constructors += 1
            factory_constructors += row["return_address"] == row["pc"] - 0xD309A8 + 0xD2E650
        elif name == "ct86_enter":
            if state["executor"] is not None or state["helpers"] or state["after"] is not None:
                raise ValueError("live executor overlapping entry")
            state["executor"] = row
            matched_objects += row["object"] in constructed
        elif name == "position_enter":
            if state["after"] is not None:
                raise ValueError("live missing after-helper callback")
            state["helpers"].append(row)
            if row["return_address"] == row["pc"] - 0xCFCF50 + 0xD30CCC:
                if state["executor"] is None:
                    raise ValueError("ct86 helper outside executor")
                ct86_helpers += 1
        elif name == "position_return":
            if not state["helpers"]:
                raise ValueError("live helper return without entry")
            entry = state["helpers"].pop()
            if row["return_address"] != entry["return_address"]:
                raise ValueError("live helper return LR differs")
            if row["object"] & 0xFFFFFFFF not in range(6):
                raise ValueError("live helper result outside audited range")
            helpers += 1
            helper_codes[row["object"] & 0xFFFFFFFF] += 1
            if entry["return_address"] == entry["pc"] - 0xCFCF50 + 0xD30CCC:
                state["after"] = row["object"] & 0xFFFFFFFF
        elif name == "ct86_after_position":
            if state["executor"] is None or state["after"] is None:
                raise ValueError("live after-helper callback without completed helper")
            if row["object"] & 0xFFFFFFFF != state["after"]:
                raise ValueError("live helper return value changed before caller")
            state["after"] = None
            after_calls += 1
        elif name == "ct86_return":
            entry = state["executor"]
            if entry is None or state["helpers"] or state["after"] is not None:
                raise ValueError("live executor incomplete return")
            if row["return_address"] != entry["return_address"]:
                raise ValueError("live executor return LR differs")
            codes[row["object"] & 0xFFFFFFFF] += 1
            callers[entry["return_address"] - (entry["pc"] - 0xD30A00) - 4] += 1
            state["executor"] = None
    if any(state["executor"] is not None or state["helpers"] or state["after"] is not None
            for state in states.values()):
        raise ValueError("live command trace ends inside a call")
    calls = sum(codes.values())
    if not calls or not helpers or not after_calls or after_calls != ct86_helpers:
        raise ValueError("live command execution positive controls absent")
    if matched_objects != calls:
        raise ValueError("live executor object lacks observed constructor")
    return dict(schema="FairFinalLiveCommandEvidence/v1", status="verified_limited_scope",
        factory_branch_entries=factories, constructor_entries=constructors,
        constructor_calls_from_factory=factory_constructors,
        executor_calls=calls, executor_return_codes={str(code): count for code, count in sorted(codes.items())},
        executor_caller_offsets={hex(offset): count for offset, count in sorted(callers.items())},
        helper_calls=helpers, ct86_helper_calls=ct86_helpers, after_helper_callbacks=after_calls,
        helper_return_codes={str(code): count for code, count in sorted(helper_codes.items())},
        executor_objects_seen_in_constructor=matched_objects,
        reported_executed_reservations=report["executed"],
        all_legal_actions_or_rejection_branches_covered=False,
        virtual_dispatch_callsite_proven=False,
        training_admissible=False, gates_closed=False)
