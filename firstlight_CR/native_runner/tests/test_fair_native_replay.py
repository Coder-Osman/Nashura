import random
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import ActionV1, EventV1
from native_runner.training.event_scheduling import EventExecutionLedger
from native_runner.training.fair_search.belief import NativeConditioner, OwnInitialDraw, initial_draw, known_own_draw, public_draw_compatible
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import PublicEvent
from native_runner.training.fair_search.native import NativeBeliefWorld, NativeMatchRuntime
from native_runner.training.fair_search.benchmark_roots import summarize


def test_install_puct_evicts_previous_runtime_state_codec(tmp_path, monkeypatch):
    from native_runner.training.fair_search import native

    root = tmp_path / "puct"
    root.mkdir()
    (root / "event_policy_rollout.py").touch()
    monkeypatch.setattr(sys, "path", list(sys.path))
    rollout_type = object()
    modules = {
        "runtime_contract": SimpleNamespace(__file__=str(root / "runtime_contract.py"), install=lambda: None),
        "recorder_capsule": SimpleNamespace(__file__=str(root / "recorder_capsule.py")),
        "event_policy_rollout": SimpleNamespace(__file__=str(root / "event_policy_rollout.py"),
                                                EventPolicyRollout=rollout_type),
    }
    for name in (*modules, "policy_rollout", "action_codec", "state_codec", "native_adapter", "tree_search"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(__file__=str(tmp_path / "old" / f"{name}.py")))

    def imported(name):
        if name == "event_policy_rollout":
            assert "state_codec" not in sys.modules
        return modules[name]

    monkeypatch.setattr(native.importlib, "import_module", imported)
    assert native.install_puct(root) is rollout_type


class ShuffleNative:
    def create_match(self, configuration):
        players = []
        for owner, deck in enumerate(configuration[:2]):
            permutation = ((5, 2, 7, 0, 4, 1, 6, 3) if owner == 0 else
                           (3, 6, 1, 4, 2, 7, 0, 5))
            players.append(dict(
                owner=owner,
                hand=[dict(cardId=deck[slot], deckSlot=slot, handIndex=index)
                      for index, slot in enumerate(permutation[:4])],
                cycle=[dict(cardId=deck[slot], deckSlot=slot, cycleIndex=index)
                       for index, slot in enumerate(permutation[4:])],
            ))
        return dict(players=players)


@pytest.mark.parametrize("owner", [0, 1])
def test_conditioned_public_first_play_preserves_own_prefix_and_samples_all_hand_slots(owner):
    conditioner = NativeConditioner(ShuffleNative(), lambda *args: args)
    own = OwnInitialDraw(owner, HOG_DECK[:6])
    opponent = 1 - owner
    observed_slots = set()
    for seed in range(40):
        _, observation = conditioner.create(own, seed=seed, rng=random.Random(seed),
                                             required_initial_cards={opponent: HOG_DECK[-1]})
        players = {player["owner"]: player for player in observation["players"]}
        assert initial_draw(players[owner])[0][:6] == own.known_cards
        opponent_hand = initial_draw(players[opponent])[0][:4]
        assert HOG_DECK[-1] in opponent_hand
        observed_slots.add(opponent_hand.index(HOG_DECK[-1]))
    assert observed_slots == set(range(4))


def test_public_conditioning_rejects_private_owner():
    conditioner = NativeConditioner(ShuffleNative(), lambda *args: args)
    with pytest.raises(ValueError, match="public card owner"):
        conditioner.create(OwnInitialDraw(0, HOG_DECK[:5]), seed=1, rng=random.Random(1),
                           required_initial_cards={0: HOG_DECK[0]})


@pytest.mark.parametrize("search_only", [False, True])
def test_commit_preserves_decision_history_without_materializing_information(search_only):
    runtime = NativeMatchRuntime.__new__(NativeMatchRuntime)
    submitted = []
    runtime.history = {0: [], 1: []}

    def unexpected_information(owner):
        raise AssertionError("commit must not copy the full information history")

    def submit(owner, action):
        submitted.append((owner, action))
        runtime.rollout.ready.remove(owner)
        runtime.rollout.tick += 1

    observation = SimpleNamespace(players=(SimpleNamespace(owner=0, next_card=HOG_DECK[4]),))
    runtime.information_for = unexpected_information
    runtime.rollout = SimpleNamespace(
        tick=393, ready=[0],
        _search_execution_ledger=object() if search_only else None,
        current_policy_observations=lambda: {0: observation},
        commit_search_action=submit, decide=lambda owner, key: submit(owner, key))
    runtime.commit(0, "wait")
    assert submitted == [(0, "wait")]
    assert runtime.history[1] == []
    assert json.loads(runtime.history[0][0]) == dict(tick=393, action="wait", next_card=HOG_DECK[4])
    with pytest.raises(ValueError, match="not at a decision"):
        runtime.commit(0, "wait")
    assert len(runtime.history[0]) == len(submitted) == 1


