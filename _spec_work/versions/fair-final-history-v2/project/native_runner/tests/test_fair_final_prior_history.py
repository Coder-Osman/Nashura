from dataclasses import replace
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_history import history_fixture, opening_fixture
from native_runner.training.fair_final import prior_history
from native_runner.training.fair_final.deck_belief import DeckFrequency, FrozenDeckPrior, IncompatiblePublicCycle
from native_runner.training.fair_final.history import OwnDecision, PublicDeployment
from native_runner.training.fair_final.history_belief import HistoryBeliefFactory, IncompatibleHistory
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.prior_history import HistoryCycleCursor, PriorHistoryBeliefFactory
from native_runner.training.fair_search.config import HOG_DECK


def prior():
    return FrozenDeckPrior("fixed-experiment", "a" * 64, (DeckFrequency(tuple(sorted(HOG_DECK)), 1),))


def factory(native_factory=lambda: None, frozen=None):
    return PriorHistoryBeliefFactory(native_factory, prior=frozen or prior(), catalog=None,
        opponent=SimpleNamespace(controller_sha="b" * 64, checkpoint_sha="c" * 64), sidecar_factory=None)


def following(first, *, card=None):
    return replace(first, root_tick=first.root_tick+60, root_fair_sha="d" * 64,
        decisions=(*first.decisions, OwnDecision(first.root_tick, first.root_fair_sha, FullAction())),
        deployments=(*first.deployments, *( () if card is None else
            (PublicDeployment(first.root_tick+2, 1-first.opening.owner, card, 1500, 2500),))))


def test_cursor_extends_only_new_public_executions_and_reuses_empty_root():
    first = history_fixture()
    second, third = following(first, card=HOG_DECK[0]), None
    third = following(second, card=HOG_DECK[1])
    cursor = HistoryCycleCursor(prior(), opening=first.opening)
    cursor.sync(second)
    work = cursor.cycle.transition_work
    assert cursor.sync(second)["order_checks"] == 0
    assert cursor.cycle.transition_work == work
    assert cursor.sync(third)["added_plays"] == 1
    cold = HistoryCycleCursor(prior(), opening=first.opening)
    assert cursor.sample(third, seed=42) == cold.sample(third, seed=42)
    assert cursor.cycle.play_count == 2


def test_cursor_rejects_stale_and_foreign_history_without_consuming_delta():
    first = history_fixture()
    second = following(first, card=HOG_DECK[0])
    cursor = HistoryCycleCursor(prior(), opening=first.opening)
    cursor.sync(second)
    for bad in (first, first.to_payload(), replace(second, root_fair_sha="f" * 64),
            replace(second, opening=replace(second.opening, owner=1))):
        with pytest.raises(ValueError):
            cursor.sync(bad)
    assert cursor.history == second and cursor.cycle.play_count == 1


def test_cursor_impossible_cycle_rolls_back_history_and_support():
    first = history_fixture()
    second = following(first, card=HOG_DECK[0])
    cursor = HistoryCycleCursor(prior(), opening=first.opening)
    cursor.sync(second)
    digest = cursor.cycle.support_digest()
    with pytest.raises(IncompatiblePublicCycle):
        cursor.sync(following(second, card=HOG_DECK[0]))
    assert cursor.history == second and cursor.cycle.support_digest() == digest


def test_native_factory_rejects_unverified_decks_before_engine_creation():
    other = tuple(sorted((*HOG_DECK[:-1], 999999)))
    for frozen in (FrozenDeckPrior("fixed-experiment", "a" * 64, (DeckFrequency(other, 1),)),
            FrozenDeckPrior("fixed-experiment", "a" * 64, tuple(sorted(
                (DeckFrequency(tuple(sorted(HOG_DECK)), 1), DeckFrequency(other, 1)), key=lambda entry:entry.cards)))):
        with pytest.raises(ValueError, match="verified base Hog"):
            factory(frozen=frozen)


def test_factory_identity_binds_prior_and_does_not_reuse_old_sampler():
    first = factory()
    second = factory(frozen=replace(prior(), source_sha="d" * 64))
    assert first.sampler_sha != second.sampler_sha != HistoryBeliefFactory.sampler_sha


def test_factory_rejects_bad_root_deadline_and_budget_before_native_access():
    accessed = []
    target = factory(lambda: accessed.append(True))
    first = history_fixture()
    _, info = opening_fixture()
    for bad in (replace(info, owner=1), replace(info, tick=102), replace(info, frame={} )):
        with pytest.raises(ValueError, match="FAIR history"):
            target.build(first, bad, seed=1)
    for attempts in (0, 33, True):
        with pytest.raises(ValueError):
            target.build(first, info, seed=1, attempts=attempts)
    def deadline():
        raise TimeoutError("budget")
    with pytest.raises(TimeoutError):
        target.build(first, info, seed=1, check_deadline=deadline)
    assert accessed == []
    target.close()
    target.close()
    with pytest.raises(ValueError):
        target.create(first, seed=1)


