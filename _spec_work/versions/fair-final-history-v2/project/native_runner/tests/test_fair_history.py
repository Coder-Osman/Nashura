from copy import deepcopy
from dataclasses import replace
import random

import pytest

from native_runner.tests.test_fair_search import frame, information
from native_runner.training.fair_search.contracts import FrozenHistory, InformationState, PublicEvent, semantic_digest


@pytest.mark.parametrize("length", [0, 1, 31, 32, 33, 64, 65, 1000])
def test_history_matches_sequence_reference(length):
    reference = tuple(str(index) for index in range(length))
    history = FrozenHistory.freeze(reference, "own")
    assert tuple(history) == reference
    assert history == reference
    assert len(history) == length
    for index in range(-length, length):
        assert history[index] == reference[index]
    rng = random.Random(length)
    for _ in range(200):
        bounds = [rng.randint(-length - 5, length + 5) for _ in range(2)]
        selection = slice(*bounds, rng.choice([None, 1, 2, -1, -3]))
        assert history[selection] == reference[selection]
    for index in (-length - 1, length):
        with pytest.raises(IndexError):
            history[index]


def test_history_shares_prefix_and_has_construction_independent_digest():
    root = FrozenHistory.freeze(tuple(str(index) for index in range(1000)), "own")
    branch = root.append("branch")
    assert branch.extends(root)
    assert not root.extends(branch)
    assert not branch.extends(root.append("different"))
    assert root == FrozenHistory.freeze(tuple(root), "own")
    assert branch.digest == FrozenHistory.freeze((*root, "branch"), "own").digest
    assert branch._parent is root._parent
    assert deepcopy(root) is root
    assert FrozenHistory.freeze(root, "own") is root
    assert len(root) == 1000
    with pytest.raises(AttributeError):
        root.kind = "public"


def test_history_rejects_invalid_kinds_and_records():
    with pytest.raises(ValueError):
        FrozenHistory("private")
    with pytest.raises(ValueError):
        FrozenHistory.freeze(FrozenHistory("public"), "own")
    with pytest.raises(ValueError):
        FrozenHistory("own").append({"opponent_hand": []})
    with pytest.raises(ValueError):
        FrozenHistory("public").append("event")


def test_information_digest_and_prefix_checks_do_not_traverse_history(information, monkeypatch):
    history = FrozenHistory.freeze(tuple(str(index) for index in range(2000)), "own")
    public = FrozenHistory.freeze(tuple(PublicEvent(0, 99, 1, 500, 500) for _ in range(2000)), "public")
    source = replace(information, own_history=history, public_history=public)
    expected = source.sha256
    restored = InformationState.from_payload(source.to_payload())
    assert restored.sha256 == expected

    def unexpected_iteration(self):
        raise AssertionError("information digest must not traverse retained history")

    monkeypatch.setattr(FrozenHistory, "__iter__", unexpected_iteration)
    assert replace(source).own_history is history
    assert source.sha256 == expected
    assert restored.sha256 == expected
    assert history.append("new").extends(history)
    assert semantic_digest(public) == semantic_digest(restored.public_history)