@pytest.mark.parametrize("execution_only", [False, True])
def test_match_runtime_activates_direct_submission_only_when_requested(monkeypatch, execution_only):
    from native_runner import battle_env
    from native_runner.training.fair_search import native

    calls = []
    transport = SimpleNamespace(start_atomic_steps=lambda: calls.append("atomic"),
                                close_transport=lambda: calls.append("close"))
    monkeypatch.setattr(native, "create_search_native", lambda *args, **kwargs: transport)
    monkeypatch.setattr(battle_env, "BattleEnvV1", lambda **kwargs: SimpleNamespace())
    observation = SimpleNamespace(players=tuple(SimpleNamespace(
        owner=owner, hand=HOG_DECK[:4], next_card=HOG_DECK[4],
        metadata={"hand_slot_by_card": {str(card): index for index, card in enumerate(HOG_DECK[:4])}})
        for owner in (0, 1)))

    class Rollout:
        reward_config = overflow = None
        tick = 0

        def __init__(self, *args, **kwargs):
            self.ready = [0, 1]
            self._search_execution_ledger = None

        def reset(self, **kwargs):
            calls.append("reset")

        def begin_search_root(self, *, search_only):
            assert search_only
            self._search_execution_ledger = object()
            calls.append("begin")

        def end_search_root(self):
            self._search_execution_ledger = None
            calls.append("end")

        def current_policy_observations(self):
            return {0: observation, 1: observation}

        def commit_search_action(self, owner, action):
            calls.append("direct")
            self.ready.remove(owner)

        def decide(self, owner, *, key):
            assert not execution_only
            calls.append("evaluate")
            self.ready.remove(owner)

    runtime = NativeMatchRuntime(1234, {owner: SimpleNamespace(policy=object()) for owner in (0, 1)},
                                 {0: "a", 1: "a"}, rollout_type=Rollout, seed=1, match_id="test",
                                 execution_only=execution_only)
    runtime.commit(0, "wait")
    assert ("direct" if execution_only else "evaluate") in calls
    assert ("begin" in calls) == execution_only
    assert len(runtime.history[0]) == 2
    runtime.close()
    runtime.close()
    assert calls.count("close") == 1
    assert calls.count("end") == int(execution_only)


def test_direct_submission_close_releases_transport_even_if_ledger_teardown_fails():
    runtime = NativeMatchRuntime.__new__(NativeMatchRuntime)
    runtime.closed = False
    runtime.execution_only = True
    closed = []

    def fail():
        raise ValueError("ledger teardown failed")

    runtime.rollout = SimpleNamespace(end_search_root=fail)
    runtime.native = SimpleNamespace(close_transport=lambda: closed.append(True))
    with pytest.raises(ValueError, match="ledger teardown"):
        runtime.close()
    runtime.close()
    assert closed == [True]


@pytest.mark.parametrize("owner", [0, 1])
def test_public_sequence_prefilter_preserves_sampling_and_rejects_before_replay(owner):
    class CountingNative(ShuffleNative):
        def __init__(self):
            self.calls = 0

        def create_match(self, configuration):
            self.calls += 1
            return super().create_match(configuration)

    opponent = 1 - owner
    sequence = (HOG_DECK[-1], HOG_DECK[0], HOG_DECK[1])
    accepted = rejected = 0
    for seed in range(40):
        native = CountingNative()
        conditioner = NativeConditioner(native, lambda *args: args)
        own = OwnInitialDraw(owner, HOG_DECK[:6])
        baseline_rng, optimized_rng = random.Random(seed), random.Random(seed)
        decks, baseline = conditioner.create(own, seed=seed, rng=baseline_rng,
                                             required_initial_cards={opponent: sequence[0]})
        draw = initial_draw(next(player for player in baseline["players"] if player["owner"] == opponent))[0]
        native.calls = 0
        if public_draw_compatible(draw, sequence):
            actual_decks, actual = conditioner.create(own, seed=seed, rng=optimized_rng,
                                                      required_initial_cards={opponent: sequence[0]},
                                                      public_play_sequences={opponent: sequence})
            assert actual_decks == decks
            assert actual == baseline
            accepted += 1
        else:
            with pytest.raises(ValueError, match="absent"):
                conditioner.create(own, seed=seed, rng=optimized_rng,
                                   required_initial_cards={opponent: sequence[0]},
                                   public_play_sequences={opponent: sequence})
            assert native.calls == 1
            rejected += 1
        assert optimized_rng.getstate() == baseline_rng.getstate()
    assert accepted > 0 and rejected > 0


