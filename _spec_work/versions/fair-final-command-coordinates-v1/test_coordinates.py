from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from coordinate_audit import expected_coordinates, validate_coordinates, validate_targets
import run_coordinates


def fixture():
    actions = [dict(card_id=26000000 + owner, cell=183 + owner, delay_slot=1) for owner in (0, 1)]
    plans = [dict(identity=owner, owner=owner, action=actions[owner], created_tick=100,
        due_tick=102) for owner in (0, 1)]
    report = dict(decisions=[dict(tick=100, choices={str(owner): dict(action=actions[owner])
        for owner in (0, 1)})], final_clock=dict(evidence=[dict(reservation=owner,
            owner=owner, tick=102, reason="executed") for owner in (0, 1)]),
        battle_offset=10, final_offset=200, executed=2)
    queues, records = [], []
    for owner in (0, 1):
        context = dict(model_subcell_offset=[0.25, -0.125] if owner else None)
        horizontal, vertical = expected_coordinates(plans[owner], context)
        queues.append(dict(reservation=owner, hand_action=dict(owner=owner, hand_index=owner,
            x=horizontal, y=vertical), coordinate_context=context,
            receipt=dict(owner=owner, handIndex=owner, cardId=actions[owner]["card_id"],
                queuedAtTick=101, executeTick=102), request=dict(execute_tick=102),
            log_start=20 + owner * 10, log_end=30 + owner * 10))
        records.append(dict(function="ct86_constructor", object=200 + owner, vcpu=0,
            file_offset=21 + owner * 10, end_offset=25 + owner * 10))
    for index, owner in enumerate((1, 0)):
        start = 50 + index * 30
        action = queues[owner]["hand_action"]
        records.extend([
            dict(function="ct86_enter", object=200 + owner, vcpu=0,
                file_offset=start, end_offset=start + 5),
            dict(function="position_enter", object=999, vcpu=0, pc=0x100000000 + 0xCFCF50,
                return_address=0x100000000 + 0xD30CCC, argument=action["x"], coordinate_y=action["y"],
                file_offset=start + 10, end_offset=start + 15),
            dict(function="ct86_return", object=0, vcpu=0,
                file_offset=start + 20, end_offset=start + 25)])
    measurements = dict(coordinate_schema="FairFinalCommandCoordinates/v1",
        reservation_schema="FairFinalReservationIntervals/v1", training_admissible=False,
        gates_closed=False, reservation_records=[dict(tick_before=101, tick_after=102,
            native_tick_before=101, due=plans, queues=queues,
            outcomes={"0": "executed", "1": "executed"}, log_start=20, log_end=120)])
    return dict(schema="FairFinalCoordinateEntrySummary/v1", records=records), report, measurements


def test_reverse_order_matches_loaded_fields_to_each_reservation():
    result = validate_coordinates(*fixture())
    assert result["paired_reservations"] == 2
    assert result["owners"] == {"0": 1, "1": 1}
    assert result["placement_offset_reservations"] == 1
    assert result["executor_loaded_coordinates_proven"] is True
    assert result["internal_card_fields_proven"] is False
    assert result["training_admissible"] is result["gates_closed"] is False


@pytest.mark.parametrize("owner,cell,offset,expected", [
    (0, 0, None, (500, 500)), (1, 0, None, (17500, 31500)),
    (0, 575, None, (17500, 31500)), (1, 575, None, (500, 500)),
    (0, 0, [-1, -1], (0, 0)), (1, 0, [-1, -1], (17999, 31999)),
    (0, 0, [0.0005, 0.0015], (500, 502)),
    (1, 0, [0.0005, 0.0015], (17500, 31498)),
])
def test_rotation_offsets_rounding_and_clipping(owner, cell, offset, expected):
    assert expected_coordinates(dict(owner=owner, action=dict(cell=cell)),
        dict(model_subcell_offset=offset)) == expected


@pytest.mark.parametrize("change", ["x", "y", "missing_x2", "wide_x2", "float_x2",
    "queued_x", "queued_y", "offset", "missing_context", "wrong_schema", "legacy_summary",
    "missing_helper", "extra_helper", "wrong_caller", "wrong_cpu", "missing_return", "return_before_helper"])
def test_corrupt_or_missing_coordinate_evidence_rejected(change):
    summary, report, measurements = fixture()
    records, queue = summary["records"], measurements["reservation_records"][0]["queues"][1]
    helper = records[3]
    if change == "x":
        helper["argument"] += 1
    elif change == "y":
        helper["coordinate_y"] += 1
    elif change == "missing_x2":
        helper.pop("coordinate_y")
    elif change == "wide_x2":
        helper["coordinate_y"] += 2**32
    elif change == "float_x2":
        helper["coordinate_y"] = float(helper["coordinate_y"])
    elif change in ("queued_x", "queued_y"):
        queue["hand_action"][change[-1]] += 1
    elif change == "offset":
        queue["coordinate_context"]["model_subcell_offset"][0] += 0.1
    elif change == "missing_context":
        queue["coordinate_context"] = {}
    elif change == "wrong_schema":
        measurements["coordinate_schema"] = "v0"
    elif change == "legacy_summary":
        summary["schema"] = "FairFinalDispatchEntrySummary/v1"
    elif change == "missing_helper":
        records.pop(3)
    elif change == "extra_helper":
        records.insert(4, deepcopy(helper))
    elif change == "wrong_caller":
        helper["return_address"] += 4
    elif change == "wrong_cpu":
        helper["vcpu"] = 1
    elif change == "missing_return":
        records.pop(4)
    else:
        records[3], records[4] = records[4], records[3]
    with pytest.raises(ValueError):
        validate_coordinates(summary, report, measurements)


