from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_history import history_fixture
from native_runner.tests.test_fair_final_local_reconditioning import native_observation
from native_runner.tests.test_fair_final_prior_history import following, prior
from native_runner.training.fair_final.deck_belief import DrawCompletion
from native_runner.training.fair_final.history import PublicDeployment
from native_runner.training.fair_final.history_belief import HistoryBeliefFactory
from native_runner.training.fair_final.refill_belief import (
    RefillPriorHistoryBeliefFactory, native_draw_layout, verify_completion_layout)
from native_runner.training.fair_search.config import HOG_DECK


def delayed_player(holes):
    player = native_observation()["players"][1]
    missing = [card for card in player["hand"] if card["handIndex"] in holes]
    player["hand"] = [card for card in player["hand"] if card["handIndex"] not in holes]
    player["cycle"] = [dict(cardId=card["cardId"], deckSlot=card["deckSlot"], cycleIndex=index)
        for index, card in enumerate(missing+player["cycle"])]
    return player


@pytest.mark.parametrize("holes", [(), (0,), (1,), (2,), (3,), (0, 2), (1, 2, 3), (0, 1, 2, 3)])
def test_layout_preserves_holes_fifo_and_native_slot_encoding(holes):
    player = delayed_player(holes)
    before = deepcopy(player)
    layout = native_draw_layout(player)
    assert layout.holes == holes
    assert len(layout.native_fields) == 12
    assert tuple(slot for slot in layout.native_fields if slot >= 0) == (
        *(index for index in range(4) if index not in holes), *holes, *range(4, 8))
    assert set(layout.logical_hand) == set(HOG_DECK[:4])
    assert layout.logical_cycle == HOG_DECK[4:]
    assert player == before


@pytest.mark.parametrize("case", ["missing_card", "duplicate_hand", "cycle_gap", "slot_identity",
    "duplicate_deck", "boolean_slot", "bad_hand_index"])
def test_layout_rejects_malformed_native_state(case):
    player = delayed_player((1,))
    if case == "missing_card":
        player["cycle"].pop()
    elif case == "duplicate_hand":
        player["hand"][1]["handIndex"] = player["hand"][0]["handIndex"]
    elif case == "cycle_gap":
        player["cycle"][0]["cycleIndex"] = 9
    elif case == "slot_identity":
        player["cycle"][0]["deckSlot"] = 7
    elif case == "duplicate_deck":
        player["deck"][0] = deepcopy(player["deck"][1])
    elif case == "boolean_slot":
        player["hand"][0]["deckSlot"] = True
    elif case == "bad_hand_index":
        player["hand"][0]["handIndex"] = 4
    with pytest.raises(ValueError):
        native_draw_layout(player)


def completion_fixture():
    history = following(history_fixture(), card=HOG_DECK[0])
    current = (HOG_DECK[4], *HOG_DECK[1:4], *HOG_DECK[5:], HOG_DECK[0])
    completion = DrawCompletion(tuple(sorted(HOG_DECK)), HOG_DECK, current, {})
    player = native_observation()["players"][1]
    player["hand"].pop(0)
    player["cycle"].append(dict(cardId=HOG_DECK[0], deckSlot=0, cycleIndex=4))
    return history, completion, player


def test_native_refill_matches_public_completion_without_advancing_tick():
    history, completion, player = completion_fixture()
    layout = native_draw_layout(player)
    verify_completion_layout(layout, completion, history)
    assert layout.hand[0] is None and layout.cycle[0] == HOG_DECK[4]
    assert history.root_tick == 160


@pytest.mark.parametrize("case", ["wrong_fifo", "wrong_logical", "missing_play", "unavailable_play"])
def test_completion_rejects_inconsistent_delayed_draw(case):
    history, completion, player = completion_fixture()
    if case == "wrong_fifo":
        player["cycle"][0]["cycleIndex"], player["cycle"][1]["cycleIndex"] = 1, 0
    elif case == "wrong_logical":
        completion = replace(completion, current_draw=completion.initial_draw)
    elif case == "missing_play":
        history = replace(history, deployments=())
    elif case == "unavailable_play":
        history = replace(history, deployments=(PublicDeployment(102, 1, HOG_DECK[7], 1500, 2500),))
    with pytest.raises(ValueError):
        verify_completion_layout(native_draw_layout(player), completion, history)


@pytest.mark.parametrize("bad_layout", [False, True])
def test_refill_factory_checks_native_history_and_releases_failed_world(monkeypatch, bad_layout):
    history, completion, player = completion_fixture()
    raw = native_observation()
    raw["tick"], raw["players"][1] = history.root_tick, player
    if bad_layout:
        raw["players"][1]["cycle"][0]["cycleIndex"] = 8
    released = []
    native = SimpleNamespace(observe=lambda: raw, close_transport=lambda: released.append("transport"))
    session = SimpleNamespace(rollout=SimpleNamespace(runtime=SimpleNamespace(environment=SimpleNamespace(native=native))),
        draw_completion=completion, conditioning_manifest={}, close=lambda: released.append("session"))
    calls = []
    def reconstructed(factory, received, information, **kwargs):
        calls.append((received, information, kwargs))
        return session
    monkeypatch.setattr(HistoryBeliefFactory, "build", reconstructed)
    factory = RefillPriorHistoryBeliefFactory(lambda: None, prior=prior(), catalog=None,
        opponent=SimpleNamespace(controller_sha="b"*64, checkpoint_sha="c"*64), sidecar_factory=None)
    information = SimpleNamespace(owner=0, tick=history.root_tick, sha256=history.root_fair_sha)
    if bad_layout:
        with pytest.raises(ValueError):
            factory.build(history, information, seed=42)
        assert released == ["session", "transport"]
    else:
        assert factory.build(history, information, seed=42) is session
        assert session.conditioning_manifest["refill_holes"] == [0]
        assert session.conditioning_manifest["native_current_draw"][0] is None
        assert released == []
    assert calls[0][0] is history
    assert calls[0][2]["seed"] == 42


def test_refill_factory_rejects_foreign_information_and_closed_state_before_engine_access():
    accessed = []
    factory = RefillPriorHistoryBeliefFactory(lambda: accessed.append(True), prior=prior(), catalog=None,
        opponent=SimpleNamespace(controller_sha="b"*64, checkpoint_sha="c"*64), sidecar_factory=None)
    history = history_fixture()
    information = SimpleNamespace(owner=1, tick=history.root_tick, sha256=history.root_fair_sha)
    with pytest.raises(ValueError):
        factory.build(history, information, seed=42)
    factory.close()
    information.owner = 0
    with pytest.raises(ValueError):
        factory.build(history, information, seed=42)
    assert accessed == []
