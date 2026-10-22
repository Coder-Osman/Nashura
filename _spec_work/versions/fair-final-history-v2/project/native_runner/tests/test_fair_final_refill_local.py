from copy import deepcopy
from dataclasses import replace
from itertools import permutations
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_history import history_fixture, opening_fixture
from native_runner.tests.test_fair_final_local_reconditioning import native_observation
from native_runner.tests.test_fair_final_prior_history import following, prior
from native_runner.tests.test_fair_final_refill_belief import completion_fixture, delayed_player
from native_runner.training.fair_final import refill_local as local
from native_runner.training.fair_final.conditioned_plan import HistoryConditionedOpponent
from native_runner.training.fair_final.deck_belief import DrawCompletion
from native_runner.training.fair_final.history_belief import HistoryReplaySession, IncompatibleHistory
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest


def factory():
    return local.IncrementalRefillBeliefFactory(lambda: None, prior=prior(), catalog=None,
        opponent=SimpleNamespace(controller_sha="b"*64, checkpoint_sha="c"*64), sidecar_factory=None)


@pytest.mark.parametrize("holes", [(), (0,), (1,), (2,), (3,), (0, 2), (1, 2, 3), (0, 1, 2, 3)])
@pytest.mark.parametrize("owner", [0, 1])
def test_native_relabeling_preserves_holes_own_slots_and_fifo_positions(holes, owner):
    raw = native_observation()
    raw["players"][1-owner] = dict(delayed_player(holes), owner=1-owner)
    before = deepcopy(raw)
    mapping = dict(zip(HOG_DECK, reversed(HOG_DECK)))
    fields, installed = local.refill_relabel_fields(raw, mapping, owner=owner)
    assert len(fields) == 51
    assert fields[3+12*owner:15+12*owner] == fields[27+12*owner:39+12*owner]
    assert tuple(index for index, card in enumerate(installed[0]) if card is None) == holes
    layout = local.native_draw_layout(raw["players"][1-owner])
    assert installed[1] == tuple(mapping[card] for card in layout.cycle)
    assert raw == before


def test_public_fixed_relabeling_preserves_each_historical_card_and_pending_draw():
    history, completion, player = completion_fixture()
    layout = local.native_draw_layout(player)
    signatures = set()
    for seed in range(40):
        mapping, changed = local.unseen_relabeling(completion, history, seed=seed)
        assert mapping[HOG_DECK[0]] == HOG_DECK[0]
        current = list(changed.initial_draw)
        for event in history.deployments:
            assert event.card_id in current[:4]
            slot = current.index(event.card_id)
            current[slot] = current[4]
            current[4:] = [*current[5:], event.card_id]
        assert tuple(current) == changed.current_draw
        changed_layout = replace(layout,
            hand=tuple(None if card is None else mapping[card] for card in layout.hand),
            cycle=tuple(mapping[card] for card in layout.cycle))
        local.verify_completion_layout(changed_layout, changed, history)
        assert changed.manifest["training_admissible"] is False
        assert local.unseen_relabeling(completion, history, seed=seed) == (mapping, changed)
        signatures.add(changed.initial_draw)
    assert len(signatures) > 1


def test_relabeling_commutes_with_delayed_refills_without_assuming_a_timer_duration():
    history, completion, _ = completion_fixture()
    mapping, _ = local.unseen_relabeling(completion, history, seed=89)
    public = HOG_DECK[0]
    for draw in permutations(HOG_DECK[:4]):
        hand = list(draw)
        queue = list(HOG_DECK[4:])
        relabeled_hand = [mapping[card] for card in hand]
        relabeled_queue = [mapping[card] for card in queue]
        slot = hand.index(public)
        hand[slot] = relabeled_hand[slot] = None
        queue.append(public)
        relabeled_queue.append(public)
        for refill_now in (False, False, True):
            if refill_now:
                hand[slot] = queue.pop(0)
                relabeled_hand[slot] = relabeled_queue.pop(0)
            assert relabeled_hand == [None if card is None else mapping[card] for card in hand]
            assert relabeled_queue == [mapping[card] for card in queue]


@pytest.mark.parametrize("bad_seed", [-1, True, 1.5])
def test_relabeling_rejects_invalid_rng_identity(bad_seed):
    history, completion, _ = completion_fixture()
    with pytest.raises(ValueError):
        local.unseen_relabeling(completion, history, seed=bad_seed)


@pytest.mark.parametrize("bad", ["missing", "duplicate", "players", "owner"])
def test_relabeling_rejects_bad_native_mapping(bad):
    raw = native_observation()
    mapping = dict(zip(HOG_DECK, HOG_DECK))
    owner = 0
    if bad == "missing":
        mapping.pop(HOG_DECK[0])
    elif bad == "duplicate":
        mapping[HOG_DECK[0]] = HOG_DECK[1]
    elif bad == "players":
        raw["players"][1]["owner"] = 0
    else:
        owner = True
    with pytest.raises(ValueError):
        local.refill_relabel_fields(raw, mapping, owner=owner)


