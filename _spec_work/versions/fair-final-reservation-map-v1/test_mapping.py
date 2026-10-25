from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reservation_audit import validate_mapping


def fixture():
    actions = [dict(card_id=26000000 + owner, cell=[1, 2], delay_slot=1) for owner in (0, 1)]
    plans = [dict(identity=owner, owner=owner, action=actions[owner], created_tick=100,
        due_tick=102) for owner in (0, 1)]
    evidence = [dict(reservation=owner, owner=owner, tick=102, reason="executed") for owner in (0, 1)]
    report = dict(decisions=[dict(tick=100, choices={str(owner): dict(action=actions[owner])
        for owner in (0, 1)})], final_clock=dict(evidence=evidence),
        battle_offset=10, final_offset=100, executed=2)
    queues = [dict(reservation=owner, hand_action=dict(owner=owner, hand_index=owner, x=1, y=2),
        receipt=dict(owner=owner, handIndex=owner, cardId=actions[owner]["card_id"],
            queuedAtTick=101, executeTick=102), request=dict(execute_tick=102),
        log_start=20 + owner * 10, log_end=30 + owner * 10) for owner in (0, 1)]
    records = [dict(function="ct86_constructor", object=200 + owner, vcpu=0,
        file_offset=21 + owner * 10, end_offset=25 + owner * 10) for owner in (0, 1)]
    records += [dict(function="ct86_enter", object=200 + owner, vcpu=0,
        file_offset=50 + index * 10, end_offset=55 + index * 10)
        for index, owner in enumerate((1, 0))]
    measurement = dict(reservation_schema="FairFinalReservationIntervals/v1",
        training_admissible=False, gates_closed=False, reservation_records=[dict(
            tick_before=101, tick_after=102, native_tick_before=101, due=plans,
            queues=queues, outcomes={"0": "executed", "1": "executed"}, log_start=20, log_end=80)])
    return dict(records=records), report, measurement


def test_same_tick_reverse_execution_uses_object_identity():
    result = validate_mapping(*fixture())
    assert result["mapped_reservations"] == 2
    assert result["owners"] == {"0": 1, "1": 1}
    assert result["simultaneous_execution_ticks"] == 1
    assert result["per_reservation_object_mapping_proven"] is True
    assert result["training_admissible"] is result["gates_closed"] is False


@pytest.mark.parametrize("field,value", [
    ("owner", 1), ("handIndex", 3), ("cardId", 999),
    ("queuedAtTick", 100), ("executeTick", 103),
])
def test_queue_receipt_corruption_rejected(field, value):
    summary, report, measurement = fixture()
    measurement["reservation_records"][0]["queues"][0]["receipt"][field] = value
    with pytest.raises(ValueError):
        validate_mapping(summary, report, measurement)


@pytest.mark.parametrize("change", ["duplicate_object", "unknown_object", "early_execution",
    "extra_constructor", "missing_constructor", "missing_execution", "different_vcpu",
    "missing_queue", "overlap_queue", "wrong_due", "wrong_action", "duplicate_plan",
    "wrong_outcome", "missing_settlement", "duplicate_settlement", "wrong_player",
    "extra_unmapped_execution", "wrong_request", "wrong_schema", "training_claim"])
def test_incomplete_or_misattributed_mapping_rejected(change):
    summary, report, measurement = fixture()
    boundary = measurement["reservation_records"][0]
    if change == "duplicate_object":
        summary["records"][1]["object"] = summary["records"][0]["object"]
    elif change == "unknown_object":
        summary["records"][2]["object"] = 999
    elif change == "early_execution":
        summary["records"][2]["file_offset"] = 35
    elif change == "extra_constructor":
        summary["records"].append(deepcopy(summary["records"][0]))
    elif change == "missing_constructor":
        summary["records"].pop(0)
    elif change == "missing_execution":
        summary["records"].pop()
    elif change == "different_vcpu":
        summary["records"][2]["vcpu"] = 1
    elif change == "missing_queue":
        boundary["queues"].pop()
    elif change == "overlap_queue":
        boundary["queues"][1]["log_start"] = 25
    elif change == "wrong_due":
        boundary["due"][0]["due_tick"] += 1
    elif change == "wrong_action":
        boundary["due"][0]["action"] = dict(card_id=999, cell=[1, 2], delay_slot=1)
    elif change == "duplicate_plan":
        boundary["due"].append(deepcopy(boundary["due"][0]))
    elif change == "wrong_outcome":
        boundary["outcomes"]["0"] = "rejected"
    elif change == "missing_settlement":
        report["final_clock"]["evidence"].pop()
    elif change == "duplicate_settlement":
        report["final_clock"]["evidence"].append(deepcopy(report["final_clock"]["evidence"][0]))
    elif change == "wrong_player":
        report["final_clock"]["evidence"][0]["owner"] = 1
    elif change == "extra_unmapped_execution":
        summary["records"].append(dict(function="ct86_enter", object=300, vcpu=0,
            file_offset=90, end_offset=95))
    elif change == "wrong_request":
        boundary["queues"][0]["request"]["execute_tick"] += 1
    elif change == "wrong_schema":
        measurement["reservation_schema"] = "v0"
    else:
        measurement["training_admissible"] = True
    with pytest.raises(ValueError):
        validate_mapping(summary, report, measurement)


def test_generated_runtime_keeps_parent_plugin_bytes():
    import run_mapping

    generated = run_mapping.sources()
    parent = run_mapping.run_dispatch.sources()
    assert set(generated) == set(parent)
    assert [name for name in generated if generated[name] != parent[name]] == ["plugin_probe.py"]
    for name, content in generated.items():
        if name.endswith(".py"):
            compile(content, name, "exec")


def test_native_objects_can_be_reused_on_a_later_tick():
    summary, report, measurement = fixture()
    group = deepcopy(report["decisions"][0])
    group["tick"] = 102
    report["decisions"].append(group)
    extra = deepcopy(report["final_clock"]["evidence"])
    for row in extra:
        row["reservation"] += 2
        row["tick"] += 2
    report["final_clock"]["evidence"].extend(extra)
    report["executed"] = 4
    report["final_offset"] = 200
    boundary = deepcopy(measurement["reservation_records"][0])
    for name in ("tick_before", "tick_after", "native_tick_before"):
        boundary[name] += 2
    for name in ("log_start", "log_end"):
        boundary[name] += 100
    boundary["outcomes"] = {"2": "executed", "3": "executed"}
    for row in boundary["due"]:
        row["identity"] += 2
        row["created_tick"] += 2
        row["due_tick"] += 2
    for queue in boundary["queues"]:
        queue["reservation"] += 2
        queue["log_start"] += 100
        queue["log_end"] += 100
        queue["request"]["execute_tick"] += 2
        queue["receipt"]["executeTick"] += 2
        queue["receipt"]["queuedAtTick"] += 2
    measurement["reservation_records"].append(boundary)
    extra = deepcopy(summary["records"])
    for row in extra:
        row["file_offset"] += 100
        row["end_offset"] += 100
    summary["records"].extend(extra)
    result = validate_mapping(summary, report, measurement)
    assert result["mapped_reservations"] == 4
    assert result["distinct_native_objects"] == 2


@pytest.mark.parametrize("start,end", [(22, 30), (20, 23), (20, 52)])
def test_log_interval_splitting_record_rejected(start, end):
    summary, report, measurement = fixture()
    queue = measurement["reservation_records"][0]["queues"][0]
    queue["log_start"], queue["log_end"] = start, end
    with pytest.raises(ValueError):
        validate_mapping(summary, report, measurement)
