from dataclasses import replace

import pytest
from types import SimpleNamespace

from native_runner.contracts import EventV1, ObservationTier
from native_runner.match_factory import STANDARD_DECK
from native_runner.tests.test_training_tracking import _observation, DECK
from native_runner.training.fair_final.information import FairPublicTracker, build_fair_tensorizer
from native_runner.training.fair_search.contracts import semantic_digest


def tracker(owner=0):
    result = FairPublicTracker(owner=owner, own_deck=DECK,
        card_costs={card: 3. for card in range(101, 130)})
    result.reset(tick=0, initial_elixir={0: 6., 1: 6.})
    return result


def test_opponent_tracker_starts_without_a_hidden_deck_and_learns_only_public_plays():
    result = tracker()
    assert result.decks[1] == () and result.owner_card_states(1) == {}
    result.update(_observation(tick=2, events=(
        EventV1(tick=1, event_type="action_executed", owner=1, card_id=120),
        EventV1(tick=2, event_type="action_executed", owner=1, card_id=121,
            data={"private_to": 1}),)))
    assert result.decks[1] == (120,)
    assert result.revealed_cards(1) == (120,)
    assert result.card_state(1, 120).cycle_distance == 4
    result.update(_observation(tick=3, events=(
        EventV1(tick=3, event_type="action_executed", owner=1, card_id=121),)))
    assert result.card_state(1, 120).cycle_distance == 3
    result.reset(tick=0, initial_elixir={0: 6., 1: 6.})
    assert result.owner_card_states(1) == {} and result.decks[1] == ()


def test_public_tracker_rejects_owner_switch_and_more_than_eight_reveals():
    result = tracker()
    with pytest.raises(ValueError, match="different actor"):
        result.update(SimpleNamespace(owner=1, tier=ObservationTier.FAIR))
    for index, card in enumerate(range(120, 128), start=1):
        result.update(_observation(tick=index, events=(
            EventV1(tick=index, event_type="action_executed", owner=1, card_id=card),)))
    with pytest.raises(ValueError, match="deck capacity"):
        result.update(_observation(tick=9, events=(
            EventV1(tick=9, event_type="action_executed", owner=1, card_id=128),)))


def test_fair_tensorizer_uses_own_observation_without_opponent_configuration():
    from native_runner.tests.test_training_v4_tensorizer import _observation as semantic_observation

    original = semantic_observation(skeleton_count=0)
    own = replace(original.players[0], ability_runtime_states=(), evolution_runtime_states=())
    observation = replace(original, players=(own, replace(original.players[1], revealed_cards=())), entities=(),
        action_mask=replace(original.action_mask, ability_sources=()))
    tensorizer = build_fair_tensorizer(observation)
    assert tensorizer.deck == tuple(STANDARD_DECK)
    assert tensorizer.tracker.decks[1] == ()
    tensorizer.start_episode(observation, initial_elixir={0: 10., 1: 10.})
    first = tensorizer.tensorize(observation)
    other = build_fair_tensorizer(observation)
    other.start_episode(observation, initial_elixir={0: 10., 1: 10.})
    assert semantic_digest(first) == semantic_digest(other.tensorize(observation))
    assert not first.opponent_cards.mask[0, :8].any()
    assert first.opponent_cards.mask[0, 8]
    private = SimpleNamespace(tier=ObservationTier.FAIR, owner=0,
        players=(own, replace(observation.players[1], hand=(STANDARD_DECK[0],), private_state_visible=True)))
    with pytest.raises(ValueError, match="opponent private state"):
        build_fair_tensorizer(private)