def test_all_cards_public_degenerates_to_identity():
    history = history_fixture()
    completion = DrawCompletion(tuple(sorted(HOG_DECK)), HOG_DECK, HOG_DECK, {})
    for card in HOG_DECK:
        history = following(history, card=card)
    mapping, changed = local.unseen_relabeling(completion, history, seed=37)
    assert all(card == replacement for card, replacement in mapping.items())
    assert changed.initial_draw == completion.initial_draw
    assert changed.current_draw == completion.current_draw
    assert changed.manifest["unseen_count"] == 0


def test_incremental_session_rebinds_history_and_advances_only_suffix(monkeypatch):
    history, completion, player = completion_fixture()
    later = following(history, card=HOG_DECK[1])
    target = factory()
    session = object.__new__(HistoryReplaySession)
    session.closed, session.owner, session.history = False, 0, history
    session.draw_completion, session.conditioning_manifest = completion, {}
    session.opponent = HistoryConditionedOpponent(target.opponent, history)
    raw = native_observation()
    raw["players"][1] = player
    session.rollout = SimpleNamespace(runtime=SimpleNamespace(environment=SimpleNamespace(
        native=SimpleNamespace(observe=lambda: raw))))
    bound = local.IncrementalRefillSession(session, history, target.opponent)
    observed = []
    def sync(current, received, information, **kwargs):
        observed.append(current.opponent.history)
        current.history = received
        return "updated"
    verified = []
    monkeypatch.setattr(HistoryReplaySession, "sync", sync)
    monkeypatch.setattr(local, "verify_completion_layout", lambda layout, draw, received: verified.append((draw, received)))
    assert bound.sync(later, SimpleNamespace()) == "updated"
    assert observed == [later]
    expected = local.advance_completion(completion, later.deployments[1:], owner=1)
    assert bound.draw_completion == expected and verified == [(expected, later)]
    assert bound.draw_history == later
    assert bound.conditioning_manifest["history_sha"] == later.sha256
    assert bound.conditioning_manifest["root_tick"] == later.root_tick
    assert bound.conditioning_manifest["completion_manifest"] == expected.manifest
    opponent = bound.opponent
    opponent.profile.update(calls=7, minimum_log_mass=-2., maximum_prefixes=17)
    expected_manifest = deepcopy(bound.conditioning_manifest)
    expected_manifest["hidden_plan_conditioning"] = dict(opponent.profile)
    assert bound.sync(later, SimpleNamespace()) == "updated"
    assert bound.draw_completion == expected
    assert bound.opponent is opponent
    assert bound.conditioning_manifest == expected_manifest


def test_incremental_failure_restores_wrapper_and_completion(monkeypatch):
    history, completion, _ = completion_fixture()
    target = factory()
    session = object.__new__(HistoryReplaySession)
    session.closed, session.owner, session.history = False, 0, history
    session.draw_completion = completion
    session.opponent = HistoryConditionedOpponent(target.opponent, history)
    original = session.opponent
    bound = local.IncrementalRefillSession(session, history, target.opponent)
    def reject(*args, **kwargs):
        raise IncompatibleHistory("rebuild required")
    monkeypatch.setattr(HistoryReplaySession, "sync", reject)
    with pytest.raises(IncompatibleHistory):
        bound.sync(following(history), SimpleNamespace())
    assert bound.opponent is original and bound.draw_history == history
    assert bound.draw_completion == completion


def test_incremental_completion_refreshes_commitment_without_nesting_history():
    history, completion, _ = completion_fixture()
    later = following(history, card=HOG_DECK[1])
    changed = local.advance_completion(completion, later.deployments[1:], owner=1)
    assert changed.manifest["completion_commitment"] == semantic_digest([
        changed.deck, changed.initial_draw, changed.current_draw])
    assert changed.manifest["completion_commitment"] != semantic_digest([
        completion.deck, completion.initial_draw, completion.current_draw])
    assert completion.manifest == changed.manifest["origin_manifest"]
    subsequent = following(later, card=HOG_DECK[2])
    again = local.advance_completion(changed, subsequent.deployments[2:], owner=1)
    assert again.manifest["origin_manifest"] == completion.manifest
    assert local.advance_completion(again, (), owner=1) is again


def test_incremental_rejects_stale_or_foreign_prefix_before_mutation():
    history, completion, _ = completion_fixture()
    session = object.__new__(HistoryReplaySession)
    session.closed, session.owner, session.history, session.draw_completion = False, 0, history, completion
    target = factory()
    bound = local.IncrementalRefillSession(session, history, target.opponent)
    for bad in (history_fixture(), replace(history, root_fair_sha="f"*64), history.to_payload()):
        with pytest.raises(ValueError):
            bound.sync(bad, SimpleNamespace())


def test_factory_and_provider_version_are_independent_and_validate_before_native_access():
    target = factory()
    history = history_fixture()
    provider = local.RefillLocalSearchProvider(target, history=history, anchor_seed=39)
    assert provider.conditioner.sampler_sha != target.sampler_sha != provider.sampler_sha
    provider.close()
    provider.close()
    _, information = opening_fixture()
    with pytest.raises(ValueError):
        provider.build(information, seed=1)
    target.close()
