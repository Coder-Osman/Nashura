"""Native timing observations and failure recovery regressions."""
from dataclasses import replace
from collections import deque
from types import SimpleNamespace

import pytest
import torch

from native_runner.battle_env import STANDARD_GAME_MODE
from native_runner.contracts import ActionV1
from native_runner.tests.test_battle_env import _make_env, _reset
from native_runner.tests.test_event_action_space import _catalog, _deck, _environment, _frame, _grid, _placement, _observation
from native_runner.timeline import load_game_mode_timeline
from native_runner.training.event_action_space import legal_event_actions, minimum_playable_delay_ms
from native_runner.training.event_scheduling import EventExecutionLedger
from tools import event_rl_rollout as r


def test_native_precision_and_rate_transition():
    timeline = load_game_mode_timeline(STANDARD_GAME_MODE).timeline
    assert minimum_playable_delay_ms(elixir=1.219, cheapest_cost=3, timeline=timeline, tick=1341) == 5100
    # Ten single-elixir ticks plus ten double-elixir ticks yield exactly
    # 1780+3570=5350 raw. The preceding 100 ms bin yields just 4636.
    assert minimum_playable_delay_ms(elixir=2.465, cheapest_cost=3, timeline=timeline, tick=2390) == 1000
    assert minimum_playable_delay_ms(elixir=1, cheapest_cost=2, timeline=timeline, tick=5990) is None
    assert minimum_playable_delay_ms(elixir=10, cheapest_cost=2, timeline=timeline, tick=6000) is None


def test_masks_exclude_delays_landing_in_finalization_and_non_elixir_slot_blocks():
    catalog, deck = _catalog(), _deck()
    env = _environment({"0": _placement(_grid([(3, 3)]))}, costs=[3])
    obs = _observation([deck[0]])
    obs.tick = 5998
    kwargs = dict(catalog=catalog, card_count=8, cell_count=576, board_width=18, board_height=32,
                  delay_offset_ms=(0, 100, 6000))
    legal = legal_event_actions(env, obs, _frame(catalog, deck), 0, **kwargs)
    assert legal.delay_masks[0].tolist() == [False, False, False]
    assert not legal.act_allowed
    obs.tick = 5999
    assert not legal_event_actions(env, obs, _frame(catalog, deck), 0, **kwargs).act_allowed
    obs.tick = 6050
    assert not legal_event_actions(env, obs, _frame(catalog, deck), 0, **kwargs).act_allowed
    obs.tick = 100
    mask = env.action_mask(0)
    env.action_mask = lambda _: replace(mask, hand_slots=(False,)*4,
        reasons={"slots": {"0": "pending_execution"}})
    assert not legal_event_actions(env, obs, _frame(catalog, deck), 0, **kwargs).act_allowed


def test_gameplay_gate_leaves_result_finalization_independent():
    env, _ = _make_env()
    _reset(env)
    env._raw.update(tick=3599, crownsRaw=[1, 0], ended=False, finalized=False)
    assert env.policy_actions_open
    env._raw['tick'] = 3600
    assert not env.policy_actions_open
    env._raw['crownsRaw'] = [0, 0]
    assert env.policy_actions_open
    env._raw['tick'] = 6000
    assert not env.policy_actions_open
    assert not env._terminated  # Gate closure does not invent an early reward.


def test_ledger_counts_confirmations_and_retains_unresolved_commands():
    ledger = EventExecutionLedger()
    for i in range(4):
        ledger.submit(ActionV1.play(i % 2, 0, (2, 7), action_id=str(i)))
    assert ledger.counters()['executed'] == 0
    outcomes = [SimpleNamespace(owner=i % 2, data={'action_id': str(i)}, event_type=kind)
                for i, kind in enumerate(('action_executed', 'action_rejected', 'action_canceled_terminal'))]
    assert ledger.reconcile(outcomes) == {1}
    assert ledger.counters() == dict(submitted=4, executed=1, confirmed=1, rejected=1,
                                    cancelled_native_terminal=1, unconfirmed=1)
    with pytest.raises(ValueError, match='duplicate'):
        ledger.reconcile(outcomes[:1])


def test_execution_evidence_survives_observation_event_ring_eviction():
    env, _ = _make_env()
    _reset(env)
    env.event_limit = 2
    env._events = deque(maxlen=2)
    env._emit_event(tick=1, event_type='action_executed', owner=0, data={'action_id': 'confirmed'})
    for tick in range(2, 12):
        env._emit_event(tick=tick, event_type='telemetry', owner=0, data={})
    assert not any(e.event_type == 'action_executed' for e in env._events)
    assert [e.data['action_id'] for e in env.action_outcomes()] == ['confirmed']
    _reset(env)
    assert env.action_outcomes() == ()


