from dataclasses import dataclass

import pytest

from recorder_capsule import RecorderBranchDelta, RootRecorderCapsule, restore_recorder


@dataclass
class Row:
    action_id: str
    status: str


class Recorder:
    def __init__(self):
        self._rows = {0: [Row("a", "pending")], 1: []}
        self._frames = {0: ["frame-a"], 1: []}
        self._row_masks = {0: [("mask-a",)], 1: []}
        pending = {"action_id": "a"}
        self._pending = {0: pending, 1: None}
        self._submitted = {"a": pending}
        self._action_ids = {"a"}
        self._terminal = {0: 0.0, 1: 0.0}

    @property
    def rows(self):
        return self._rows


def test_late_receipt_replaces_prefix_and_keeps_pending_alias():
    recorder = Recorder()
    capsule = RootRecorderCapsule.from_recorder(recorder)
    recorder._rows[0][0] = Row("a", "executed")
    recorder._pending[0]["status"] = "executed"
    delta = RecorderBranchDelta.from_recorder(recorder, capsule)
    assert delta.digest(capsule) == RecorderBranchDelta.from_recorder(recorder, capsule).digest(capsule)
    logical = delta.materialize_logical_history(capsule)
    assert logical["rows"][0][0].status == "executed"
    assert logical["runtime"]["pending"][0] is logical["runtime"]["submitted"]["a"]

    sibling = Recorder()
    restore_recorder(sibling, capsule, delta)
    assert sibling._rows[0][0].status == "executed"
    assert sibling._pending[0] is sibling._submitted["a"]


def test_branch_appends_are_isolated():
    recorder = Recorder()
    capsule = RootRecorderCapsule.from_recorder(recorder)
    recorder._rows[0].append(Row("b", "wait"))
    recorder._frames[0].append("frame-b")
    recorder._row_masks[0].append(("mask-b",))
    delta = RecorderBranchDelta.from_recorder(recorder, capsule)
    other = RecorderBranchDelta.from_recorder(Recorder(), capsule)
    assert len(delta.materialize_logical_history(capsule)["rows"][0]) == 2
    assert len(other.materialize_logical_history(capsule)["rows"][0]) == 1
    assert delta.digest(capsule) != other.digest(capsule)


def test_root_prefix_mutation_fails_closed():
    recorder = Recorder()
    capsule = RootRecorderCapsule.from_recorder(recorder)
    capsule.rows_prefix[0][0].status = "tampered"
    with pytest.raises(ValueError, match="capsule was mutated"):
        capsule.audit()


def test_restore_copies_prefix_and_branch_replacement():
    recorder = Recorder()
    capsule = RootRecorderCapsule.from_recorder(recorder)
    recorder._rows[0][0] = Row("a", "executed")
    delta = RecorderBranchDelta.from_recorder(recorder, capsule)
    restored = Recorder()
    restore_recorder(restored, capsule, delta)
    assert restored._frames[0] is not capsule.frames_prefix[0]
    assert restored._rows[0][0] is not capsule.rows_prefix[0][0]
    restored._rows[0][0].status = "changed"
    assert capsule.rows_prefix[0][0].status == "pending"
    capsule.audit()


def test_branch_prefix_write_does_not_modify_capsule():
    recorder = Recorder()
    capsule = RootRecorderCapsule.from_recorder(recorder)
    delta = RecorderBranchDelta.from_recorder(recorder, capsule)
    restore_recorder(recorder, capsule, delta)
    recorder._rows[0][0].status = "tampered"
    capsule.audit()
    assert capsule.rows_prefix[0][0].status == "pending"
