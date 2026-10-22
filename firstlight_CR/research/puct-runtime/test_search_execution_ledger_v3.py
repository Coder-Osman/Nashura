from types import SimpleNamespace

import pytest

from event_policy_rollout import EventPolicyRollout, RootRolloutCapsule, SearchExecutionLedger
from native_runner.contracts import ActionKind, ActionV1


def _ability_receipt(action_id, *, source=123, ability_id="ability-a"):
    return SimpleNamespace(
        owner=0,
        entity_id=source,
        event_type="action_executed",
        data={
            "action_id": action_id,
            "kind": "activate_ability",
            "ability_id": ability_id,
            "fair_ability_activation_exact": True,
        },
    )


@pytest.mark.parametrize(
    ("key", "scheduled_tick"),
    [("ability:2:-1", 100), ("ability:2:4", 104)],
)
def test_ability_execution_receipt_preserves_source_and_delay(key, scheduled_tick):
    ledger = SearchExecutionLedger([], {}, {}, set())
    ledger.commit(0, 100, key, scheduled_tick)
    action = ActionV1(
        owner=0,
        kind=ActionKind.ACTIVATE_ABILITY,
        source_entity=123,
        ability_id="ability-a",
        action_id=f"ability:{scheduled_tick}",
    )

    ledger.submit(0, 100, action)
    ledger.reconcile([_ability_receipt(action.action_id)])

    entry = ledger.entries[0]
    assert entry.status == "executed"
    assert entry.action_key == key
    assert entry.scheduled_tick == scheduled_tick
    assert entry.closed_ref
    assert not ledger.submitted


def test_ability_execution_receipt_rejects_wrong_source():
    ledger = SearchExecutionLedger([], {}, {}, set())
    ledger.commit(0, 100, "ability:2:-1", 100)
    action = ActionV1(
        owner=0,
        kind=ActionKind.ACTIVATE_ABILITY,
        source_entity=123,
        ability_id="ability-a",
        action_id="ability:100",
    )
    ledger.submit(0, 100, action)

    with pytest.raises(ValueError, match="exact search source"):
        ledger.reconcile([_ability_receipt(action.action_id, source=124)])


def test_late_executed_receipt_after_restore_updates_closed_search_entry():
    ledger = SearchExecutionLedger([], {}, {}, set())
    ledger.commit(0, 100, "play:1:3:2", 103)
    action = ActionV1.play(
        0, 1, (3, 4), card_id=7, execute_offset_ticks=1, action_id="play:103"
    )
    ledger.submit(0, 100, action)
    ledger.current_by_owner.pop(0)
    ledger.entries[0].closed_ref = True
    restored = SearchExecutionLedger.from_state(ledger.to_state())

    restored.reconcile([SimpleNamespace(
        owner=0,
        event_type="action_executed",
        data={"action_id": "play:103"},
    )])

    entry = restored.entries[0]
    assert entry.status == "executed"
    assert entry.closed_ref
    assert not restored.submitted
    assert not restored.current_by_owner


def test_dual_ready_search_entries_remain_owner_scoped_and_terminal_closes_plans():
    ledger = SearchExecutionLedger([], {}, {}, set())
    ledger.commit(0, 100, "play:1:3:2", 103)
    ledger.commit(1, 100, "ability:2:-1", 100)

    assert ledger.current_by_owner == {0: 0, 1: 1}
    ledger.close_episode()

    assert not ledger.current_by_owner
    assert [(entry.owner, entry.status, entry.cancel_reason, entry.closed_ref)
            for entry in ledger.entries] == [
                (0, "cancelled", "terminal_before_execution", True),
                (1, "cancelled", "terminal_before_execution", True),
            ]


def test_search_only_root_does_not_copy_recorder_history():
    class UncopyableRow:
        def __deepcopy__(self, memo):
            raise AssertionError("search-only root copied recorder history")

    row = UncopyableRow()
    rollout = object.__new__(EventPolicyRollout)
    rollout.started = True
    rollout.recorder = SimpleNamespace(
        rows={0: [row], 1: []}, _frames={0: [], 1: []},
        _row_masks={0: [], 1: []}, _submitted={}, _pending={0: None, 1: None},
    )
    rollout._search_recorder_capsule = None
    rollout._search_execution_ledger = None
    rollout._search_recorder_lengths = None

    rollout.begin_search_root(search_only=True)
    assert rollout._search_recorder_capsule is None
    assert rollout._search_execution_ledger is not None
    rollout.audit_search_root()
    rollout.recorder.rows[0].append(row)
    with pytest.raises(ValueError, match="modified training recorder rows"):
        rollout.audit_search_root()
    rollout.end_search_root()
    assert rollout._search_execution_ledger is None


def test_root_capsule_audits_before_first_restore_and_at_root_end(monkeypatch):
    capsule = RootRolloutCapsule("root-rollout-capsule.v1", {}, {}, None, None, None, "digest")
    rollout = object.__new__(EventPolicyRollout)
    rollout._search_execution_ledger = SearchExecutionLedger([], {}, {}, set())
    rollout._search_recorder_capsule = None
    rollout._search_recorder_lengths = None
    rollout._root_rollout_capsule = capsule
    rollout._root_rollout_capsule_audited = False
    audited = []
    restored = []
    monkeypatch.setattr(RootRolloutCapsule, "audit", lambda self: audited.append(self))
    monkeypatch.setattr(
        RootRolloutCapsule, "restore_into",
        lambda self, target, **kwargs: restored.append((self, target, kwargs)),
    )

    receipt = SimpleNamespace(state_epoch=9)
    rollout.restore_root_capsule(capsule, receipt)
    rollout.restore_root_capsule(capsule, receipt)
    rollout.end_search_root()

    assert audited == [capsule, capsule]
    assert len(restored) == 2
    assert all(call[2] == {"state_epoch": 9, "verify_digest": False} for call in restored)
