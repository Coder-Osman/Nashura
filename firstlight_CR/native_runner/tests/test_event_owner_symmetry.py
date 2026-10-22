"""Event model coordinates must never leak into native plans or replay."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import ActionV1
from native_runner.tests.test_event_action_space import (
    _catalog, _deck, _environment, _frame, _grid, _placement,
)
from native_runner.training.event_action_space import event_cell_to_native, legal_event_actions
from native_runner.training.event_spell_constraints import (
    LIGHTNING_CARD_ID, LOG_CARD_ID, LightningTargetConstraintV1, LogTargetConstraintV1,
)
from native_runner.training.v4.catalog import CardCatalogV1
from tools.demo_event_selfplay import Decoder, legal_play_options
from tools import event_rl_rollout


def _scene(owner, cells):
    environment = _environment({"0": _placement(_grid(cells))})
    observation = SimpleNamespace(
        tick=1,
        players=[SimpleNamespace(owner=owner, hand=[_deck()[0]], elixir_exact=10.0)],
        time=SimpleNamespace(elixir_multiplier=1.0),
    )
    return environment, observation


def _legal(environment, observation, owner):
    return legal_event_actions(
        environment, observation, _frame(_catalog(), _deck()), owner,
        catalog=_catalog(), card_count=8, cell_count=576,
        board_width=18, board_height=32, delay_offset_ms=(0, 100),
    )


def test_mirrored_native_masks_are_exactly_equal_in_model_coordinates():
    cells = [(0, 0), (2, 7), (16, 29), (17, 31)]
    env0, obs0 = _scene(0, cells)
    env1, obs1 = _scene(1, [(17 - x, 31 - y) for x, y in cells])
    before = env1.action_mask(1).to_dict()
    legal0, legal1 = _legal(env0, obs0, 0), _legal(env1, obs1, 1)
    for field in ("cell_masks", "card_mask", "delay_masks", "hand_index", "row_card_id"):
        assert torch.equal(getattr(legal0, field), getattr(legal1, field))
    assert legal1.cell_masks[0].nonzero().flatten().tolist() == sorted(y * 18 + x for x, y in cells)
    assert env1.action_mask(1).to_dict() == before
    assert legal_play_options(env0, obs0, 0)[0] == legal_play_options(env1, obs1, 1)[0]


@pytest.mark.parametrize("owner", [0, 1])
def test_demo_sparse_hand_uses_native_slot(owner):
    env, observation = _scene(owner, [(2, 7)])
    card = observation.players[0].hand[0]
    observation.players[0].metadata = {"hand_slot_by_card": {str(card): 3}, "native_hand_capacity": 4}
    old = env.action_mask(owner)
    mask = replace(old, hand_slots=(False, False, False, True),
                   placement_masks={"3": old.placement_masks["0"]})
    env.action_mask = lambda _: mask
    options, _ = legal_play_options(env, observation, owner)
    assert options[card][0] == 3


@pytest.mark.parametrize("owner", [0, 1])
def test_every_sampled_model_cell_maps_back_to_native(owner):
    for index in range(576):
        cell = event_cell_to_native(index, owner=owner, board_width=18)
        expected = (index % 18, index // 18) if owner == 0 else (17 - index % 18, 31 - index // 18)
        assert cell == expected


@pytest.mark.parametrize("owner,native_cell", [(0, (2, 7)), (1, (15, 24))])
@pytest.mark.parametrize("bins", [(0, 50), (0, 50, 100)])
def test_demo_conditions_on_model_cell_and_returns_native_replay_cell(owner, native_cell, bins):
    env, observation = _scene(owner, [native_cell])
    frame = _frame(_catalog(), _deck())
    frame.to_model_input = lambda *args, **kwargs: SimpleNamespace(batch_size=1)
    conditioned = []

    class Model:
        def initial_state(self, *args, **kwargs):
            return None

        def encode(self, *args, **kwargs):
            return None

        def heads(self, context, **kwargs):
            if "cell" in kwargs:
                conditioned.append(int(kwargs["cell"][0]))
            return SimpleNamespace(
                gate_logits=torch.tensor([[0.0, 1.0]]), card_logits=torch.zeros(1, 8),
                cell_logits=torch.zeros(1, 576), delay_logits=torch.tensor([[10000.0, 1000.0, 0.0][:len(bins)]]),
            )

    decoder = Decoder(Model(), _catalog(), SimpleNamespace(board_width=18, delay_offset_ms=bins), "cpu")
    options, reason = legal_play_options(env, observation, owner)
    plan = decoder(frame, observation, owner, options, reason)
    assert conditioned == [7 * 18 + 2]
    if max(bins) < 100:
        assert plan is None
        return
    assert plan["delay_ms"] == 100
    assert plan["cell"] == native_cell
    assert event_rl_rollout._cell_is_legal(env.action_mask(owner), 0, plan["cell"])
    replay_action = ActionV1.play(owner, plan["hand_index"], plan["cell"], card_id=plan["card_id"])
    assert tuple(replay_action.to_dict()["target_grid"]) == native_cell


@pytest.mark.parametrize("blocked_owner", [0, 1])
@pytest.mark.parametrize("closed", [None, "hand_slot", "all_slots", "play_kind", "moved_slot", "lightning_target", "log_target"])
def test_rollout_records_model_cells_but_validates_and_executes_native(monkeypatch, blocked_owner, closed):
    model_index = 7 * 18 + 2
    native_cells = {0: (2, 7), 1: (15, 24)}
    scenes = {owner: _scene(owner, [native_cells[owner]]) for owner in (0, 1)}
    catalog, deck = _catalog(), _deck()
    constraint = None
    log_constraint = None
    if closed in {"lightning_target", "log_target"}:
        constraint = LightningTargetConstraintV1() if closed == "lightning_target" else None
        log_constraint = LogTargetConstraintV1() if closed == "log_target" else None
        card_id = LIGHTNING_CARD_ID if closed == "lightning_target" else LOG_CARD_ID
        deck = [card_id, *deck[1:]]
        catalog = CardCatalogV1.from_raw_ids(deck)
        for owner, (_, observation) in scenes.items():
            x, y = native_cells[owner]
            observation.players[0].hand = [card_id]
            observation.entities = [SimpleNamespace(
                owner=1 - owner, hitpoints=100, position=(x * 1000 + 500, y * 1000 + 500),
                entity_kind="troop", visibility_state=None,
                native_data_global_id=34_000_010,
            )]
            observation.towers = []
    frame = _frame(catalog, deck)
    frame.to_model_input = lambda *args, **kwargs: None
    recorded = []
    submitted = []
    advances = []

    class Recorder:
        cancel = submit = reconcile = lambda *a, **kw: None
        def __init__(self, **kwargs):
            pass

        def decide(self, **kwargs):
            recorded.append(kwargs)

        def advance(self, **kwargs):
            advances.append(kwargs)

        def finish(self, **kwargs):
            pass

        def lane_count(self, owner):
            return 1

        def results(self):
            return {}

    class Environment:
        episode_config = None
        tick = 0

        def action_outcomes(self):
            return self.outcomes

        def reset(self, **kwargs):
            return {owner: scene[1] for owner, scene in scenes.items()}, {}

        def action_mask(self, owner):
            mask = scenes[owner][0].action_mask(owner)
            if self.tick >= 2 and owner == blocked_owner:
                if closed == "hand_slot":
                    return replace(mask, hand_slots=(False, True, True, True),
                                   reasons={"0": "insufficient_elixir"})
                if closed == "all_slots":
                    return replace(mask, hand_slots=(False,) * 4,
                                   kinds={"wait": True, "play_card": False})
                if closed == "play_kind":
                    return replace(mask, kinds={"wait": True, "play_card": False})
                if closed == "moved_slot":
                    return replace(mask, hand_slots=(True, False, True, True),
                                   placement_masks={"1": mask.placement_masks["0"]},
                                   reasons={"1": "pending_execution"})
            return mask

        def deployment_timing_events(self, owner):
            return [{"nativeEventId": "initial", "kind": "card_play"}] if self.tick == 1 else []

        def step(self, commands, **kwargs):
            self.outcomes = [SimpleNamespace(event_type="action_executed", owner=a.owner,
                data={"action_id": a.action_id}) for group in commands.values() for a in group
                if a.target_grid is not None]
            for actions in commands.values():
                for action in actions:
                    if action.target_grid is not None:
                        mask = self.action_mask(action.owner)
                        assert mask.kinds["play_card"], "engine rejects masked play kind"
                        assert mask.hand_slots[action.hand_slot], "engine rejects masked hand slot"
            self.tick += 1
            submitted.extend(action for actions in commands.values() for action in actions
                             if action.target_grid is not None)
            observations = {owner: scene[1] for owner, scene in scenes.items()}
            for observation in observations.values():
                observation.tick = self.tick + 1
            if self.tick == 2 and closed == "moved_slot":
                scenes[blocked_owner][1].players[0].hand = [deck[1], deck[0]]
            if self.tick == 2 and closed in {"lightning_target", "log_target"}:
                scenes[blocked_owner][1].entities = []
            return observations, {}, {"__all__": self.tick == 4}, {"__all__": False}, {}

    class Model:
        card_count = 8
        cell_count = 576

        def initial_state(self, *args, **kwargs):
            return None

        def encode(self, *args, **kwargs):
            return None

        def select_action(self, context, **kwargs):
            assert kwargs["cell_masks"][0, 0].nonzero().flatten().tolist() == [model_index]
            return SimpleNamespace(action=SimpleNamespace(
                act=torch.tensor([True]), card_row=torch.tensor([0]),
                cell=torch.tensor([model_index]), delay_bin=torch.tensor([0]),
            ))

    monkeypatch.setattr(event_rl_rollout, "EventLaneRecorderV1", Recorder)
    monkeypatch.setattr(event_rl_rollout, "canonicalize_event_observation", lambda value: value)
    monkeypatch.setattr(event_rl_rollout, "build_episode_tensorizer_v4", lambda *args, **kwargs:
                        SimpleNamespace(start_episode=lambda *args, **kwargs: None,
                                        tensorize=lambda *args, **kwargs: frame))
    coordinator = SimpleNamespace(**{name: lambda *args: None for name in
                                    ("start_session", "begin_round", "finish_round", "close_session")})
    config = SimpleNamespace(board_width=18, board_height=32, delay_offset_ms=(100,), bin_to_delay_ms=lambda _: 100)
    _, counters = event_rl_rollout.play_match(
        environment=Environment(), coordinator=coordinator, model=Model(), event_config=config,
        catalog=catalog, device=torch.device("cpu"), decks={0: tuple(deck), 1: tuple(deck)},
        match_id="symmetry", policy_version="test", seed=1, env_id=0,
        temperatures=(1.0,) * 4, max_ticks=10, max_seconds=10, max_advance_ticks=1, max_wait_ticks=10,
        lightning_constraint=constraint,
        log_constraint=log_constraint,
    )
    assert len(recorded) == 2
    assert all(int(row["step"].action.cell[0]) == model_index for row in recorded)
    assert all(bool(row["step"].action.act[0]) for row in recorded)
    assert counters["terminal"]
    assert advances[-1]["done"] and sum(item["ticks"] for item in advances) == 4
    if closed is not None:
        assert {action.owner for action in submitted} == {1 - blocked_owner}
        counter = (f"cancelled_{closed}" if closed in {"lightning_target", "log_target"} else
                   "cancelled_play_kind" if closed == "play_kind" else "cancelled_hand_slot")
        assert counters[counter] == counters["rows_without_execution"] == 1
        if closed not in {"lightning_target", "log_target"}:
            assert counters["execution_mask_samples"][0]["owner"] == blocked_owner
        if closed == "moved_slot":
            assert counters["execution_mask_samples"][0]["slot_now"] == 1
    else:
        assert {action.owner for action in submitted} == {0, 1}
        assert counters["rows_without_execution"] == 0
    assert submitted
    for action in submitted:
        assert tuple(action.target_grid) == native_cells[action.owner]
        assert tuple(action.to_dict()["target_grid"]) == native_cells[action.owner]