def fake_construction(monkeypatch, *, changed_draw=False, fail_session=False):
    state = dict(transports=0, atomic=0, closed=0, initial_draws=[])
    class Native:
        def __init__(self):
            state["transports"] += 1
        def close_transport(self):
            state["closed"] += 1
        def start_atomic_steps(self):
            state["atomic"] += 1
    class Conditioner:
        def __init__(self, native, match):
            pass
        def create(self, own, **kwargs):
            return (HOG_DECK, HOG_DECK), dict(players=[dict(owner=owner, draw=HOG_DECK) for owner in (0, 1)])
    class Environment:
        def __init__(self, native):
            self.native = native
        def reset(self, *, match_config, options):
            draw = match_config.deck1
            if changed_draw:
                draw = (*draw[1:], draw[0])
            state["initial_draws"].append(draw)
            self.raw_observation = dict(players=[dict(owner=0, draw=match_config.deck0), dict(owner=1, draw=draw)])
    def session(rollout, **kwargs):
        if fail_session:
            raise IncompatibleHistory("opening frame")
        return SimpleNamespace(rollout=rollout)
    monkeypatch.setattr(prior_history, "NativeConditioner", Conditioner)
    monkeypatch.setattr(prior_history, "FinalBattleEnv", Environment)
    monkeypatch.setattr(prior_history, "FinalRollout", lambda environment, **kwargs:SimpleNamespace(environment=environment))
    monkeypatch.setattr(prior_history, "HistoryReplaySession", session)
    monkeypatch.setattr(prior_history, "initial_draw", lambda player:(tuple(player["draw"]), tuple(range(8))))
    return Native, state


def test_sampled_draw_is_installed_in_new_native_engine_each_time(monkeypatch):
    native, state = fake_construction(monkeypatch)
    target = factory(native)
    first = history_fixture()
    sessions = [target.create(first, seed=seed) for seed in (17, 17, 18)]
    assert state["transports"] == state["atomic"] == 3
    assert len(target.cursors) == 1
    assert sessions[0].conditioning_manifest == sessions[1].conditioning_manifest
    assert sessions[0].conditioning_manifest != sessions[2].conditioning_manifest
    assert sessions[0].draw_completion.initial_draw == state["initial_draws"][0]
    assert sessions[0].conditioning_manifest["actual_state_copied"] is False
    assert sessions[0].conditioning_manifest["training_admissible"] is False
    target.close()
    assert target.cursors == {}


@pytest.mark.parametrize("failure", ["shuffle", "session"])
def test_conditional_creation_failures_close_transport(monkeypatch, failure):
    native, state = fake_construction(monkeypatch, changed_draw=failure=="shuffle", fail_session=failure=="session")
    target = factory(native)
    with pytest.raises(ValueError):
        target.create(history_fixture(), seed=42)
    assert state["closed"] == 1


@pytest.mark.parametrize("variant", ["same", "hand-slots", "wrong-hand", "wrong-cycle"])
def test_build_verifies_native_current_draw_and_cleans_up_mismatch(monkeypatch, variant):
    first = history_fixture()
    _, info = opening_fixture()
    cleanup = []
    native = SimpleNamespace(observe=lambda:dict(tick=100, players=[dict(owner=1)]),
        close_transport=lambda:cleanup.append("transport"))
    session = SimpleNamespace(rollout=SimpleNamespace(runtime=SimpleNamespace(environment=SimpleNamespace(native=native))),
        draw_completion=SimpleNamespace(initial_draw=HOG_DECK, current_draw=HOG_DECK), conditioning_manifest={},
        close=lambda:cleanup.append("snapshot"))
    monkeypatch.setattr(HistoryBeliefFactory, "build", lambda *args, **kwargs:session)
    actual = list(HOG_DECK)
    if variant == "hand-slots":
        actual[0], actual[1] = actual[1], actual[0]
    elif variant == "wrong-hand":
        actual[0], actual[4] = actual[4], actual[0]
    elif variant == "wrong-cycle":
        actual[4], actual[5] = actual[5], actual[4]
    monkeypatch.setattr(prior_history, "initial_draw", lambda player:(tuple(actual), tuple(range(8))))
    target = factory()
    if variant in ("same", "hand-slots"):
        assert target.build(first, info, seed=42) is session
        assert session.conditioning_manifest["native_draw_verified"] is True
        assert session.conditioning_manifest["native_current_draw"] == actual
        assert session.conditioning_manifest["logical_hand_order_differs"] is (variant == "hand-slots")
        assert cleanup == []
    else:
        with pytest.raises(ValueError, match="reconstructed hand"):
            target.build(first, info, seed=42)
        assert cleanup == ["snapshot", "transport"]
