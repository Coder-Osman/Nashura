from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_history import history_fixture, opening_fixture
from native_runner.tests.test_fair_final_prior_history import factory, prior
from native_runner.training.fair_final import local_reconditioning as local
from native_runner.training.fair_final.deck_belief import PublicCycleBelief
from native_runner.training.fair_final.history_belief import HistoryReplaySession
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.prior_history import HistoryCycleCursor
from native_runner.training.fair_final.timing import DecisionClock, Reservation
from native_runner.training.fair_search.config import HOG_DECK


def native_observation():
    players = []
    for owner in (0, 1):
        deck = [dict(cardId=card, deckSlot=slot) for slot, card in enumerate(HOG_DECK)]
        players.append(dict(owner=owner, deck=deck,
            hand=[dict(card, handIndex=index) for index, card in enumerate(deck[:4])],
            cycle=[dict(card, cycleIndex=index) for index, card in enumerate(deck[4:])]))
    return dict(generation=3, stateEpoch=5, tick=100, players=players)


@pytest.mark.parametrize("owner", [0, 1])
def test_fields_preserve_own_slots_and_install_canonical_enemy_hand(owner):
    raw = native_observation()
    completion = PublicCycleBelief(prior(), owner=1-owner, opening_tick=100).sample(seed=29)
    fields, installed = local.native_draw_fields(raw, completion, owner=owner)
    assert len(fields) == 51 and fields[:3] == [3, 5, 100]
    assert fields[3+12*owner:15+12*owner] == fields[27+12*owner:39+12*owner]
    assert installed[:4] == tuple(sorted(completion.current_draw[:4]))
    assert installed[4:] == completion.current_draw[4:]
    assert raw == native_observation()


@pytest.mark.parametrize("failure", ["refill", "duplicate-player", "deck-mapping"])
def test_fields_reject_bad_native_layout(failure):
    raw = native_observation()
    if failure == "refill":
        raw["players"][1]["hand"].pop()
    elif failure == "duplicate-player":
        raw["players"][1]["owner"] = 0
    else:
        raw["players"][1]["deck"][0]["deckSlot"] = 7
    completion = PublicCycleBelief(prior(), owner=1, opening_tick=100).sample(seed=29)
    with pytest.raises(ValueError):
        local.native_draw_fields(raw, completion, owner=0)


@pytest.fixture
def transaction(monkeypatch):
    target = factory()
    history = history_fixture()
    _, information = opening_fixture()
    state = dict(raw=native_observation(), requests=0, restores=0, releases=0, receipt_bad=False,
        refresh_bad=False, rebase_bad=False, snapshots_bad=False)
    clock = DecisionClock()
    clock.tick, clock.ready = 100, {0, 1}
    runtime = SimpleNamespace(clock=clock, failed=False, ended=False, truncated=False,
        frozen={0:dict(own=True), 1:dict(own=True)})
    environment = SimpleNamespace(_pending_abilities=[], _pending=[], _raw=state["raw"],
        raw_observation=state["raw"], _invalidate_rich_cache=lambda:None,
        _rich_snapshot_for=lambda raw:SimpleNamespace(objects=()))
    runtime.environment = environment
    rollout = SimpleNamespace(runtime=runtime, information={0:information, 1:replace(information, owner=1)})
    rollout.freeze_due_information = lambda:deepcopy(rollout.information)

    class Snapshot:
        def __init__(self, unused):
            if state["snapshots_bad"]:
                raise RuntimeError("snapshot failed")
            self.raw = deepcopy(state["raw"])
            self.information = deepcopy(rollout.information)
        def restore(self):
            state["restores"] += 1
            state["raw"] = deepcopy(self.raw)
            environment.raw_observation = environment._raw = state["raw"]
            rollout.information = deepcopy(self.information)
            runtime.failed = False
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            state["releases"] += 1

    def request(command):
        state["requests"] += 1
        fields = list(map(int, command.split()[1:]))
        raw = deepcopy(state["raw"])
        raw["stateEpoch"] += 1
        for owner in (0, 1):
            player = raw["players"][owner]
            slots = fields[27+owner*12:35+owner*12]
            player["hand"] = [dict(player["deck"][slot], handIndex=index) for index, slot in enumerate(slots[:4])]
            player["cycle"] = [dict(player["deck"][slot], cycleIndex=index) for index, slot in enumerate(slots[4:])]
        state["raw"] = raw
        environment.raw_observation = raw
        return dict(ok=not state["receipt_bad"], experimental=True, generation=3, stateEpoch=raw["stateEpoch"], tick=100)

    def refresh(unused):
        if state["refresh_bad"]:
            rollout.information[0] = replace(information, frame={"changed":True})
        return rollout.freeze_due_information()

    def rebase(*unused):
        if state["rebase_bad"]:
            raise ValueError("dirty telemetry")

    environment.native = SimpleNamespace(observe=lambda:deepcopy(state["raw"]), _request=request)
    rollout.snapshot = Snapshot
    monkeypatch.setattr(local, "refresh_local_information", refresh)
    monkeypatch.setattr(local.NativeBeliefWorld, "_rebase_native_cursors", rebase)
    session = object.__new__(HistoryReplaySession)
    session.closed, session.owner, session.history = False, 0, history
    session.rollout, session.opponent = rollout, target.opponent
    session.conditioning_manifest = dict(source="independent-belief", actual_state_copied=False,
        sampler_sha=target.sampler_sha, prior_sha=target.prior.sha256, native_draw_verified=True)
    return local.SettledDrawReconditioner(target), session, history, information, state