def test_public_sequence_prefilter_handles_new_draws_and_card_recycling():
    assert public_draw_compatible(HOG_DECK, HOG_DECK * 2)
    assert public_draw_compatible(HOG_DECK, (HOG_DECK[0], *HOG_DECK[4:], HOG_DECK[0]))
    assert not public_draw_compatible(HOG_DECK, (HOG_DECK[4],))
    assert not public_draw_compatible(HOG_DECK, (HOG_DECK[0], HOG_DECK[0]))


def test_public_sequence_prefilter_rejects_own_history():
    conditioner = NativeConditioner(ShuffleNative(), lambda *args: args)
    with pytest.raises(ValueError, match="public play sequence owner"):
        conditioner.create(OwnInitialDraw(0, HOG_DECK[:5]), seed=1, rng=random.Random(1),
                           public_play_sequences={0: HOG_DECK[:2]})


def test_current_own_next_card_conditions_draw_before_the_next_decision_commit():
    own = SimpleNamespace(mask=torch.ones(1, 8, dtype=torch.bool),
                          card_vocab_id=torch.arange(8).unsqueeze(0),
                          runtime_features=torch.zeros(1, 8, 2))
    own.runtime_features[0, 5, 1] = 1.
    information = SimpleNamespace(owner=1, frame=SimpleNamespace(own_cards=own),
                                  own_history=(json.dumps({"initial_draw": list(HOG_DECK[:5])}),))
    catalog = SimpleNamespace(vocab_id=lambda card: HOG_DECK.index(card))
    assert known_own_draw(information, catalog).known_cards == HOG_DECK[:6]
    own.runtime_features[0, 5, 1] = 0.
    own.runtime_features[0, 0, 1] = 1.
    assert known_own_draw(information, catalog).known_cards == HOG_DECK[:5]


def replay_runtime(*, card_present=True, rejected=False):
    runtime = NativeMatchRuntime.__new__(NativeMatchRuntime)
    runtime.public, runtime.public_seen = [], set()
    player = SimpleNamespace(owner=1, hand=(HOG_DECK[0],) if card_present else (),
                             metadata={"hand_slot_by_card": {str(HOG_DECK[0]): 3}})
    observation = SimpleNamespace(players=(player,), events=())
    observations = {0: observation, 1: observation}
    ledger = EventExecutionLedger()
    submitted = []

    def step(commands, **kwargs):
        submitted.append(commands)
        action = commands[1][0]
        event = SimpleNamespace(event_type="action_rejected" if rejected else "action_executed",
                                tick=393, owner=1, card_id=action.card_id,
                                position=(1250, 30750), data={"action_id": action.action_id}, combat=None)
        observation.events = (event,)
        return (event,)

    runtime.environment = SimpleNamespace(step=step)

    def step_native(*, stop_tick):
        outcomes = runtime.environment.step({0: (ActionV1.wait(0),), 1: (ActionV1.wait(1),)})
        ledger.reconcile(outcomes)
        runtime.rollout.tick = 393
        return outcomes

    runtime.rollout = SimpleNamespace(tick=392, match_id="sampled-world", ledger=ledger,
                                      current_policy_observations=lambda: observations,
                                      step_native=step_native)
    return runtime, submitted


