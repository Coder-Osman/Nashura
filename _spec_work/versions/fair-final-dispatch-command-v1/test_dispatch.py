from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_audit import OPCODES, ROOT, TARGETS, generated_sources, validate_dispatch, validate_targets
import run_dispatch


BASE = 0x100000000
LIVE_TARGETS = {
    "ct86_factory_branch": 0xD2E63C, "ct86_constructor": 0xD309A8,
    "ct86_enter": 0xD30A00, "ct86_return": 0xD30AC4,
    "position_enter": 0xCFCF50, "position_return": 0xCFD088,
    "ct86_after_position": 0xD30CCC,
    **{name: offset for offset, name in TARGETS.items()},
}


def trace():
    def entry(name, value=0, caller=0xD2F83C):
        return dict(function=name, phase="battle", vcpu=0, object=value,
            argument=0x987000, pc=BASE + LIVE_TARGETS[name],
            return_address=BASE + caller, target=BASE + 0xD30A00)
    return dict(schema="FairFinalDispatchEntrySummary/v1", records=[
        entry("ct86_factory_branch"), entry("ct86_constructor", 0x123000, 0xD2E650),
        entry("command_dispatch", 0x123000), entry("ct86_enter", 0x123000),
        entry("position_enter", 0x456000, 0xD30CCC),
        entry("position_return", 0, 0xD30CCC), entry("ct86_after_position", 0, 0xD30CCC),
        entry("ct86_return", 0), entry("command_dispatch_return", 0)])


def test_explicit_synthetic_virtual_call_proof():
    result = validate_dispatch(trace(), dict(executed=1))
    assert result["dispatch_calls"] == result["paired_ct86_calls"] == 1
    assert result["target_offsets"] == {"0xd30a00": 1}
    assert result["virtual_dispatch_callsite_proven"] is True
    assert result["live_command"]["virtual_dispatch_callsite_proven"] is True
    assert result["per_reservation_object_mapping_proven"] is False
    assert result["gates_closed"] is result["training_admissible"] is False


@pytest.mark.parametrize("index", range(2, 9))
def test_missing_call_boundary_rejected(index):
    value = trace()
    del value["records"][index]
    with pytest.raises(ValueError):
        validate_dispatch(value, dict(executed=1))


@pytest.mark.parametrize("index,field,value", [
    (2, "target", 0), (2, "target", BASE + 0xD30A04), (2, "object", 0),
    (3, "object", 9), (3, "argument", 9), (3, "return_address", BASE + 8),
    (8, "object", 1), (8, "pc", BASE + 0xD2F840), (8, "vcpu", 1),
    (2, "phase", "reset"), (2, "target", None),
])
def test_identity_and_result_corruption_rejected(index, field, value):
    altered = trace()
    altered["records"][index][field] = value
    with pytest.raises(ValueError):
        validate_dispatch(altered, dict(executed=1))


def test_independent_vcpu_interleaving():
    value = trace()
    second = deepcopy(value["records"])
    for row in second:
        row["vcpu"] = 1
    value["records"] = [row for pair in zip(value["records"], second, strict=True) for row in pair]
    assert validate_dispatch(value, dict(executed=2))["paired_ct86_calls"] == 2


def test_other_command_target_is_reported_without_ct86_claim():
    value = trace()
    second = deepcopy([value["records"][2], value["records"][8]])
    second[0]["target"] = BASE + 0x10000
    value["records"].extend(second)
    result = validate_dispatch(value, dict(executed=1))
    assert result["dispatch_calls"] == 2
    assert result["paired_ct86_calls"] == 1
    assert result["target_offsets"] == {"0x10000": 1, "0xd30a00": 1}


def test_nonzero_result_is_compared_and_reported():
    value = trace()
    for index in (7, 8):
        value["records"][index]["object"] = 25
    assert validate_dispatch(value, dict(executed=1))["ct86_return_codes"] == {"25": 1}


