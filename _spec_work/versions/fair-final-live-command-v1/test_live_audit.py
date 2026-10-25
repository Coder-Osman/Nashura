from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_audit import EXTRA_TARGETS, ROOT, generated_sources, validate_live
import run_plugin


BASE = 0x100000000


def trace():
    def entry(name, value=0, caller=0x12340):
        offset = next(offset for offset, candidate in EXTRA_TARGETS.items() if candidate == name)
        return dict(function=name, phase="battle", vcpu=0, object=value,
            pc=BASE + offset, return_address=BASE + caller)
    return dict(records=[entry("ct86_factory_branch"), entry("ct86_constructor", 0x123000),
        entry("ct86_enter", 0x123000), entry("position_enter", 0x456000, 0xD30CCC),
        entry("position_return", 0, 0xD30CCC), entry("ct86_after_position", 0, 0xD30CCC),
        entry("ct86_return", 0)])


def test_complete_synthetic_chain():
    result = validate_live(trace(), dict(executed=1))
    assert result["executor_calls"] == result["ct86_helper_calls"] == 1
    assert result["executor_return_codes"] == {"0": 1}
    assert result["gates_closed"] is False
    assert result["virtual_dispatch_callsite_proven"] is False


@pytest.mark.parametrize("index", range(2, 7))
def test_missing_call_boundary_rejected(index):
    value = trace()
    del value["records"][index]
    with pytest.raises(ValueError):
        validate_live(value, dict(executed=1))


@pytest.mark.parametrize("index,field,value", [
    (1, "object", 0), (2, "object", 99), (3, "return_address", BASE + 7),
    (4, "object", 6), (4, "return_address", BASE + 8), (5, "object", 1),
    (6, "return_address", BASE + 9), (2, "phase", "reset"), (4, "vcpu", 1),
])
def test_identity_and_result_corruption_rejected(index, field, value):
    altered = trace()
    altered["records"][index][field] = value
    with pytest.raises(ValueError):
        validate_live(altered, dict(executed=1))


def test_independent_vcpu_interleaving():
    value = trace()
    second = deepcopy(value["records"])
    for row in second:
        row["vcpu"] = 1
    value["records"] = [row for pair in zip(value["records"], second, strict=True) for row in pair]
    assert validate_live(value, dict(executed=2))["executor_calls"] == 2


def test_nonzero_executor_result_is_reported():
    value = trace()
    value["records"][-1]["object"] = 25
    assert validate_live(value, dict(executed=1))["executor_return_codes"] == {"25": 1}


def test_parent_generation_is_explicit_and_deterministic():
    parent = run_plugin.runtime_sources()
    original = deepcopy(parent)
    result = generated_sources(parent)
    assert parent == original
    assert result == generated_sources(parent)
    assert b"target_index < 15" in result["random_entries.c"]
    assert b"offsets[14]" in result["random_entries.c"]
    assert b"0xD30CCC" in result["random_entries.c"]
    assert b"TARGETS.update" in result["plugin_protocol.py"]
    assert b"timeout=660" in result["plugin_worker.py"]
    assert b"service_source(source)" in result["plugin_launcher.py"]
    assert result["trace_launcher.py"] == parent["trace_launcher.py"]


def test_changed_parent_shape_rejected():
    parent = run_plugin.runtime_sources()
    parent["random_entries.c"] = parent["random_entries.c"].replace(b"target_index < 8", b"target_index < 9")
    with pytest.raises(ValueError, match="shape"):
        generated_sources(parent)


def test_prearm_filter_preserves_parent_targets_and_all_armed_targets():
    from live_audit import PREARM_OFFSETS

    parent = run_plugin.runtime_sources()
    source = generated_sources(parent)["random_entries.c"].decode()
    assert len(PREARM_OFFSETS) == 8
    assert not {offset & 4095 for offset in PREARM_OFFSETS} & {
        offset & 4095 for offset in EXTRA_TARGETS}
    for offset in PREARM_OFFSETS:
        assert f"(expected_pc & 4095) != 0x{offset & 4095:X}" in source
    assert source.index("base == 0 &&") < source.index('fopen(arm_path, "rb")')
    assert "base != 0 && !target_address(expected_pc, base)" in source
    altered = deepcopy(parent)
    altered["random_entries.c"] = altered["random_entries.c"].replace(
        b"base != 0 && !target_address(expected_pc, base)", b"changed")
    with pytest.raises(ValueError, match="prearm source shape"):
        generated_sources(altered)


@pytest.mark.parametrize("name,before", [
    ("plugin_launcher.py", b"    specification.loader.exec_module(service)"),
    ("plugin_worker.py", b"wait_for_engine(engine, instance, timeout=180)"),
])
def test_changed_startup_parent_shape_rejected(name, before):
    parent = run_plugin.runtime_sources()
    parent[name] = parent[name].replace(before, b"changed")
    with pytest.raises(ValueError, match="parent source shape"):
        generated_sources(parent)


def test_independent_root_retains_parent_source_bytes(monkeypatch):
    import run_live

    parent = run_plugin.runtime_sources()
    monkeypatch.setattr(run_plugin, "ROOT", ROOT)
    assert run_live.sources() == generated_sources(parent)


def test_target_identity_rejects_wrong_library(tmp_path):
    from target_identity import validate_targets

    library = tmp_path / "wrong.so"
    library.write_bytes(b"not libg")
    with pytest.raises(ValueError, match="SHA"):
        validate_targets(library)


def test_target_instructions_match_private_frozen_library():
    from target_identity import OPCODES, validate_targets

    library = ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not library.exists():
        pytest.skip("private frozen library not present")
    assert set(OPCODES) == set(EXTRA_TARGETS)
    assert len(validate_targets(library)["instruction_bytes"]) == 7


def test_uploaded_and_independent_summary_match(tmp_path):
    import plugin_protocol
    import run_live

    parent = run_live.sources()
    namespace = dict(__file__=str(ROOT.parent / "fair-final-plugin-runtime-v1/plugin_protocol.py"))
    exec(compile(parent["plugin_protocol.py"], "generated-plugin-protocol", "exec"), namespace)
    header = f"FairFinalPluginEntries/v3\nARM\t{BASE:016x}\n"
    records = ""
    for sequence, offset in enumerate((0x121B118, 0x121B2E4)):
        records += (f"E\t{sequence}\t0\t{BASE+offset:016x}\t{BASE+offset:016x}"
            f"\t{BASE+0x200:016x}\t{0:016x}\t{BASE+0x100:016x}\n")
    boundary = len((header + records).encode())
    log = tmp_path / "qemu.log"
    log.write_text(header + records + "END\t2\n")
    arguments = dict(base=BASE, center=BASE, reset_offset=len(header.encode()),
        battle_offset=boundary, final_offset=boundary, global_object=BASE+1, manager_object=BASE+2)
    actual = run_live.summarize(log, **arguments)
    assert actual == namespace["summarize"](log, **arguments)
    assert actual["scope"] != run_live.PARENT_SUMMARIZE(log, **arguments)["scope"]
