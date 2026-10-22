from copy import deepcopy
import random

import pytest

from native_runner.training.event_set import SnapshotEventSet
from native_runner.tests.test_training_tracking import _tracker


def test_event_set_matches_builtin_set_across_snapshots():
    generator = random.Random(1755)
    actual, expected, snapshots = SnapshotEventSet(), set(), []
    for index in range(4000):
        value = (generator.randrange(300), "spawn", index % 2, None)
        if index % 4:
            actual.add(value)
            expected.add(value)
        else:
            actual.discard(value)
            expected.discard(value)
        if index % 100 == 0:
            snapshots.append((actual.copy(), expected.copy()))
        assert len(actual) == len(expected)
        assert (value in actual) == (value in expected)
    assert actual == expected
    for saved, reference in snapshots:
        assert saved == reference
    actual.clear()
    assert len(actual) == 0
    assert all(saved == reference for saved, reference in snapshots)


def test_event_snapshots_share_root_without_reading_history(monkeypatch):
    actual = SnapshotEventSet((index, "spawn", None) for index in range(10_000))
    root = actual._root

    def unexpected_iteration(_self):
        raise AssertionError("snapshot scanned historical events")

    monkeypatch.setattr(SnapshotEventSet, "__iter__", unexpected_iteration)
    for _ in range(32):
        branch = deepcopy(actual)
        assert branch._root is root
        branch.add((10_001, "branch", None))
        assert len(branch) == 10_001
        assert (10_001, "branch", None) not in actual
        branch.clear()
        assert len(actual) == 10_000


def test_event_hash_collisions_are_not_duplicate_identities():
    assert hash((-1,)) == hash((-2,))
    actual = SnapshotEventSet(((-1,), (-2,)))
    saved = actual.copy()
    actual.discard((-1,))
    assert actual == {(-2,)}
    assert saved == {(-1,), (-2,)}


def test_deep_shared_hash_prefixes(monkeypatch):
    from native_runner.training import event_set
    monkeypatch.setattr(event_set, "hash", lambda value: value[0] << 60, raising=False)
    actual = SnapshotEventSet((index,) for index in range(40))
    saved = actual.copy()
    for index in range(0, 40, 2):
        actual.discard((index,))
    assert actual == {(index,) for index in range(1, 40, 2)}
    assert saved == {(index,) for index in range(40)}


@pytest.mark.parametrize("value", ((True,), (1.0,), ((1,),), ([1],), "spawn"))
def test_mutable_or_non_scalar_event_keys_are_rejected(value):
    with pytest.raises(ValueError, match="immutable scalar tuples"):
        SnapshotEventSet().add(value)


def test_tracker_reset_detaches_a_snapshot():
    tracker = _tracker()
    tracker._seen_events = SnapshotEventSet(((90, "spawn", 0, None),))
    saved = tracker._seen_events.copy()
    tracker.reset(tick=0, initial_elixir={0: 6., 1: 6.})
    assert tracker._seen_events == set()
    assert saved == {(90, "spawn", 0, None)}
