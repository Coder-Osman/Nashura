"""A public play interrupts an opponent's not-yet-submitted delay plan."""

from types import SimpleNamespace
from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_event_action_space import _catalog, _deck, _environment, _frame, _grid, _placement
from tools import event_rl_rollout as rollout
from native_runner.training.event_model import EventActionV1
from native_runner.training.event_rollout import EventLaneRecorderV1, training_segments
from native_runner.tests.test_event_rollout import _step


@pytest.mark.parametrize("waiting_owner", [0, 1])
@pytest.mark.parametrize("replacement_acts", [False, True])
@pytest.mark.parametrize("defer_observations", [False, True])
@pytest.mark.parametrize("learner_owner", [None, 0, 1])
@pytest.mark.parametrize("shared_inference", [False, True])
def test_public_play_at_three_seconds_replaces_five_second_plan(
    monkeypatch, waiting_owner, replacement_acts, defer_observations, learner_owner, shared_inference
):
    catalog, deck = _catalog(), _deck()
    frames = {owner: _frame(catalog, deck) for owner in (0, 1)}
    for owner, frame in frames.items():
        frame.batch_size = 1
        frame.to_model_input = lambda *a, owner=owner, **kw: SimpleNamespace(owner=owner)
    mask = _environment({"0": _placement(_grid([(2, 7)]))}).action_mask(0)
    decisions, submitted = [], []

    class Env:
        episode_config = None
        tick = 99
        outcomes = ()
        public = ()

        @property
        def current_tick(self):
            return self.tick

        def observe_dual(self, **kwargs):
            return {owner: SimpleNamespace(
                tick=self.tick, time=SimpleNamespace(elixir_multiplier=1.0),
                players=[SimpleNamespace(owner=owner, hand=[deck[0]], elixir_exact=10.0)],
            ) for owner in (0, 1)}

        def reset(self, **kwargs):
            return self.observe_dual(), {}

        def action_mask(self, owner):
            return mask

        def action_outcomes(self):
            return self.outcomes

        def deployment_timing_events(self, owner):
            return [event for event in self.public if event["owner"] == owner]

        def step(self, commands, **kwargs):
            self.outcomes, self.public = [], []
            plays = [a for group in commands.values() for a in group if a.kind.value == "play_card"]
            advance = min(a.next_decision_ticks or 1 for group in commands.values() for a in group)
            target = self.tick + advance
            # The native event step stops at A's public plays, exactly 3 s apart.
            target = min([target] + [tick for tick in (100, 160) if self.tick < tick <= target])
            for action in plays:
                submitted.append((self.tick, action.owner, action.card_id))
                self.outcomes.append(SimpleNamespace(
                    owner=action.owner, data={"action_id": action.action_id}, event_type="action_executed"
                ))
                self.public.append(dict(nativeEventId=action.action_id, kind="card_play", owner=action.owner))
            self.tick = target
            if target in (100, 160):
                self.public.append(dict(nativeEventId=f"A:{target}", kind="card_play", owner=1-waiting_owner))
            observations = {} if kwargs.get("defer_observations") else self.observe_dual()
            return observations, ({0: 1., 1: -1.} if target >= 230 else {}), {"__all__": target >= 230}, {"__all__": False}, {}

    env = Env()

    class Model:
        card_count, cell_count = 8, 576

        def initial_state(self, *a, **kw):
            return None

        def encode(self, batch, *a, **kw):
            return batch

        def select_action(self, context, **kwargs):
            acts = context.owner == waiting_owner and (
                env.tick == 100 or (env.tick == 160 and replacement_acts)
            )
            return replace(_step(act=acts, log_prob=-3.0 if acts else -0.4), action=EventActionV1(
                act=torch.tensor([acts]), card_row=torch.tensor([0 if acts else -1]),
                cell=kwargs["cell_masks"][0, 0].nonzero()[0] if acts else torch.tensor([-1]),
                delay_bin=torch.tensor([1 if env.tick == 100 else 0] if acts else [-1]),
            ))

    class Recorder(EventLaneRecorderV1):
        def decide(self, **kw):
            decisions.append((kw["tick"], kw["owner"], bool(kw["step"].action.act[0])))
            super().decide(**kw)
    monkeypatch.setattr(rollout, "EventLaneRecorderV1", Recorder)
    monkeypatch.setattr(rollout, "canonicalize_event_observation", lambda frame: frame)
    monkeypatch.setattr(rollout, "build_episode_tensorizer_v4", lambda *a, actor_owner, **kw: SimpleNamespace(
        start_episode=lambda *a, **kw: None, tensorize=lambda *a, **kw: frames[actor_owner]
    ))
    coordinator = SimpleNamespace(**{name: lambda *a: None for name in (
        "start_session", "begin_round", "finish_round", "close_session", "abort_round"
    )})
    routed = []
    class Client:
        def __init__(self, historical):
            self.historical = historical
        def infer(self, frame, *, owner, tick, **kwargs):
            routed.append((owner, self.historical))
            return Model().select_action(SimpleNamespace(owner=owner), **kwargs)
    recorder, counters = rollout.play_match(
        environment=env, coordinator=coordinator, model=Model(),
        opponent_model=None if learner_owner is None else Model(),
        learner_owner=0 if learner_owner is None else learner_owner,
        inference_client=Client(False) if shared_inference else None,
        opponent_inference_client=Client(True) if shared_inference and learner_owner is not None else None,
        event_config=SimpleNamespace(board_width=18, board_height=32, delay_offset_ms=(1000, 5000),
                                     bin_to_delay_ms=lambda value: (1000, 5000)[value]),
        catalog=catalog, device=torch.device("cpu"), decks={0: tuple(deck), 1: tuple(deck)},
        match_id="replace-plan", policy_version="test", seed=1, env_id=0,
        temperatures=(1.0,) * 4, max_ticks=250, max_seconds=10,
        max_advance_ticks=30, max_wait_ticks=300, defer_observations=defer_observations,
    )
    recorded = {0, 1} if learner_owner is None else {learner_owner}
    assert {d[1] for d in decisions} == recorded
    if shared_inference:
        assert {o for o, _ in routed} == {0, 1}
        assert all(historical == (o not in recorded) for o, historical in routed)
    if waiting_owner in recorded:
        assert (100, waiting_owner, True) in decisions
        assert (160, waiting_owner, replacement_acts) in decisions
    # A replacement ACT starts its own 1 s delay at tick 160; WAIT clears it.
    # The original 5 s plan at tick 200 must never reach the native queue.
    assert submitted == ([(180, waiting_owner, deck[0])] if replacement_acts else [])
    assert counters["not_ready"] == counters["cancelled_window"] == 0
    for owner in (0, 1):
        assert bool(recorder.rows[owner]) == (owner in recorded)
    assert counters['results'] == {0: 1., 1: -1.}
    assert sum(counters['confirmed_by_owner'].values()) == len(submitted)
    if waiting_owner not in recorded:
        assert not recorder.rows[waiting_owner]
        return
    rows = recorder.rows[waiting_owner]
    assert rows[0].observation_tick == 100
    assert rows[0].execution_status == "cancelled"
    assert rows[0].exclusion_reason == "interrupted"
    assert rows[1].execution_status == ("executed" if replacement_acts else "wait")
    segments = training_segments(rows)
    assert all(row.observation_tick != 100 for _, row in segments)
    assert segments[0][1].observation_tick == 160
