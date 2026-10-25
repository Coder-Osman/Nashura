import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-reservation-map-v1"))
import run_mapping
from reservation_audit import committed_plans, validate_mapping


OPCODES = {
    0xD30CB8: "610a4529", 0xD30CBC: "24030012", 0xD30CC0: "e00318aa",
    0xD30CC4: "e3031aaa", 0xD30CC8: "a230ff97",
    0xCFCF50: "ff8301d1", 0xCFCF6C: "f603012a", 0xCFCF84: "f703022a",
}


def expected_coordinates(plan, context):
    owner, cell = plan["owner"], plan["action"]["cell"]
    if (type(owner) is not int or owner not in (0, 1) or type(cell) is not int
            or not 0 <= cell < 576 or set(context) != {"model_subcell_offset"}):
        raise ValueError("coordinate action or context differs")
    horizontal, vertical = cell % 18, cell // 18
    if owner == 1:
        horizontal, vertical = 17 - horizontal, 31 - vertical
    coordinates = [horizontal * 1000 + 500, vertical * 1000 + 500]
    offset = context["model_subcell_offset"]
    if offset is not None:
        if (not isinstance(offset, (list, tuple)) or len(offset) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in offset)):
            raise ValueError("coordinate placement offset differs")
        sign = 1 if owner == 0 else -1
        coordinates = [coordinate + round(component * sign * 1000)
            for coordinate, component in zip(coordinates, offset, strict=True)]
    return tuple(min(max(value, 0), limit) for value, limit in zip(
        coordinates, (17999, 31999), strict=True))


def validate_targets(library):
    result = run_mapping.run_dispatch.validate_targets(library)
    import lief

    binary = lief.parse(str(library))
    for offset, expected in OPCODES.items():
        if bytes(binary.get_content_from_virtual_address(offset, 4)).hex() != expected:
            raise ValueError("coordinate load or helper instruction bytes differ")
    result["coordinate_instruction_bytes"] = {hex(offset): value for offset, value in OPCODES.items()}
    return result


def validate_coordinates(summary, report, measurements):
    if (summary.get("schema") != "FairFinalCoordinateEntrySummary/v1"
            or measurements.get("coordinate_schema") != "FairFinalCommandCoordinates/v1"):
        raise ValueError("coordinate evidence schema differs")
    mapping = validate_mapping(summary, report, measurements)
    plans, _ = committed_plans(report)
    paired, owners, offsets, distinct = 0, {"0": 0, "1": 0}, 0, set()
    for boundary in measurements["reservation_records"]:
        queues = boundary["queues"]
        local = {}
        for queue in queues:
            constructors = [row for row in summary["records"]
                if row["function"] == "ct86_constructor"
                and queue["log_start"] <= row["file_offset"] < queue["log_end"]]
            plan = plans[queue["reservation"]]
            expected = expected_coordinates(plan, queue["coordinate_context"])
            action = queue["hand_action"]
            if any(type(action[name]) is not int for name in ("x", "y")) or (action["x"], action["y"]) != expected:
                raise ValueError("queued coordinates differ from independent model-cell conversion")
            local[constructors[0]["object"]] = (plan, expected)
            offsets += queue["coordinate_context"]["model_subcell_offset"] is not None
        active, seen = {}, set()
        records = [row for row in summary["records"]
            if boundary["log_start"] <= row["file_offset"] < boundary["log_end"]]
        for row in records:
            name, cpu = row["function"], row["vcpu"]
            if name == "ct86_enter":
                if cpu in active or row["object"] not in local:
                    raise ValueError("coordinate executor identity differs")
                active[cpu] = row["object"]
            elif name == "position_enter":
                base = row["pc"] - 0xCFCF50
                if row["return_address"] != base + 0xD30CCC:
                    continue
                if cpu not in active or active[cpu] in seen:
                    raise ValueError("coordinate helper lacks unique executor")
                plan, expected = local[active[cpu]]
                if (type(row.get("coordinate_y")) is not int or not 0 <= row["coordinate_y"] < 2**32
                        or row["argument"] != expected[0] or row["coordinate_y"] != expected[1]):
                    raise ValueError("native loaded coordinates differ from queued action")
                seen.add(active[cpu])
                owners[str(plan["owner"])] += 1
                distinct.add(expected)
                paired += 1
            elif name == "ct86_return":
                if cpu not in active or active[cpu] not in seen:
                    raise ValueError("coordinate executor returned without field evidence")
                active.pop(cpu)
        if active or seen != set(local):
            raise ValueError("coordinate native trajectory coverage incomplete")
    if paired != mapping["mapped_reservations"] or paired == 0:
        raise ValueError("coordinate positive control absent")
    return dict(schema="FairFinalCommandCoordinateEvidence/v1", status="verified_limited_scope",
        paired_reservations=paired, owners=owners, distinct_world_coordinates=len(distinct),
        placement_offset_reservations=offsets, model_cell_to_queue_coordinates_proven=True,
        executor_loaded_coordinates_proven=True, internal_card_fields_proven=False,
        complete_legal_mask_equivalence_proven=False, legal_rejection_coverage_proven=False,
        training_admissible=False, gates_closed=False)
