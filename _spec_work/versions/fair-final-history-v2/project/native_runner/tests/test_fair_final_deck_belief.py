from dataclasses import replace
from itertools import permutations
import json
from types import SimpleNamespace

import pytest

from native_runner.contracts import EventV1
from native_runner.training.fair_final.deck_belief import (DeckFrequency, FrozenDeckPrior,
    IncompatiblePublicCycle, ORDER_COUNT, PublicCycleBelief, PublicDeckRecord, PublicPlay)
from native_runner.training.fair_search.belief import public_draw_compatible


DECK = tuple(range(1, 9))


def prior(*, entries=None, source="a" * 64):
    return FrozenDeckPrior("fixed-experiment", source, entries or (DeckFrequency(DECK, 1),))


def belief(*, frozen=None, owner=1):
    return PublicCycleBelief(frozen or prior(), owner=owner, opening_tick=90)


def advance(state, cards, *, tick=None):
    plays = tuple(PublicPlay(state.tick + 2 * (index + 1), card) for index, card in enumerate(cards))
    return state.advance(plays, from_tick=state.tick,
        to_tick=tick if tick is not None else plays[-1].tick, expected_evidence_sha=state.evidence_sha)


def record(*, game="b" * 64, owner=0, cards=DECK):
    return PublicDeckRecord(game, owner, tuple(PublicPlay(100 + index * 2, card)
        for index, card in enumerate(cards)))


@pytest.mark.parametrize("cards", [(1,) * 8, tuple(range(8)), tuple(range(1, 8)),
    tuple(reversed(DECK)), (True, *range(2, 9)), (1., *range(2, 9)), (*range(1, 8), 2**31)])
def test_deck_rejects_invalid_or_noncanonical_identity(cards):
    with pytest.raises(ValueError):
        DeckFrequency(cards, 1)


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_prior_rejects_invalid_frequency(count):
    with pytest.raises(ValueError):
        DeckFrequency(DECK, count)


def test_prior_roundtrip_and_identity_binds_frequency_source_and_rules():
    frozen = prior()
    payload = json.loads(json.dumps(frozen.to_payload()))
    assert FrozenDeckPrior.from_payload(payload) == frozen
    assert prior(source="b" * 64).sha256 != frozen.sha256
    assert prior(entries=(DeckFrequency(DECK, 2),)).sha256 != frozen.sha256
    for key, value in (("real_seed", 17), ("opponent_hand", list(DECK[:4])),
            ("rules", "champion-short-cycle"), ("schema", "other")):
        with pytest.raises(ValueError):
            FrozenDeckPrior.from_payload({**payload, key: value})
    payload["entries"][0]["private_seed"] = 17
    with pytest.raises(ValueError):
        FrozenDeckPrior.from_payload(payload)


def test_prior_support_bound_and_duplicate_entries_are_rejected():
    with pytest.raises(ValueError):
        FrozenDeckPrior("fixed-experiment", "a" * 64, ())
    with pytest.raises(ValueError):
        prior(entries=(DeckFrequency(DECK, 1), DeckFrequency(DECK, 2)))
    entries = tuple(DeckFrequency(tuple(range(1 + index * 8, 9 + index * 8)), 1) for index in range(25))
    with pytest.raises(ValueError):
        prior(entries=entries)


def test_public_prior_deduplicates_game_and_seat_without_using_private_decks():
    first = record()
    second = record(owner=1)
    third = record(game="c" * 64, cards=(*range(1, 8), 9))
    frozen = FrozenDeckPrior.from_public_records([first, second, first, third], source_sha="d" * 64)
    assert frozen.kind == "empirical-complete-public"
    assert [entry.count for entry in frozen.entries] == [2, 1]
    assert frozen == FrozenDeckPrior.from_public_records([third, second, first], source_sha="d" * 64)
    with pytest.raises(ValueError, match="conflicting"):
        FrozenDeckPrior.from_public_records([first, replace(first, plays=third.plays)], source_sha="d" * 64)
    with pytest.raises(ValueError):
        FrozenDeckPrior.from_public_records([SimpleNamespace(deck=DECK)], source_sha="d" * 64)
    with pytest.raises(ValueError):
        record(cards=DECK[:7])
    impossible = record(cards=(1, 1, *range(2, 9)))
    with pytest.raises(IncompatiblePublicCycle):
        FrozenDeckPrior.from_public_records([impossible], source_sha="d" * 64)


@pytest.mark.parametrize("data", [{"private_to": 0}, {"private_to": 1}, {"oracle_only": True}])
def test_private_events_are_rejected(data):
    event = EventV1(100, "action_executed", owner=1, card_id=1, data=data)
    with pytest.raises(ValueError, match="public"):
        PublicPlay.from_event(event, owner=1)


def test_event_projection_drops_seed_plan_and_action_identity():
    first = EventV1(100, "action_executed", owner=1, card_id=1,
        data={"seed": 42, "requested_tick": 90, "action_id": "secret"})
    assert PublicPlay.from_event(first, owner=1) == PublicPlay.from_event(
        replace(first, data={"seed": 999}), owner=1) == PublicPlay(100, 1)
    for event in (replace(first, owner=0), replace(first, event_type="action_scheduled")):
        with pytest.raises(ValueError):
            PublicPlay.from_event(event, owner=1)