def test_public_replay_uses_sampled_slot_normal_receipt_and_exact_target():
    runtime, submitted = replay_runtime()
    original_step = runtime.environment.step
    public = PublicEvent(1, 393, HOG_DECK[0], 1250, 30750)
    runtime.advance(393, replay_events=(public,))
    action = submitted[0][1][0]
    assert action.hand_slot == 3
    assert action.target_grid == (1, 30)
    assert action.subcell_offset == (-.25, .25)
    assert action.execute_offset_ticks == 1
    assert runtime.environment.step is original_step
    assert runtime.public == [public]
    assert runtime.rollout.ledger.counters()["confirmed"] == 1
    assert runtime.rollout.ledger.counters()["unconfirmed"] == 0


def test_replay_step_wrapper_leaves_no_bound_method_in_environment_snapshot():
    runtime, submitted = replay_runtime()
    inherited_step = runtime.environment.step

    class Environment:
        def step(self, commands, **kwargs):
            return inherited_step(commands, **kwargs)

    runtime.environment = Environment()
    runtime.advance(393, replay_events=(PublicEvent(1, 393, HOG_DECK[0], 1250, 30750),))
    assert "step" not in vars(runtime.environment)
    assert submitted


def test_incremental_receipts_match_observation_history_without_materializing_views():
    events = (
        EventV1(tick=393, event_type="action_executed", owner=1, card_id=HOG_DECK[0],
                position=(1250, 30750)),
        EventV1(tick=394, event_type="action_rejected", owner=0, card_id=HOG_DECK[1],
                position=(500, 500)),
        EventV1(tick=395, event_type="action_executed", owner=0, card_id=HOG_DECK[1],
                position=(500, 500)),
        EventV1(tick=396, event_type="action_executed", owner=1, card_id=HOG_DECK[2],
                position=(500, 500), data={"oracle_only": True}),
    )
    runtime = NativeMatchRuntime.__new__(NativeMatchRuntime)
    runtime.public, runtime.public_seen = [], set()

    def unexpected_observation():
        raise AssertionError("incremental receipts must preserve lazy observations")

    runtime.rollout = SimpleNamespace(current_policy_observations=unexpected_observation)
    for event in events:
        runtime.collect_public_events((event, event))
    incremental = list(runtime.public)
    assert incremental == [PublicEvent(1, 393, HOG_DECK[0], 1250, 30750),
                           PublicEvent(0, 395, HOG_DECK[1], 500, 500)]
    runtime.public, runtime.public_seen = [], set()
    visible = events[:3]
    runtime.rollout.current_policy_observations = lambda: {
        owner: SimpleNamespace(events=visible) for owner in (0, 1)}
    runtime.collect_public_events()
    assert runtime.public == incremental


@pytest.mark.parametrize("stop_tick", [None, 393, 764])
def test_actual_and_replay_advance_use_the_same_single_tick_damage_interval(stop_tick):
    from native_runner.tests.test_battle_env import FakeNativeEnv, _make_env, _reset

    runtime = NativeMatchRuntime.__new__(NativeMatchRuntime)
    runtime.public, runtime.public_seen = [], set()
    runtime.environment, native = _make_env(FakeNativeEnv(initial_entity=False))
    observations, _ = _reset(runtime.environment)
    initial_tick = observations[0].tick

    def damage_tower(state, old_tick, new_tick):
        tower = next(item for item in state["objects"] if item["slot"] == 1)
        tower["hp"] -= 5 * (new_tick - old_tick)

    native.step_mutators.extend([damage_tower] * 3)

    def step_native(*, stop_tick):
        commands = {owner: (ActionV1.wait(owner, ticks=stop_tick - runtime.rollout.tick),)
                    for owner in (0, 1)}
        observed, *_ = runtime.environment.step(commands)
        runtime.rollout.tick = observed[0].tick
        return ()

    runtime.rollout = SimpleNamespace(tick=initial_tick, step_native=step_native,
                                      current_policy_observations=runtime.environment.observe_dual)
    runtime.advance(stop_tick)
    runtime.advance()
    runtime.advance(initial_tick + 120)
    assert runtime.rollout.tick == initial_tick + 3
    damage = [event for event in runtime.environment.observe_dual()[0].events
              if event.event_type == "tower_damage"]
    assert [event.tick for event in damage] == list(range(initial_tick + 1, initial_tick + 4))
    assert [event.combat.amount for event in damage] == [5, 5, 5]


