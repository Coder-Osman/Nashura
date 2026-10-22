"""Exercise event scheduling against real BattleEnv decision deadlines."""

from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import ActionV1
from native_runner.semantic_subset import SEMANTIC_BASELINE_DECK
from native_runner.tests.test_battle_env import _make_env, _reset
from native_runner.tests.test_event_action_space import _frame
from native_runner.training.event_scheduling import complete_event_commands
from native_runner.training.v4.catalog import CardCatalogV1
from tools import event_rl_rollout


def playable(env, owner):
    mask = env.action_mask(owner)
    slot = next(i for i, legal in enumerate(mask.hand_slots) if legal)
    rows = mask.placement_masks[str(slot)]["row_major"]
    cell = next((x, y) for y, row in enumerate(rows) for x, legal in enumerate(row) if legal)
    return ActionV1.play(owner, slot, cell, execute_offset_ticks=1)


@pytest.mark.parametrize("first_owner", [0, 1])
def test_alternating_plays_leave_both_owners_ready(first_owner):
    env, _ = _make_env()
    _reset(env)
    for owner in (first_owner, 1 - first_owner):
        before = int(env.raw_observation["tick"])
        _, _, _, _, infos = env.step(complete_event_commands({owner: (playable(env, owner),)}))
        assert int(env.raw_observation["tick"]) == before + 1
        assert all(infos[o]["decision_ready"] for o in (0, 1))
        assert all(env.action_mask(o).reasons["decision_ready"] for o in (0, 1))


def test_explicit_independent_waits_are_preserved():
    env, _ = _make_env()
    _reset(env)
    commands = {0: (ActionV1.wait(0, ticks=5),), 1: (ActionV1.wait(1, ticks=2),)}
    _, _, _, _, infos = env.step(complete_event_commands(commands))
    assert not infos[0]["decision_ready"]
    assert infos[1]["decision_ready"]
    env.step(complete_event_commands({1: (ActionV1.wait(1, ticks=2),)}))
    assert not env.action_mask(0).reasons["decision_ready"]
    env.step(complete_event_commands({}))
    assert env.action_mask(0).reasons["decision_ready"]


@pytest.mark.parametrize("fast_owner", [0, 1])
def test_rollout_keeps_opponent_ready_at_one_sided_play(monkeypatch, fast_owner):
    env, _ = _make_env()
    catalog = CardCatalogV1.from_raw_ids(SEMANTIC_BASELINE_DECK)
    frames = {}
    for owner in (0, 1):
        frame = _frame(catalog, list(SEMANTIC_BASELINE_DECK))
        frame.to_model_input = lambda *args, owner=owner, **kwargs: SimpleNamespace(owner=owner)
        frames[owner] = frame
    monkeypatch.setattr(event_rl_rollout, "canonicalize_event_observation", lambda x: x)
    monkeypatch.setattr(event_rl_rollout, "build_episode_tensorizer_v4",
                        lambda *args, actor_owner, **kwargs: SimpleNamespace(
                            start_episode=lambda *a, **k: None,
                            tensorize=lambda *a, **k: frames[actor_owner]))
    recorder = SimpleNamespace(decide=lambda **k: None, advance=lambda **k: None,
                               cancel=lambda *a, **k: None, submit=lambda *a, **k: None, reconcile=lambda *a: None,
                               finish=lambda **k: None, lane_count=lambda owner: 0, results=lambda: {})
    monkeypatch.setattr(event_rl_rollout, "EventLaneRecorderV1", lambda **k: recorder)
    stepped = []
    real_step = env.step

    def step(commands, **kwargs):
        result = real_step(commands, **kwargs)
        if any(a.kind.value == "play_card" for group in commands.values() for a in group):
            stepped.append(int(env.raw_observation["tick"]))
            assert all(env.action_mask(o).reasons["decision_ready"] for o in (0, 1))
        return result

    monkeypatch.setattr(env, "step", step)

    class Model:
        card_count, cell_count = 8, 576

        def initial_state(self, *a, **k):
            return None

        def encode(self, batch, *a, **k):
            return batch

        def select_action(self, context, **kwargs):
            row = int(kwargs["card_mask"][0].nonzero()[0])
            cell = int(kwargs["cell_masks"][0, row].nonzero()[0])
            return SimpleNamespace(action=SimpleNamespace(
                act=torch.tensor([True]), card_row=torch.tensor([row]),
                cell=torch.tensor([cell]), delay_bin=torch.tensor([0 if context.owner == fast_owner else 1])))

    coordinator = SimpleNamespace(**{name: lambda *a: None for name in
                                    ("start_session", "begin_round", "finish_round", "close_session", "abort_round")})
    _, counters = event_rl_rollout.play_match(
        environment=env, coordinator=coordinator, model=Model(),
        event_config=SimpleNamespace(board_width=18, board_height=32, delay_offset_ms=(0, 100),
                                     bin_to_delay_ms=lambda value: (0, 100)[value]),
        catalog=catalog, device=torch.device("cpu"),
        decks={0: tuple(SEMANTIC_BASELINE_DECK), 1: tuple(SEMANTIC_BASELINE_DECK)},
        match_id="interleaved", policy_version="test", seed=7, env_id=0,
        temperatures=(1.,) * 4, max_ticks=96, max_seconds=10,
        max_advance_ticks=30, max_wait_ticks=3)
    assert stepped
    assert counters["cancelled_window"] == counters["not_ready"] == 0