@pytest.mark.parametrize("owner,cell,offset", [(True, 0, None), (0, True, None),
    (2, 0, None), (0, -1, None), (0, 576, None), (0, 0, [float("nan"), 0]),
    (0, 0, [float("inf"), 0]), (0, 0, [True, 0]), (0, 0, [1])])
def test_malformed_conversion_rejected(owner, cell, offset):
    with pytest.raises(ValueError):
        expected_coordinates(dict(owner=owner, action=dict(cell=cell)), dict(model_subcell_offset=offset))


def test_generated_v5_registers_and_python_sources():
    generated = run_coordinates.sources()
    parent = run_coordinates.run_mapping.sources()
    assert set(generated) == set(parent)
    assert {name for name in generated if generated[name] != parent[name]} == {
        "plugin_probe.py", "plugin_protocol.py", "random_entries.c"}
    assert b'"x8", "x2"' in generated["random_entries.c"]
    assert b"FairFinalPluginEntries/v5" in generated["random_entries.c"]
    for name, content in generated.items():
        if name.endswith(".py"):
            compile(content, name, "exec")


def test_raw_v5_parser_preserves_separate_x1_x2_and_target(tmp_path):
    import types

    protocol = types.ModuleType("coordinate_test_protocol")
    protocol.__file__ = str(ROOT.parent / "fair-final-plugin-runtime-v1/plugin_protocol.py")
    exec(compile(run_coordinates.sources()["plugin_protocol.py"], protocol.__file__, "exec"), protocol.__dict__)
    base = 0x4002B2C00000
    values = [base + 0xCFCF50, base + 0xCFCF4C, 0x1234, 3500, base + 0xD30CCC, 0x5678, 10500]
    entry = "E\t0\t0\t" + "\t".join(f"{value:016x}" for value in values) + "\n"
    content = "FairFinalPluginEntries/v5\n" + f"ARM\t{base:016x}\n" + entry + "END\t1\n"
    path = tmp_path / "qemu.log"
    path.write_text(content)
    records = list(protocol.parse(path, base=base, center=base))
    assert len(records) == 1
    assert records[0]["argument"] == 3500
    assert records[0]["coordinate_y"] == 10500
    assert records[0]["target"] == 0x5678
    assert records[0]["sampled_pc"] == base + 0xCFCF4C
    for bad in (content.replace("/v5", "/v4"), content.replace(f"\t{10500:016x}", ""),
            content.replace(f"\t{10500:016x}", "\txyz")):
        path.write_text(bad)
        with pytest.raises(ValueError):
            list(protocol.parse(path, base=base, center=base))


def test_all_model_cells_match_frozen_runtime_conversion():
    import subprocess

    source = '''
import sys
from pathlib import Path
root = Path(sys.argv[1])
project = root.parent / "fair-final-history-v2/project"
sys.path[:0] = [str(root), str(project)]
from native_runner import battle_env
from native_runner.battle_env import BattleEnvV1
from native_runner.contracts import ActionV1
from native_runner.training.event_action_space import event_cell_to_native
from coordinate_audit import expected_coordinates
assert Path(battle_env.__file__).resolve() == project / "native_runner/battle_env.py"
environment = BattleEnvV1.__new__(BattleEnvV1)
for owner in (0, 1):
    for cell in range(576):
        for offset in (None, [0.25, -0.125], [-0.5, 0.5]):
            sign = 1 if owner == 0 else -1
            grid = event_cell_to_native(cell, owner=owner, board_width=18)
            action = ActionV1.play(owner, 0, grid, subcell_offset=(
                None if offset is None else tuple(value * sign for value in offset)))
            assert expected_coordinates(dict(owner=owner, action=dict(cell=cell)),
                dict(model_subcell_offset=offset)) == environment._world_target(action)
'''
    subprocess.run([sys.executable, "-c", source, str(ROOT)], check=True)


def test_actual_coordinate_load_instruction_bytes():
    library = ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not library.exists():
        pytest.skip("private fixed native library not present")
    result = validate_targets(library)
    assert len(result["coordinate_instruction_bytes"]) == 8


def test_legacy_native_trace_does_not_prove_coordinates():
    archive = ROOT.parent / "fair-final-reservation-map-v1/native-plugin-1792912707427676000/archive/plugin"
    if not archive.exists():
        pytest.skip("private legacy native archive not present")
    import json

    read = lambda name: json.loads((archive / name).read_text())
    with pytest.raises(ValueError, match="schema"):
        validate_coordinates(read("trace-summary.json"), read("report.json"), read("measurements.json"))