@pytest.mark.parametrize("reason", ["absent", "boundary", "rejected"])
def test_incompatible_replay_never_fabricates_public_execution(reason):
    runtime, submitted = replay_runtime(card_present=reason != "absent", rejected=reason == "rejected")
    original_step = runtime.environment.step
    public = PublicEvent(1, 394 if reason == "boundary" else 393, HOG_DECK[0], 1250, 30750)
    if reason == "rejected":
        runtime.advance(393, replay_events=(public,))
        assert runtime.rollout.ledger.counters()["rejected"] == 1
    else:
        with pytest.raises(ValueError, match="boundary"):
            runtime.advance(393, replay_events=(public,))
        assert not submitted
    assert runtime.environment.step is original_step
    assert not runtime.public


def test_world_cleanup_releases_snapshot_even_when_restore_validation_fails():
    world = NativeBeliefWorld.__new__(NativeBeliefWorld)
    world.snapshot = {"handle": 17}
    released, ended, closed = [], [], []
    world.runtime = SimpleNamespace(
        native=SimpleNamespace(release_snapshot=lambda snapshot: released.append(snapshot)),
        rollout=SimpleNamespace(end_search_root=lambda: ended.append(True)),
        close=lambda: closed.append(True),
    )

    def failed_restore():
        raise ValueError("world-local root restore checksum differs")

    world.restore_root = failed_restore
    with pytest.raises(ValueError, match="checksum"):
        world.close()
    assert released == [{"handle": 17}]
    assert ended == closed == [True]
    assert world.snapshot is None
    world.close()
    assert len(released) == len(closed) == 1


@pytest.mark.parametrize("failure", [None, "capture", "snapshot", "digest"])
def test_replace_root_bounds_restore_cache_and_keeps_failed_root(monkeypatch, failure):
    from collections import OrderedDict

    world = NativeBeliefWorld.__new__(NativeBeliefWorld)
    world.owner, world.search_only = 0, True
    world.sidecar = {"tick": 1}
    old_sidecar = world.sidecar
    old_decoded = object()
    cache = OrderedDict({id(old_sidecar): (old_sidecar, old_decoded)})
    world.snapshot = "old"
    world.battle_sidecar, world.battle_state = "env", "old-state"
    released = []

    def capture():
        if failure == "capture":
            raise RuntimeError("capture")
        state = {"tick": world.sidecar["tick"] + 1}
        cache[id(state)] = (state, object())
        return state

    def snapshot():
        if failure == "snapshot":
            raise RuntimeError("snapshot")
        return "new"

    def digest(command):
        if failure == "digest":
            raise RuntimeError("digest")
        return {"sha256": "new-digest"}

    world.runtime = SimpleNamespace(information_for=lambda owner: object(),
        rollout=SimpleNamespace(_search_restore_cache=cache, capture_search=capture),
        native=SimpleNamespace(create_snapshot=snapshot, _request=digest, release_snapshot=released.append))
    monkeypatch.setattr(world, "_capture_environment", lambda: None)
    monkeypatch.setattr(world, "_capture_histories", lambda: None)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            world.replace_root()
        assert world.snapshot == "old" and world.sidecar is old_sidecar
        assert cache[id(old_sidecar)][1] is old_decoded
        assert released == (["new"] if failure == "digest" else [])
    else:
        for _ in range(200):
            world.replace_root()
            assert len(cache) == 1
            assert cache[id(world.sidecar)][0] is world.sidecar
        assert world.sidecar["tick"] == 201
        assert id(old_sidecar) not in cache
    assert len(cache) == 1


def test_world_constructor_releases_snapshot_when_digest_capture_fails(monkeypatch):
    released, closed = [], []
    monkeypatch.setattr(NativeBeliefWorld, "_capture_environment", lambda world: None)

    def failed_digest(command):
        raise ValueError("digest capture failed")

    runtime = SimpleNamespace(
        information_for=lambda owner: None,
        native=SimpleNamespace(create_snapshot=lambda: {"handle": 18}, _request=failed_digest,
                               release_snapshot=lambda snapshot: released.append(snapshot)),
        rollout=SimpleNamespace(begin_search_root=lambda **kwargs: None, capture=lambda: {},
                                end_search_root=lambda: None),
        close=lambda: closed.append(True),
    )
    with pytest.raises(ValueError, match="digest capture"):
        NativeBeliefWorld(runtime, 0)
    assert released == [{"handle": 18}]
    assert closed == [True]


