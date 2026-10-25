from collections import Counter


def integer(value):
    return type(value) is int


def committed_plans(report):
    plans = {}
    previous = -1
    for group in report["decisions"]:
        tick = group["tick"]
        if not integer(tick) or tick <= previous:
            raise ValueError("reservation decision chronology differs")
        previous = tick
        for owner, choice in sorted(group["choices"].items(), key=lambda item: int(item[0])):
            action = choice["action"]
            identity = len(plans)
            plans[identity] = dict(identity=identity, owner=int(owner), action=action,
                created_tick=tick, due_tick=tick + (60 if action["card_id"] is None
                    else 2 * action["delay_slot"]))
    evidence = {}
    for row in report["final_clock"]["evidence"]:
        identity = row["reservation"]
        if identity not in plans or identity in evidence or row["owner"] != plans[identity]["owner"]:
            raise ValueError("reservation final settlement identity differs")
        evidence[identity] = row
    if set(evidence) != set(plans):
        raise ValueError("reservation final settlement incomplete")
    return plans, evidence


def validate_mapping(summary, report, measurements):
    if (measurements.get("reservation_schema") != "FairFinalReservationIntervals/v1"
            or measurements.get("training_admissible") is not False
            or measurements.get("gates_closed") is not False):
        raise ValueError("reservation interval schema or scope differs")
    plans, evidence = committed_plans(report)
    records = summary["records"]
    mapped, constructor_offsets, execution_offsets = set(), set(), set()
    previous_tick, previous_end = -1, report["battle_offset"]
    owners, simultaneous, objects = Counter(), 0, []
    for boundary in measurements["reservation_records"]:
        tick, start, end = (boundary[name] for name in ("tick_before", "log_start", "log_end"))
        if (not all(integer(value) for value in (tick, start, end, boundary["tick_after"],
                boundary["native_tick_before"])) or tick <= previous_tick
                or boundary["tick_after"] != tick + 1
                or not previous_end <= start <= end <= report["final_offset"]):
            raise ValueError("reservation interval chronology differs")
        previous_tick, previous_end = tick, end
        if any(row["file_offset"] < boundary_offset < row["end_offset"]
                for row in records for boundary_offset in (start, end)):
            raise ValueError("reservation tick interval splits native log record")
        due = {row["identity"]: row for row in boundary["due"]}
        if len(due) != len(boundary["due"]) or any(
                plans.get(identity) != plan or plan["due_tick"] != tick + 1
                or plan["action"]["card_id"] is None for identity, plan in due.items()):
            raise ValueError("reservation due plan differs from committed action")
        if boundary["outcomes"] != {str(identity): evidence[identity]["reason"] for identity in due}:
            raise ValueError("reservation outcome differs from final evidence")
        queues = boundary["queues"]
        if {queue["reservation"] for queue in queues} != set(due) or len(queues) != len(due):
            raise ValueError("reservation queue coverage incomplete")
        interval = [row for row in records if start <= row["file_offset"] < end]
        local_objects, queue_end = {}, start
        for queue in queues:
            identity = queue["reservation"]
            plan, receipt, action = due[identity], queue["receipt"], queue["hand_action"]
            before, after = queue["log_start"], queue["log_end"]
            if (not integer(before) or not integer(after) or not queue_end <= before < after <= end
                    or identity in mapped or evidence[identity]["tick"] != tick + 1
                    or evidence[identity]["reason"] != "executed"):
                raise ValueError("reservation queue interval or settlement differs")
            queue_end = after
            if any(row["file_offset"] < boundary_offset < row["end_offset"]
                    for row in interval for boundary_offset in (before, after)):
                raise ValueError("reservation queue interval splits native log record")
            if (action["owner"] != plan["owner"] or receipt["owner"] != plan["owner"]
                    or receipt["handIndex"] != action["hand_index"]
                    or receipt["cardId"] != plan["action"]["card_id"]
                    or receipt["queuedAtTick"] != boundary["native_tick_before"]
                    or receipt["executeTick"] != boundary["native_tick_before"] + 1
                    or queue["request"] != {"execute_tick": receipt["executeTick"]}):
                raise ValueError("reservation native queue receipt differs")
            entries = [row for row in interval if before <= row["file_offset"]
                and row["end_offset"] <= after and row["function"] == "ct86_constructor"]
            if len(entries) != 1 or entries[0]["object"] <= 0:
                raise ValueError("reservation queue needs exactly one native constructor")
            constructor = entries[0]
            if constructor["object"] in local_objects:
                raise ValueError("simultaneous reservations share one native object")
            local_objects[constructor["object"]] = (identity, constructor)
            constructor_offsets.add(constructor["file_offset"])
        calls = [row for row in interval if row["function"] == "ct86_enter"]
        if len(calls) != len(local_objects):
            raise ValueError("reservation execution count differs")
        called = set()
        for row in calls:
            if (row["object"] not in local_objects or row["object"] in called
                    or row["file_offset"] < queue_end):
                raise ValueError("reservation executor lacks unique queued object")
            identity, constructor = local_objects[row["object"]]
            if row["vcpu"] != constructor["vcpu"]:
                raise ValueError("reservation constructor executor vCPU differs")
            called.add(row["object"])
            mapped.add(identity)
            owners[plans[identity]["owner"]] += 1
            objects.append(row["object"])
            execution_offsets.add(row["file_offset"])
        simultaneous += len(calls) == 2
    expected = {identity for identity, row in evidence.items() if row["reason"] == "executed"}
    constructors = {row["file_offset"] for row in records if row["function"] == "ct86_constructor"}
    executions = {row["file_offset"] for row in records if row["function"] == "ct86_enter"}
    if (not mapped or mapped != expected or constructor_offsets != constructors
            or execution_offsets != executions or len(mapped) != report["executed"]):
        raise ValueError("reservation native trajectory coverage incomplete")
    return dict(schema="FairFinalReservationMappingEvidence/v1", status="verified_limited_scope",
        mapped_reservations=len(mapped), owners={str(owner): count for owner, count in sorted(owners.items())},
        simultaneous_execution_ticks=simultaneous, distinct_native_objects=len(set(objects)),
        object_reuse_across_ticks_supported=True, per_reservation_object_mapping_proven=True,
        legal_rejection_coverage_proven=False, training_admissible=False, gates_closed=False)