@pytest.mark.parametrize('failure', ['hand_slot', 'cell', 'missing_card', 'native_rejected'])
def test_cancel_or_native_reject_redecides_only_affected_owner_at_next_tick(monkeypatch, failure):
    catalog, deck = _catalog(), _deck()
    decisions, commands_seen = [], []
    frames = {o: _frame(catalog, deck) for o in (0, 1)}
    for owner, frame in frames.items():
        frame.to_model_input = lambda *a, owner=owner, **k: SimpleNamespace(owner=owner)
    masks = {o: _environment({'0': _placement(_grid([(2, 7)]))}).action_mask(o) for o in (0, 1)}

    class Env:
        episode_config = None
        tick = 0
        outcomes = ()
        rejected = False
        def observations(self):
            return {o: SimpleNamespace(tick=self.tick, time=SimpleNamespace(elixir_multiplier=1.),
                players=[SimpleNamespace(owner=o, hand=[] if failure == 'missing_card' and self.tick == 3 and o == 0
                    else [deck[0]], elixir_exact=10.)]) for o in (0, 1)}
        def reset(self, **kwargs): return self.observations(), {}
        def action_mask(self, owner):
            mask = masks[owner]
            if self.tick == 3 and owner == 0:
                if failure == 'hand_slot':
                    return replace(mask, hand_slots=(False,)*4, reasons={'slots': {'0': 'insufficient_elixir'}})
                if failure == 'cell': return replace(mask, placement_masks={'0': _placement(_grid([]))})
            return mask
        def action_outcomes(self): return self.outcomes
        def deployment_timing_events(self, owner):
            return [{'nativeEventId': 'initial', 'kind': 'card_play'}] if self.tick == 1 and owner == 1 else []
        def step(self, commands, **kwargs):
            self.outcomes = []
            for group in commands.values():
                for action in group:
                    if action.kind.value == 'play_card':
                        commands_seen.append((self.tick, action.owner))
                        reject = failure == 'native_rejected' and not self.rejected
                        self.rejected |= reject
                        self.outcomes.append(SimpleNamespace(owner=action.owner, data={'action_id': action.action_id},
                            event_type='action_rejected' if reject else 'action_executed'))
            self.tick += 1 if self.tick == 0 else min(a.next_decision_ticks or 1 for v in commands.values() for a in v)
            return self.observations(), {}, {'__all__': self.tick >= 10}, {'__all__': False}, {}

    class Model:
        card_count, cell_count = 8, 576
        def initial_state(self, *a, **k): return None
        def encode(self, batch, *a, **k): return batch
        def select_action(self, context, **kwargs):
            return SimpleNamespace(action=SimpleNamespace(act=torch.tensor([context.owner == 0]),
                card_row=torch.tensor([0]), cell=torch.tensor([128]), delay_bin=torch.tensor([0])))

    recorder = SimpleNamespace(decide=lambda **kw: decisions.append((kw['tick'], kw['owner'])),
        cancel=lambda *a, **kw: None, submit=lambda *a, **kw: None, reconcile=lambda *a: None,
        advance=lambda **kw: None, finish=lambda **kw: None, lane_count=lambda owner: 0, results=lambda: {})
    monkeypatch.setattr(r, 'EventLaneRecorderV1', lambda **kw: recorder)
    monkeypatch.setattr(r, 'canonicalize_event_observation', lambda x: x)
    monkeypatch.setattr(r, 'build_episode_tensorizer_v4', lambda *a, actor_owner, **k: SimpleNamespace(
        start_episode=lambda *a, **k: None, tensorize=lambda *a, **k: frames[actor_owner]))
    coordinator = SimpleNamespace(**{k: lambda *a: None for k in ('start_session', 'begin_round', 'finish_round', 'close_session')})
    _, counts = r.play_match(environment=Env(), coordinator=coordinator, model=Model(),
        event_config=SimpleNamespace(board_width=18, board_height=32, delay_offset_ms=(100,), bin_to_delay_ms=lambda _: 100),
        catalog=catalog, device=torch.device('cpu'), decks={0: tuple(deck), 1: tuple(deck)},
        match_id='recovery', policy_version='test', seed=1, env_id=0, temperatures=(1.,)*4,
        max_ticks=400, max_seconds=10, max_advance_ticks=30, max_wait_ticks=300)
    assert decisions == [(1, 0), (1, 1), (4, 0)]
    assert commands_seen[-1] == (6, 0)
    assert counts['recovery'] == 1 and counts['confirmed'] == 1 and counts['unconfirmed'] == 0
    assert counts['rejected'] == int(failure == 'native_rejected')
