import hashlib
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e1_semantics import (load_attachments, verify_damaged_data, verify_namespace,
                                 verify_semantics, verify_fmod_binding, verify_controls,
                                 verify_provider_identity, verify_stock_trajectory, FMOD_SYMBOLS)


def test_hash_checked_before_loading_attachment(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text('{"synthetic": true}')
    manifest = dict(schema="e1-semantics.v2", attachments=dict(fixture=dict(path=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), format="json")))
    assert load_attachments(manifest, tmp_path)[0]["fixture"] == dict(synthetic=True)
    path.write_text('{}')
    with pytest.raises(ValueError, match="attachment hash differs"):
        load_attachments(manifest, tmp_path)


def test_old_boolean_semantics_rejected():
    checks, errors = verify_semantics(dict(bytes=True, fallback=True), {})
    assert not any(checks.values())
    assert "manifest" in errors


def test_missing_attachments_are_unverified():
    checks, errors = verify_semantics(dict(schema="e1-semantics.v2"), {})
    assert not any(checks.values()) and errors


def boundary():
    names = ["stored", "deflated", "crc_bad_stored", "deflate_bad", "missing", "../stored", "./stored", "/stored"]
    def response(name):
        if name in names[4:]:
            return dict(opened=False)
        return dict(opened=True, length="240", read=-1 if name == "deflate_bad" else 240,
                    bytes=None if name == "deflate_bad" else [1] * 240,
                    zero_read=-1 if name == "deflate_bad" else 0,
                    remaining="240" if name == "deflate_bad" else "0",
                    again=-1 if name == "deflate_bad" else 0, seeks=[{}] * 6,
                    descriptor=dict(opened=name in ("stored", "crc_bad_stored")))
    return dict(schema="e1-native-asset-boundary.v2", cookie=1,
        metrics=dict(live_assets="0", failures="48"),
        rows=[dict(name=name, mode=mode, stock=response(name), replacement=response(name))
              for name in names for mode in range(4)])


def test_damaged_open_vs_read_timing_is_not_ignored():
    fixture = boundary()
    fixture["rows"][8]["replacement"] = dict(opened=False)
    fixture["rows"][12]["replacement"] = dict(opened=False)
    with pytest.raises(ValueError, match="crc_bad_stored:0, deflate_bad:0"):
        verify_damaged_data(fixture)


def test_intentional_diagnostic_errors_must_not_be_hidden():
    fixture = boundary()
    fixture["metrics"]["failures"] = "0"
    with pytest.raises(ValueError, match="must remain counted"):
        verify_damaged_data(fixture)


def test_boundary_missing_case_rejected():
    fixture = boundary()
    fixture["rows"].pop()
    with pytest.raises(ValueError, match="cases missing"):
        verify_damaged_data(fixture)


def test_empty_namespace_and_fmod_receipts_rejected():
    with pytest.raises(ValueError, match="24-case"):
        verify_namespace([], [], {}, {}, "fixture")
    with pytest.raises(ValueError, match="installation"):
        verify_fmod_binding(dict(schema="e1-fmod-binding.v1", installed=False))


def fmod_binding():
    events = [dict(kind="resolver_import", name=name, actual_target="libdl.so+fixture")
              for name in ("dlopen", "dlsym")]
    events.append(dict(kind="resolver_call", name="dlopen", caller="libfmod.so+fixture",
                       request="libandroid.so", result="0x123"))
    bindings = []
    for name in sorted(FMOD_SYMBOLS):
        target = "libe1provider.so+fixture_" + name
        events.extend([
            dict(kind="resolver_call", name="dlsym", caller="libfmod.so+fixture",
                 request=name, actual_target="libdl.so+fixture", argument0="0x123",
                 resolved_target="libandroid.so+fixture_" + name),
            dict(kind="resolved_binding", name=name, verified=True, slot_verified=True,
                 actual_target="libandroid.so+fixture_" + name),
            dict(kind="redirected_binding", name=name, verified=True, target=target),
        ])
        bindings.append(dict(name=name, target=target))
    events.append(dict(kind="initialize_leave", result=0, manager="0x456",
                       java_global_ref="0x789", bindings=bindings))
    return dict(schema="e1-fmod-binding.v1", installed=True, mode="redirect", errors=[],
        events=events, resource_calls=[dict(name="AAssetManager_fromJava", caller="libfmod.so+fixture",
            count=1, thread=1, phase="synthetic", target="libe1provider.so+fixture_AAssetManager_fromJava",
            samples=[dict(result="0x456", argument1="0x789")])])


def test_fmod_requires_actual_resolution_and_manager_propagation():
    fixture = fmod_binding()
    original = copy.deepcopy(fixture)
    verify_fmod_binding(fixture)
    assert fixture == original


@pytest.mark.parametrize("mutation", ["missing_dlsym", "wrong_handle", "wrong_target",
    "unknown_caller", "missing_dlopen", "unsaved_redirect", "manager", "java_reference"])
def test_fmod_incomplete_propagation_rejected(mutation):
    fixture = fmod_binding()
    dynamic = next(row for row in fixture["events"] if row.get("name") == "dlsym"
                   and row["kind"] == "resolver_call")
    initialized = fixture["events"][-1]
    if mutation == "missing_dlsym":
        fixture["events"].remove(dynamic)
    elif mutation == "wrong_handle":
        dynamic["argument0"] = "0xbad"
    elif mutation == "wrong_target":
        dynamic["resolved_target"] = "unknown"
    elif mutation == "unknown_caller":
        dynamic["caller"] = "unknown"
    elif mutation == "missing_dlopen":
        fixture["events"] = [row for row in fixture["events"]
                             if row.get("request") != "libandroid.so"]
    elif mutation == "unsaved_redirect":
        initialized["bindings"][0]["target"] = "libandroid.so+fixture"
    elif mutation == "manager":
        initialized["manager"] = "0xbad"
    else:
        initialized["java_global_ref"] = "0xbad"
    with pytest.raises(ValueError):
        verify_fmod_binding(fixture)


def test_control_mismatch_is_not_normalized():
    first = [dict(phase="first_tick", command="step 1", response=dict(ok=True, tick=1))]
    second = [dict(phase="first_tick", command="step 1", response=dict(ok=True, tick=2))]
    with pytest.raises(ValueError, match="responses differ"):
        verify_controls(first, second)


def test_resource_comparison_requires_same_provider_build_and_clean_metrics():
    fixture = dict(resource_build=dict(sha256="a" * 64, source_sha256="b" * 64),
                   replacement_build=dict(sha256="a" * 64, source_sha256="b" * 64),
                   resource_metrics=dict(result=0, operations="100", failures="0", live_assets="0", messages=[]))
    verify_provider_identity(fixture)
    fixture["resource_build"]["sha256"] = "c" * 64
    with pytest.raises(ValueError, match="different provider build"):
        verify_provider_identity(fixture)
    fixture["resource_build"]["sha256"] = "a" * 64
    fixture["resource_metrics"]["failures"] = "1"
    with pytest.raises(ValueError, match="failed or leaked"):
        verify_provider_identity(fixture)


def test_observer_must_preserve_complete_stock_trajectory():
    from verify_e1_gate import EXPECTED_TRAJECTORY
    fixture = dict(schema="e0-snapshot-comparison.v1", status="passed", **EXPECTED_TRAJECTORY)
    verify_stock_trajectory(fixture)
    fixture["total_bytes"] -= 1
    with pytest.raises(ValueError, match="unobserved baseline"):
        verify_stock_trajectory(fixture)