@pytest.mark.parametrize("search_only", [False, True])
def test_world_restores_python_environment_before_each_branch(monkeypatch, search_only):
    from native_runner.tests.test_battle_env import FakeNativeEnv, _make_env, _reset

    environment, _ = _make_env(FakeNativeEnv(initial_entity=False))
    observations, _ = _reset(environment)
    initial_tick = observations[0].tick
    restored_receipts = []

    class Sidecar:
        def __init__(self, environment, **kwargs):
            self.environment = environment

        def capture(self):
            return deepcopy({key: value for key, value in vars(self.environment).items()
                             if key in ("_raw", "_events", "_pending", "_step_action_outcomes",
                                        "_previous_entities", "_previous_towers")})

        def restore(self, state, *, receipt):
            vars(self.environment).update(deepcopy(state))
            restored_receipts.append(receipt)

    receipt_type = SimpleNamespace(from_observation=lambda observation, digest: SimpleNamespace(
        generation=observation["generation"], state_epoch=observation["stateEpoch"],
        tick=observation["tick"], native_digest=digest))
    monkeypatch.setitem(sys.modules, "native_adapter", SimpleNamespace(
        BattleEnvSidecar=Sidecar, NativeRestoreReceipt=receipt_type))
    released = []
    root_observation = deepcopy(environment.raw_observation)
    rollout = SimpleNamespace(
        tick=initial_tick, ready=[0, 1], begin_search_root=lambda **kwargs: None,
        capture=lambda: {}, capture_search=lambda: {},
        current_policy_observations=lambda: environment.observe_dual(policy_features_only=True),
        end_search_root=lambda: None)

    def restore_rollout(state):
        assert environment.current_tick == initial_tick
        assert not environment._pending
        assert not environment._step_action_outcomes
        assert not any(event.tick > initial_tick for event in environment._events)
        rollout.tick, rollout.ready = initial_tick, [0, 1]

    rollout.restore = rollout.restore_search = restore_rollout
    runtime = SimpleNamespace(
        environment=environment, rollout=rollout,
        information_for=lambda owner: SimpleNamespace(sha256="root"),
        history={0: [], 1: []}, public=[], public_seen=set(), close=lambda: None,
        native=SimpleNamespace(create_snapshot=lambda: {"handle": 1},
                               restore=lambda snapshot: deepcopy(root_observation),
                               _request=lambda command: {"sha256": "root"},
                               clear_replay_schedule=lambda: None,
                               release_snapshot=lambda snapshot: released.append(snapshot)))
    world = NativeBeliefWorld(runtime, 0, search_only=search_only)
    root_public, root_history = runtime.public, dict(runtime.history)
    for branch in range(2):
        environment._raw["tick"] += 3
        environment._events.append(EventV1(tick=initial_tick + 3, event_type="action_executed",
                                           owner=0, card_id=HOG_DECK[branch], position=(500, 500)))
        environment._pending.append(object())
        environment._step_action_outcomes.append(object())
        public = PublicEvent(0, initial_tick + 3, HOG_DECK[branch], 500, 500)
        runtime.public = runtime.public.append(public)
        runtime.public_seen.add(public)
        runtime.history[0] = runtime.history[0].append(json.dumps(dict(branch=branch)))
        world.restore_root()
        assert runtime.public == []
        assert runtime.public is root_public
        assert not runtime.public_seen
        assert all(runtime.history[owner] is root_history[owner] for owner in (0, 1))
        assert rollout.ready == [0, 1]
    world.close()
    assert len(restored_receipts) == 3
    assert released == [{"handle": 1}]