def test_transaction_preserves_controller_and_fair_root_and_is_reproducible(transaction):
    conditioner, session, history, info, state = transaction
    clock = session.rollout.runtime.clock.capture()
    original = deepcopy(state["raw"])
    first = conditioner.apply(session, history, info, seed=71)
    second = conditioner.apply(session, history, info, seed=71)
    assert first["manifest"] == second["manifest"]
    assert second["cycle_delta"]["order_checks"] == 0
    assert first["manifest"]["history_ticks_replayed"] == 0
    assert first["manifest"]["training_admissible"] is False
    assert session.rollout.runtime.clock.capture() == clock
    assert state["raw"]["players"][0] == original["players"][0]
    assert state["releases"] == 2 and state["restores"] == 0


@pytest.mark.parametrize("failure", ["receipt_bad", "refresh_bad", "rebase_bad", "deadline"])
def test_transaction_rolls_back_on_post_mutation_failure(transaction, failure):
    conditioner, session, history, info, state = transaction
    original = deepcopy(state["raw"])
    if failure != "deadline":
        state[failure] = True
    calls = []
    def deadline():
        calls.append(True)
        if failure == "deadline" and len(calls) == 3:
            raise TimeoutError("budget")
    with pytest.raises((ValueError, TimeoutError)):
        conditioner.apply(session, history, info, seed=71, check_deadline=deadline)
    assert state["requests"] == state["restores"] == state["releases"] == 1
    assert state["raw"] == original
    assert session.rollout.freeze_due_information()[0].sha256 == info.sha256


@pytest.mark.parametrize("failure", ["single-ready", "pending", "queued", "actual", "identity", "closed", "snapshot"])
def test_unsupported_or_untrusted_roots_fail_before_mutation(transaction, failure):
    conditioner, session, history, info, state = transaction
    runtime = session.rollout.runtime
    if failure == "single-ready":
        runtime.clock.ready = {0}
    elif failure == "pending":
        runtime.clock.pending[1] = Reservation(0, 1, FullAction(), 100, 160)
    elif failure == "queued":
        runtime.environment._pending.append(SimpleNamespace(status="queued"))
    elif failure == "actual":
        session.conditioning_manifest["actual_state_copied"] = True
    elif failure == "identity":
        session.conditioning_manifest["sampler_sha"] = "f" * 64
    elif failure == "closed":
        conditioner.factory.close()
    else:
        state["snapshots_bad"] = True
    with pytest.raises((ValueError, RuntimeError)):
        conditioner.apply(session, history, info, seed=71)
    assert state["requests"] == 0


def test_local_sampling_matches_cold_public_cycle(transaction):
    conditioner, session, history, info, state = transaction
    for seed in (71, 72, 73):
        result = conditioner.apply(session, history, info, seed=seed)
        assert result["completion"] == HistoryCycleCursor(prior(), opening=history.opening).sample(history, seed=seed)
