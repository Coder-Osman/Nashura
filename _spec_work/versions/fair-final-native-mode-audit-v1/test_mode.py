from types import SimpleNamespace

import pytest

from audit_mode import active_mode, verify_predicate, PREDICATE, PREDICATE_START
from verify_mode import validate_census


@pytest.mark.parametrize("world,rules,bit", [(1, 6, 1), (1, 15, 1),
    (1, 0, 0), (1, 7, 0), (3, None, 1), (0, None, 0), (2, None, 0), (4, None, 0)])
def test_active_native_predicate(world, rules, bit):
    result = active_mode(world, rules)
    assert result["predicate_bit0"] == bit
    assert result["queue_branch"] == ("bit0-set" if bit else "bit0-clear-age20")


@pytest.mark.parametrize("world,rules", [(True, None), (-1, None), (2**32, None),
    (1, None), (1, True), (1, -1), (3, 6)])
def test_predicate_rejects_incomplete_and_ambiguous_input(world, rules):
    with pytest.raises(ValueError):
        active_mode(world, rules)


def instructions():
    return [SimpleNamespace(address=PREDICATE_START + 4 * index, mnemonic=mnemonic, op_str=operands)
        for index, (mnemonic, operands) in enumerate(PREDICATE)]


def test_exact_instruction_fingerprint():
    verify_predicate(instructions())


@pytest.mark.parametrize("change", ["address", "mnemonic", "op_str", "missing", "extra"])
def test_predicate_fingerprint_rejects_tampering(change):
    values = instructions()
    if change == "missing":
        values.pop()
    elif change == "extra":
        values.append(values[-1])
    else:
        setattr(values[8], change, 0 if change == "address" else "tampered")
    with pytest.raises(ValueError):
        verify_predicate(values)


def census():
    process = dict(child_pid=123, child_start_ticks="456")
    record = dict(**active_mode(1, 7), **process, readonly=True, world_backlink_matches=True,
        training_admissible=False, gates_closed=False, tick=90, generation=1, stateEpoch=1)
    return record, process


def test_native_census_verification():
    validate_census(*census())


@pytest.mark.parametrize("field,value", [("predicate_bit0", 1), ("predicate_bit0", False),
    ("queue_branch", "bit0-set"), ("readonly", False), ("world_backlink_matches", False),
    ("training_admissible", True), ("gates_closed", True), ("child_pid", 124),
    ("child_start_ticks", "457"), ("tick", True)])
def test_native_census_rejects_tampering(field, value):
    record, process = census()
    record[field] = value
    with pytest.raises(ValueError):
        validate_census(record, process)