@pytest.mark.parametrize("failure", [None, "identity", "events", "overflow", "rejected", "gap"])
def test_restore_cursor_rebase_requires_attested_empty_native_boundary(failure):
    window = SimpleNamespace(complete=True, events=(), overflow_count=0, rejected_count=0,
                             rejected_capture_count=0, epoch_first_sequence=100,
                             oldest_retained_sequence=100, next_sequence=100)
    snapshot = SimpleNamespace(generation=1, state_epoch=2, tick=764,
                               combat_events=window, phase_runtime=SimpleNamespace(window=window, events=()),
                               visibility_runtime=window, remaining_runtime=window)
    receipt = SimpleNamespace(generation=1, state_epoch=2, tick=764)
    if failure == "identity":
        snapshot.state_epoch = 1
    elif failure == "events":
        window.events = (object(),)
    elif failure == "overflow":
        window.overflow_count = 1
    elif failure == "rejected":
        window.rejected_capture_count = 1
    elif failure == "gap":
        window.oldest_retained_sequence = 101
    world = NativeBeliefWorld.__new__(NativeBeliefWorld)
    world.runtime = SimpleNamespace(environment=SimpleNamespace())
    if failure:
        with pytest.raises(ValueError, match="identity|boundary"):
            world._rebase_native_cursors(snapshot, receipt)
    else:
        world._rebase_native_cursors(snapshot, receipt)
        environment = world.runtime.environment
        assert environment._combat_next_sequence == environment._remaining_next_sequence == 100
        assert environment._public_card_play_event_identity == (1, 2)
        assert environment._remaining_rejected_count_baseline == 0


@pytest.mark.parametrize("failure", [None, "future", "transition", "sequence"])
def test_restore_accepts_only_root_tick_phase_measurement_events(failure):
    event = SimpleNamespace(tick=764, kind="movement_scale", sequence=100)
    window = SimpleNamespace(complete=True, overflow_count=0, rejected_count=0,
                             epoch_first_sequence=100, oldest_retained_sequence=100, next_sequence=101)
    if failure == "future":
        event.tick += 1
    elif failure == "transition":
        event.kind = "target_change"
    elif failure == "sequence":
        event.sequence += 1
    snapshot = SimpleNamespace(generation=1, state_epoch=2, tick=764,
                               combat_events=None, visibility_runtime=None, remaining_runtime=None,
                               phase_runtime=SimpleNamespace(window=window, events=(event,)))
    world = NativeBeliefWorld.__new__(NativeBeliefWorld)
    world.runtime = SimpleNamespace(environment=SimpleNamespace())
    receipt = SimpleNamespace(generation=1, state_epoch=2, tick=764)
    if failure:
        with pytest.raises(ValueError, match="boundary"):
            world._rebase_native_cursors(snapshot, receipt)
    else:
        world._rebase_native_cursors(snapshot, receipt)
        assert world.runtime.environment._phase_next_sequence == 101


def test_root_benchmark_rejects_different_branches_despite_identical_visits():
    baseline = dict(root_sha256="root", checkpoint_sha256="model", config_sha256="config",
                    runtime_sha256="runtime", visits={"wait": 32}, selected="wait",
                    particles=1, trace_sha256=["information-trace"], seconds=20., search_only=False)
    optimized = {**baseline, "seconds": 10., "search_only": True}
    assert summarize([baseline, optimized])["root_speedup"] == 2
    with pytest.raises(ValueError, match="branch information"):
        summarize([baseline, {**optimized, "trace_sha256": ["different-information"]}])


def test_update_failure_atomically_saves_fair_inputs_without_reading_actual_match(tmp_path, monkeypatch):
    from native_runner.training.fair_search.native import NativeReplayFactory
    from native_runner.training.fair_search.draw import ConditionalDraws

    factory = NativeReplayFactory([30000], {}, {}, object, diagnostic_directory=tmp_path, conditional_draws=True)
    pool = ConditionalDraws(0, HOG_DECK[:5])
    world = SimpleNamespace(draw_pool=pool, draw_seed=123,
                            root_information=lambda: SimpleNamespace(to_payload=lambda: {"tick": 3263}))
    information = SimpleNamespace(owner=0, tick=3276, to_payload=lambda: {"tick": 3276})

    def fail_update(world, information):
        raise ValueError("incompatible draw fixture")

    monkeypatch.setattr(factory, "_update", fail_update)
    for attempt in range(2):
        with pytest.raises(ValueError, match="incompatible draw fixture"):
            factory.update(world, information)
    paths = list(tmp_path.glob("update-root-0-3276-*.pt"))
    assert len(paths) == 2
    assert not list(tmp_path.glob("*.tmp"))
    saved = torch.load(paths[0], weights_only=True)
    assert set(saved) == {"schema", "error", "information", "previous", "seed", "draw_state"}
    assert saved["information"] == {"tick": 3276}
    assert saved["previous"] == {"tick": 3263}
    assert saved["seed"] == 123
    assert saved["draw_state"]["next_refill"] == {0: 0, 1: 0}
