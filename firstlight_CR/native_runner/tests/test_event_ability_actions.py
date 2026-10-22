"""Event skills: exact source, policy likelihood, execution credit and resume."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import ActionKind, ActionV1
from native_runner.training.event_abilities import candidate_ability_mask, ability_plan
from native_runner.training.event_model import EventCardPolicyV1, EventModelConfigV1
from native_runner.training.event_ppo import EventLaneRolloutV1, EventPpoConfigV1, event_ppo_update
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.training.event_rollout import EventLaneRecorderV1, lanes_from_shard
from native_runner.training.event_cache import load_event_rl_shard
from native_runner.training.event_scheduling import EventExecutionLedger
from native_runner.training.event_preflight import preflight_event_rollout
from native_runner.training.v4.tensors import CANDIDATE_ABILITY, TARGET_NONE
from native_runner.tests.test_training_v4_model import _batch, _model, _ability_catalog, _catalog


def ability_frame(n=1):
    frame = _batch(n)
    c = frame.candidates
    c.mask[:, 2] = True
    c.variant[:, 2] = CANDIDATE_ABILITY
    c.target_mode[:, 2] = TARGET_NONE
    c.native_source_entity[:, 2] = 123
    c.ability_vocab_id[:, 2] = 1
    c.own_card_row[:, 2] = 0
    c.cost[:, 2] = 1.
    c.elixir[:] = 5.
    return frame


def ability_model():
    model = EventCardPolicyV1(_model(), config=EventModelConfigV1(ability_actions=True)).eval()
    with torch.no_grad():
        model.ability_gate.bias.fill_(20.)
    return model


def masks(model, n=1):
    return dict(gate_mask=torch.tensor([[True, False]] * n),
                card_mask=torch.zeros(n, model.card_count, dtype=torch.bool),
                cell_masks=torch.zeros(n, model.card_count, model.cell_count, dtype=torch.bool),
                delay_mask=torch.zeros(n, model.card_count, 61, dtype=torch.bool))


def context(model, frame):
    return model.encode(frame.to_model_input("cpu", float_dtype=torch.float32), model.initial_state(frame.batch_size))


def test_skill_is_a_separate_exact_source_event_without_card_or_delay():
    model, frame = ability_model(), ability_frame()
    ctx = context(model, frame)
    step = model.select_action(ctx, **masks(model), sample=False)
    assert step.action.gate_choice.tolist() == [2]
    assert step.action.ability_candidate.tolist() == [2]
    assert step.action.card_row.tolist() == step.action.cell.tolist() == step.action.delay_bin.tolist() == [-1]
    assert float((step.card_log_prob + step.cell_log_prob + step.delay_log_prob).detach()) == 0
    rescored = model.evaluate_actions(ctx, step.action, **masks(model))
    torch.testing.assert_close(rescored.log_prob, step.log_prob, atol=0, rtol=0)
    assert float(torch.exp(rescored.log_prob - step.log_prob).detach()) == 1.
    plan = ability_plan(frame, 2, owner=1, tick=300, ability_catalog=_ability_catalog(_catalog()))
    assert plan["source_entity"] == 123 and plan["ability_id"] == "ability-a"
    assert plan["scheduled_tick"] == 300 and plan["kind"] == "activate_ability"


@pytest.mark.parametrize("invalid", ["cost", "identity", "mask", "target", "padding"])
def test_ineligible_ability_never_becomes_an_action(invalid):
    model, frame = ability_model(), ability_frame()
    c = frame.candidates
    if invalid == "cost": c.cost[:, 2] = 6.
    if invalid == "identity": c.native_source_entity[:, 2] = -1
    if invalid == "mask": c.mask[:, 2] = False
    if invalid == "target": c.target_mode[:, 2] = 1
    if invalid == "padding": c.ability_vocab_id[:, 2] = 0
    assert not bool(candidate_ability_mask(frame).any())
    step = model.select_action(context(model, frame), **masks(model), sample=False)
    assert step.action.gate_choice.tolist() == [0]


@pytest.mark.parametrize("prepared", [False, True])
def test_skill_ppo_ratio_and_reference_kl_and_gradients(prepared):
    model, frame = ability_model(), ability_frame()
    # Permit exploration so there is a nonzero gate gradient.
    with torch.no_grad(): model.ability_gate.bias.fill_(0.)
    step = model.select_action(context(model, frame), **masks(model), sample=False)
    assert step.action.ability_candidate.item() == 2
    lane = EventLaneRolloutV1(observations=(frame,), actions=step.action,
        old_log_prob=step.log_prob.detach(), reward=torch.tensor([1.]),
        done=torch.tensor([True]), truncated=torch.tensor([False]),
        temperatures=step.temperatures, **masks(model))
    old = model.ability_gate.bias.detach().clone()
    stats = event_ppo_update(model, [lane], torch.optim.SGD(model.parameters(), lr=.01),
        config=EventPpoConfigV1(normalize_advantage=False, value_coefficient=0.,
            entropy_coefficient=0., kl_coefficient=.1, minibatch_lanes=0),
        reference_model=model,
        prepared=PreparedEventRollout(model, [lane]) if prepared else None)
    assert stats.ratio_mean == pytest.approx(1., abs=1e-5)
    assert not torch.equal(old, model.ability_gate.bias)


def test_native_receipt_opens_one_skill_event_and_rejection_does_not():
    ledger = EventExecutionLedger()
    action = ActionV1(owner=0, kind=ActionKind.ACTIVATE_ABILITY, source_entity=123,
                      ability_id="ability-a", action_id="skill-1")
    ledger.submit(action)
    assert ledger.ability_events == []
    event = SimpleNamespace(owner=0, entity_id=123, tick=401, event_type="action_executed",
        data=dict(action_id="skill-1", kind="activate_ability", ability_id="ability-a",
                  fair_ability_activation_exact=True))
    ledger.reconcile([event])
    assert ledger.ability_events == [event]
    ledger.reconcile([])
    assert ledger.ability_events == [] and ledger.confirmed_abilities == 1
    with pytest.raises(ValueError, match="duplicate"): ledger.reconcile([event])
    ledger.submit(replace(action, action_id="skill-2"))
    assert ledger.reconcile([SimpleNamespace(owner=0, event_type="action_rejected",
        data={"action_id": "skill-2"})]) == {0}
    assert ledger.ability_events == []


def test_skill_receipt_rejects_different_source():
    ledger = EventExecutionLedger()
    ledger.submit(ActionV1(owner=0, kind=ActionKind.ACTIVATE_ABILITY, source_entity=123,
                          ability_id="ability-a", action_id="skill-1"))
    with pytest.raises(ValueError, match="exact source"):
        ledger.reconcile([SimpleNamespace(owner=0, entity_id=124, event_type="action_executed",
            data=dict(action_id="skill-1", kind="activate_ability", ability_id="ability-a",
                      fair_ability_activation_exact=True))])


@pytest.mark.parametrize("executed", [True, False])
def test_skill_shard_round_trip_and_cancel_filter(tmp_path, executed):
    model, frame = ability_model(), ability_frame()
    with torch.inference_mode():
        step = model.select_action(context(model, frame), **masks(model), sample=False)
    recorder = EventLaneRecorderV1(match_id="skill", policy_version="v4",
        temperatures=step.temperatures, decks={0: tuple(range(1,9)), 1: tuple(range(9,17))})
    recorder.decide(owner=0, tick=100, frame=frame, step=step, **masks(model))
    if executed:
        recorder.submit(0, tick=100, action_id="skill-1")
        recorder.reconcile([SimpleNamespace(owner=0, entity_id=123, event_type="action_executed",
            data=dict(action_id="skill-1", kind="activate_ability", fair_ability_activation_exact=True))])
    else:
        recorder.cancel(0, reason="ability_no_longer_legal")
    recorder.advance(ticks=1, rewards={0:1.}, done=True)
    target = tmp_path / "skill.crev.zst"
    recorder.to_shard(target, shard_id="skill", contract={})
    shard = load_event_rl_shard(tmp_path / "skill.json")
    row = shard.records[0]
    assert row.ability_candidate == 2 and row.ability_source_entity == 123
    lanes = lanes_from_shard(shard)
    assert len(lanes) == int(executed)
    if executed:
        replay = model.evaluate_actions(context(model, lanes[0].observations[0]), lanes[0].actions, **masks(model))
        torch.testing.assert_close(replay.log_prob, lanes[0].old_log_prob, atol=2e-4, rtol=0)
        metadata = [[dict(old_head_log_probs={
            head: getattr(row, f"{head}_log_prob")
            for head in ("gate", "card", "cell", "delay", "ability")})]]
        preflight = preflight_event_rollout(model, lanes, row_metadata=metadata)
        assert preflight.passed
        assert preflight.diagnostics["heads"]["ability"]["available_rows"] == 1
        assert "ability" in preflight.diagnostics["joint"]["top_rows"][0]["heads"]


def test_ability_microbatch_padding_and_order_preserve_likelihood():
    from dataclasses import fields
    from native_runner.training.event_inference_service import infer_batch
    model = ability_model()
    requests = []
    for count in (3, 5):
        frame = ability_frame()
        frame.candidates = replace(frame.candidates, **{
            f.name: getattr(frame.candidates, f.name)[:, :count]
            for f in fields(frame.candidates)
            if isinstance(getattr(frame.candidates, f.name), torch.Tensor)
            and getattr(frame.candidates, f.name).ndim >= 2})
        requests.append(dict(frame=frame, **masks(model), temperatures=(1.,)*4,
                             uniforms=torch.full((1, 4), .5, dtype=torch.float64)))
    batch = infer_batch(model, requests)
    reverse = infer_batch(model, requests[::-1])[::-1]
    for request, actual, reordered in zip(requests, batch, reverse):
        single = infer_batch(model, [request])[0]
        assert actual.action.ability_candidate.item() == single.action.ability_candidate.item() == 2
        torch.testing.assert_close(actual.log_prob, single.log_prob, atol=2e-5, rtol=2e-5)
        torch.testing.assert_close(actual.log_prob, reordered.log_prob, atol=2e-5, rtol=2e-5)


def test_demo_decoder_uses_the_same_skill_action_branch():
    from tools.demo_event_selfplay import Decoder
    model, frame = ability_model(), ability_frame()
    m = masks(model)
    legal = SimpleNamespace(act_allowed=False, card_mask=m["card_mask"][0],
                            cell_masks=m["cell_masks"][0], delay_masks=m["delay_mask"][0])
    decoder = Decoder(model, model.backbone.catalog, model.event_config, "cpu")
    plan = decoder(frame, SimpleNamespace(tick=100, players=[]), 0, {}, legal=legal)
    assert plan["kind"] == "activate_ability" and plan["source_entity"] == 123
    assert decoder.stats["abilities"] == 1


@pytest.mark.parametrize("skill_owner", [0, 1])
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("defer_observations", [False, True])
def test_collector_skill_receipt_wakes_both_sides_and_interrupts_pending_play(
        monkeypatch, skill_owner, accepted, defer_observations):
    from tools import event_rl_rollout as r
    from native_runner.tests.test_event_rollout import _step
    from native_runner.training.event_model import EventActionV1
    from native_runner.training.event_config import EventPolicyConfigV1
    frames = {owner: ability_frame() for owner in (0, 1)}
    decisions, submitted = [], []
    deck = tuple(_catalog().raw_card_ids[:8])

    class Env:
        episode_config = None
        tick = 0
        outcomes = ()

        @property
        def current_tick(self): return self.tick

        def observe_dual(self, **kwargs):
            return {o: SimpleNamespace(owner=o, tick=self.tick, players=[SimpleNamespace(
                owner=o, hand=[deck[0]], deck=deck, elixir_exact=5.)]) for o in (0, 1)}

        def reset(self, **kwargs): return self.observe_dual(), {}

        def action_mask(self, owner):
            return SimpleNamespace(reasons={"decision_ready": True},
                kinds={"activate_ability": self.tick == 1}, ability_sources=(123,),
                hand_slots=(True,), placement_masks={})

        def action_outcomes(self): return self.outcomes

        def deployment_timing_events(self, owner):
            return [dict(nativeEventId="initial", kind="card_play")] if self.tick == 1 and owner == 0 else []

        def step(self, commands, **kwargs):
            self.outcomes = []
            for actions in commands.values():
                for action in actions:
                    if action.kind == ActionKind.WAIT: continue
                    submitted.append(action)
                    self.outcomes.append(SimpleNamespace(owner=action.owner, entity_id=action.source_entity,
                        tick=self.tick+1, event_type="action_executed" if accepted else "action_rejected",
                        data=dict(action_id=action.action_id, kind=action.kind.value,
                                  ability_id=action.ability_id, fair_ability_activation_exact=True)))
            self.tick += 1
            return ({} if kwargs.get("defer_observations") else self.observe_dual()), \
                ({0:1., 1:-1.} if self.tick >= 4 else {}), {"__all__": self.tick >= 4}, {"__all__": False}, {}

    env = Env()

    class Model:
        config = EventModelConfigV1(ability_actions=True)
        card_count, cell_count = 8, 576
        def initial_state(self, *a, **kw): return None
        def encode(self, batch, *a, **kw): return batch
        def select_action(self, batch, **kw):
            owner = next(o for o, frame in frames.items() if frame.match_scalars.data_ptr() == batch.match_scalars.data_ptr())
            skill = env.tick == 1 and owner == skill_owner
            play = env.tick == 1 and owner != skill_owner
            step = _step(act=skill or play)
            return replace(step, action=EventActionV1(
                act=torch.tensor([skill or play]), ability_candidate=torch.tensor([2 if skill else -1]),
                card_row=torch.tensor([0 if play else -1]), cell=torch.tensor([0 if play else -1]),
                delay_bin=torch.tensor([60 if play else -1])),
                card_log_prob=torch.zeros(1), cell_log_prob=torch.zeros(1), delay_log_prob=torch.zeros(1),
                card_entropy=torch.zeros(1), cell_entropy=torch.zeros(1), delay_entropy=torch.zeros(1))

    class Recorder(EventLaneRecorderV1):
        def decide(self, **kwargs):
            decisions.append((kwargs["tick"], kwargs["owner"]))
            super().decide(**kwargs)

    monkeypatch.setattr(r, "EventLaneRecorderV1", Recorder)
    monkeypatch.setattr(r, "canonicalize_event_observation", lambda frame: frame)
    monkeypatch.setattr(r, "production_semantic_bundle", lambda: SimpleNamespace(ability_catalog=_ability_catalog(_catalog())))
    monkeypatch.setattr(r, "build_episode_tensorizer_v4", lambda *a, actor_owner, **kw: SimpleNamespace(
        start_episode=lambda *a, **kw: None, tensorize=lambda *a, **kw: frames[actor_owner]))
    # Native runtime binding itself is covered separately; preserve the real
    # plan/execution/receipt/recorder/scheduler path around this exact identity.
    from native_runner.training.v4 import native_actions
    monkeypatch.setattr(native_actions, "ability_runtime_contract", lambda *a, **kw: SimpleNamespace(ability_id="ability-a"))
    monkeypatch.setattr(r, "legal_event_actions", lambda *a, **kw: SimpleNamespace(
        act_allowed=True, playable_now=True, decision_ready=True, legal_rows=(0,),
        card_mask=torch.ones(8, dtype=torch.bool), cell_masks=torch.ones(8,576,dtype=torch.bool),
        delay_masks=torch.ones(8,61,dtype=torch.bool), hand_index=torch.zeros(8,dtype=torch.long),
        row_card_id=torch.tensor(list(deck))))
    coordinator = SimpleNamespace(**{name: lambda *a: None for name in (
        "start_session", "begin_round", "finish_round", "close_session", "abort_round")})
    recorder, counts = r.play_match(environment=env, coordinator=coordinator, model=Model(),
        event_config=EventPolicyConfigV1(), catalog=_catalog(), device=torch.device("cpu"),
        decks={0:deck,1:deck}, match_id="skill-event", policy_version="test", seed=1, env_id=0,
        temperatures=(1.,)*4, max_ticks=10, max_seconds=10, max_advance_ticks=1, max_wait_ticks=300,
        defer_observations=defer_observations)
    assert len(submitted) == 1 and submitted[0].kind == ActionKind.ACTIVATE_ABILITY
    assert submitted[0].source_entity == 123
    assert decisions == [(1,0), (1,1)] + ([(2,0),(2,1)] if accepted else [(2,skill_owner)])
    assert counts["ability_events"] == counts["confirmed_abilities"] == int(accepted)
    assert recorder.rows[skill_owner][0].execution_status == ("executed" if accepted else "cancelled")
    if not accepted:
        assert recorder.rows[skill_owner][0].exclusion_reason == "action_rejected"
    other = recorder.rows[1-skill_owner][0]
    if accepted:
        assert other.execution_status == "cancelled" and other.exclusion_reason == "interrupted"


def test_no_ability_candidate_preserves_card_only_policy_exactly():
    source = EventCardPolicyV1(_model()).eval()
    target = EventCardPolicyV1(_model(), config=EventModelConfigV1(ability_actions=True)).eval()
    target.load_state_dict(source.state_dict(), strict=False)
    frame = ability_frame()
    frame.candidates.mask[:, 2] = False
    legal = dict(gate_mask=torch.tensor([[True, True]]),
        card_mask=torch.ones(1,8,dtype=torch.bool), cell_masks=torch.ones(1,8,576,dtype=torch.bool),
        delay_mask=torch.ones(1,8,61,dtype=torch.bool), sample_uniforms=torch.tensor([[.8,.3,.4,.6]]))
    old = source.select_action(context(source, frame), **legal)
    new = target.select_action(context(target, frame), **legal)
    for name in ("act", "card_row", "cell", "delay_bin"):
        torch.testing.assert_close(getattr(old.action, name), getattr(new.action, name), atol=0, rtol=0)
    torch.testing.assert_close(old.log_prob, new.log_prob, atol=0, rtol=0)
