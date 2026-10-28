import copy

import pytest

from audit_linux_owner_failure import ENGINE, compare


def test_failure_requires_matching_control_and_covered_constructor():
    base = 0x10000000
    common = {"status": "BLOCKED", "timed_out": False, "inputs_unchanged": True,
              "resources_unchanged": True, "auxiliary_unchanged": True,
              "inputs_before": {"libg.so": ENGINE}}
    debug = dict(common, exit_code=1)
    control = dict(common, exit_code=-11)
    observation = {"error": None, "constructors_covered": False, "final_stop": "SIGSEGV",
                   "targets": [{"name": "owner_constructor", "base": base, "armed": True, "calls": 1},
                               {"name": "engine_database_init", "armed": True, "calls": 0}],
                   "stop_registers": {"pc": base + 0x17d8b14, "x30": base + 0x140b320, "x0": 0}}
    output = "OWNER_SLOT_BEFORE_NONNULL\t0\nPHASE\towner_getter_enter\n"
    error = "Fatal signal 11 (SIGSEGV), fault addr 0x0"
    assert compare(debug, observation, output, control, output, error)["constructor_calls"] == 1
    for damage in ("missing_constructor", "wrong_pc", "different_control", "returned", "changed_resources"):
        d, o, c, text = copy.deepcopy(debug), copy.deepcopy(observation), copy.deepcopy(control), output
        if damage == "missing_constructor": o["targets"][0]["calls"] = 0
        elif damage == "wrong_pc": o["stop_registers"]["pc"] += 4
        elif damage == "different_control": c["exit_code"] = 0
        elif damage == "returned": text += "PHASE\towner_getter_return\n"
        else: c["resources_unchanged"] = False
        with pytest.raises(ValueError):
            compare(d, o, text, c, output, error)