def test_empirical_probability_is_count_times_compatible_permutation_count():
    other = (*range(1, 8), 9)
    state = belief(frozen=prior(entries=(DeckFrequency(DECK, 3), DeckFrequency(other, 1))))
    assert [(row["mass_numerator"], row["mass_denominator"]) for row in state.posterior()] == [
        (3 * ORDER_COUNT, 4 * ORDER_COUNT), (ORDER_COUNT, 4 * ORDER_COUNT)]
    advance(state, (8,))
    rows = state.posterior()
    assert len(rows) == 1 and rows[0]["deck"] == DECK
    assert rows[0]["compatible_orders"] == ORDER_COUNT // 2
    assert rows[0]["mass_numerator"] == rows[0]["mass_denominator"]


def test_sampling_is_reproducible_single_completion_and_prior_weighted():
    other = (*range(1, 8), 9)
    state = belief(frozen=prior(entries=(DeckFrequency(DECK, 3), DeckFrequency(other, 1))))
    before = state.support_digest()
    selected = [state.sample(seed=seed).deck for seed in range(2000)]
    assert .71 < selected.count(DECK) / len(selected) < .79
    assert state.sample(seed=42) == state.sample(seed=42)
    assert state.sample(seed=42).manifest["training_admissible"] is False
    assert before == state.support_digest()
    for seed in (-1, True, 1.5):
        with pytest.raises(ValueError):
            state.sample(seed=seed)


def test_incremental_and_cold_support_and_sampling_are_identical():
    cards = (*DECK, *DECK, 1, 2, 3)
    cold, incremental = belief(), belief()
    advance(cold, cards)
    for card in cards:
        advance(incremental, (card,))
    assert cold.evidence_sha == incremental.evidence_sha
    assert cold.support_digest() == incremental.support_digest()
    for seed in range(20):
        assert cold.sample(seed=seed) == incremental.sample(seed=seed)
        assert public_draw_compatible(cold.sample(seed=seed).initial_draw, cards)


def test_exhaustive_reference_support_and_hand_cycle_match():
    cards = (1, 2, 3, 4, 5, 1)
    state = belief()
    advance(state, cards)
    expected = [draw for draw in permutations(DECK) if public_draw_compatible(draw, cards)]
    _, initial, current = state._support[0]
    assert initial.tolist() == [list(draw) for draw in expected]
    for index in range(0, len(expected), max(1, len(expected) // 17)):
        draw = list(expected[index])
        for card in cards:
            slot = draw[:4].index(card)
            draw[slot] = draw[4]
            draw[4:] = [*draw[5:], card]
        assert current[index].tolist() == draw


def test_delta_cost_is_new_events_only_and_memory_does_not_grow_with_history():
    state = belief()
    initial_bytes = state.memory_bytes
    advance(state, DECK * 5)
    before = state.transition_work
    candidates = sum(row["compatible_orders"] for row in state.posterior())
    advance(state, (1,))
    assert state.transition_work - before == candidates
    assert state.memory_bytes <= initial_bytes
    result = advance(state, (), tick=state.tick + 20)
    assert result["added_plays"] == result["order_checks"] == 0


def test_incompatible_delta_rolls_back_every_field_and_retains_valid_support():
    state = belief()
    snapshot = state.support_digest(), state.evidence_sha, state.tick, state.play_count, state.transition_work
    with pytest.raises(IncompatiblePublicCycle):
        advance(state, (1, 1))
    assert snapshot == (state.support_digest(), state.evidence_sha, state.tick, state.play_count, state.transition_work)
    advance(state, (1, 2))
    assert state.play_count == 2


@pytest.mark.parametrize("change", [dict(from_tick=89), dict(to_tick=91),
    dict(expected_evidence_sha="f" * 64), dict(plays=(PublicPlay(90, 1),)),
    dict(plays=(PublicPlay(92, 1), PublicPlay(92, 2))),
    dict(plays=(PublicPlay(94, 1), PublicPlay(92, 2)))])
def test_delta_rejects_stale_or_invalid_prefix_before_mutation(change):
    state = belief()
    before = state.support_digest()
    arguments = dict(plays=(PublicPlay(92, 1),), from_tick=90, to_tick=94, expected_evidence_sha=state.evidence_sha)
    with pytest.raises(ValueError):
        state.advance(**{**arguments, **change})
    assert state.tick == 90 and state.support_digest() == before


def test_unknown_deck_never_falls_back_to_actual_hidden_state():
    state = belief()
    with pytest.raises(IncompatiblePublicCycle):
        advance(state, (99,))
    with pytest.raises(TypeError):
        state.sample(seed=1, actual_hidden_draw=DECK)
    assert state.play_count == 0


def test_public_evidence_and_cache_identity_are_seat_separated():
    first, other = belief(owner=0), belief(owner=1)
    assert first.evidence_sha != other.evidence_sha
    advance(first, (1, 2))
    advance(other, (1, 2))
    assert first.sample(seed=42).manifest != other.sample(seed=42).manifest
    assert first.support_digest() == other.support_digest()