def protocol():
    namespace = dict(__file__=str(ROOT.parent / "fair-final-plugin-runtime-v1/plugin_protocol.py"))
    exec(compile(run_dispatch.sources()["plugin_protocol.py"], "generated-protocol", "exec"), namespace)
    return namespace


def log_bytes():
    header = f"FairFinalPluginEntries/v4\nARM\t{BASE:016x}\n"
    records = ""
    for sequence, offset in enumerate((0x121B118, 0x121B2E4, 0xD2F838)):
        records += (f"E\t{sequence}\t0\t{BASE+offset:016x}\t{BASE+offset:016x}"
            f"\t{BASE+0x200:016x}\t{BASE+0x400:016x}\t{BASE+0x100:016x}"
            f"\t{BASE+0xD30A00:016x}\n")
    return header, records


def test_uploaded_protocol_preserves_distinct_x1_and_x8(tmp_path):
    namespace = protocol()
    header, records = log_bytes()
    log = tmp_path / "qemu.log"
    log.write_text(header + records + "END\t3\n")
    rows = list(namespace["parse"](log, base=BASE, center=BASE))
    assert len(rows) == 3
    assert rows[-1]["function"] == "command_dispatch"
    assert rows[-1]["argument"] == BASE + 0x400
    assert rows[-1]["target"] == BASE + 0xD30A00
    arguments = dict(base=BASE, center=BASE, reset_offset=len(header.encode()),
        battle_offset=len((header + records).encode()), final_offset=len((header + records).encode()),
        global_object=BASE + 1, manager_object=BASE + 2)
    result = namespace["summarize"](log, **arguments)
    assert result["schema"] == "FairFinalDispatchEntrySummary/v1"
    assert result["captured_entries"] == 3


@pytest.mark.parametrize("kind", ("v3", "missing_x8", "bad_x8", "partial", "unknown_target"))
def test_wrong_log_contract_rejected(tmp_path, kind):
    namespace = protocol()
    header, records = log_bytes()
    data = header + records + "END\t3\n"
    if kind == "v3":
        data = data.replace("Entries/v4", "Entries/v3")
    elif kind == "missing_x8":
        data = data.replace(f"\t{BASE+0xD30A00:016x}", "")
    elif kind == "bad_x8":
        data = data.replace(f"\t{BASE+0xD30A00:016x}", "\tbad")
    elif kind == "partial":
        data = data[:-1]
    else:
        data = data.replace(f"{BASE+0xD2F838:016x}", f"{BASE+0xD2F840:016x}")
    log = tmp_path / "qemu.log"
    log.write_text(data)
    with pytest.raises(ValueError):
        list(namespace["parse"](log, base=BASE, center=BASE))


def test_generation_retains_frozen_parent_bytes():
    parent = deepcopy(run_dispatch.LIVE_SOURCES)
    source = generated_sources(parent)
    assert source == generated_sources(parent)
    assert parent == run_dispatch.LIVE_SOURCES
    assert b'"x30", "x8"' in source["random_entries.c"]
    assert b"target_index < 17" in source["random_entries.c"]
    assert b"values[4]" in source["random_entries.c"]
    assert b"FairFinalPluginEntries/v4" in source["random_entries.c"]
    assert source["plugin_launcher.py"] == parent["plugin_launcher.py"]
    assert source["plugin_worker.py"] == parent["plugin_worker.py"]


def test_changed_parent_generation_is_rejected():
    parent = deepcopy(run_dispatch.LIVE_SOURCES)
    parent["random_entries.c"] = parent["random_entries.c"].replace(b"values[4] = {0}", b"changed")
    with pytest.raises(ValueError, match="shape"):
        generated_sources(parent)


def test_fixed_library_instruction_identity():
    library = ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not library.exists():
        pytest.skip("private frozen library not present")
    result = validate_targets(library)
    assert len(result["dispatch_instruction_bytes"]) == len(OPCODES)


def test_wrong_library_is_rejected(tmp_path):
    library = tmp_path / "wrong.so"
    library.write_bytes(b"wrong library")
    with pytest.raises(ValueError, match="SHA"):
        validate_targets(library)
