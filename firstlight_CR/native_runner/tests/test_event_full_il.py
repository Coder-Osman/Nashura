from copy import deepcopy
from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_event_ability_actions import ability_frame, ability_model, context, masks
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventCardPolicyV1
from native_runner.training.event_full_il import pack, unpack, loss, contract
from native_runner.training.event_ability_il_data import replay_partition


def row(gate=0, tick=100):
    return dict(replay_tag='a'*32, payload_sha256='b'*64, partition=replay_partition('a'*32),
        trusted=True, owner=0, tick=tick, gate=gate, wait_deadline=tick+60,
        card_mask=[True]*8, cell_masks=[[True]*576 for _ in range(8)],
        delay_masks=[[True]*31 for _ in range(8)])


def delayed_model():
    old = ability_model()
    return EventCardPolicyV1(old.backbone, config=replace(old.config, ability_delay=True),
                              event_config=EventPolicyConfigV1.three_second())


def test_joint_skill_delay_sampling_rescores_and_backpropagates():
    model, frame = delayed_model(), ability_frame()
    with torch.no_grad():
        model.ability_gate.bias.fill_(20)
    legal = masks(model); legal['delay_mask'] = legal['delay_mask'][..., :31]
    ctx = context(model, frame)
    sampled = model.select_action(ctx, **legal, sample=True)
    assert sampled.action.ability_candidate.item() == 2
    assert 0 <= sampled.action.delay_bin.item() <= 30
    rescored = model.evaluate_actions(ctx, sampled.action, **legal)
    torch.testing.assert_close(sampled.log_prob, rescored.log_prob)
    assert sampled.delay_log_prob.item() < 0
    (-rescored.log_prob.mean()).backward()
    assert model.delay_head[-1].weight.grad.abs().sum() > 0


def test_delayed_checkpoint_roundtrip_preserves_contract_and_tensors(tmp_path):
    from native_runner.training.event_checkpoint import _build_model, save_event_checkpoint, load_event_checkpoint
    from native_runner.training.v4.config import scaled_model_config_v4
    from native_runner.training.event_model import EventModelConfigV1
    model = _build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
        EventModelConfigV1(ability_actions=True, ability_delay=True, residual_blocks=2, residual_hidden=32),
        EventPolicyConfigV1.three_second())
    path = tmp_path/'new.pt'
    save_event_checkpoint(path, model)
    loaded = load_event_checkpoint(path)
    assert loaded.payload['event_checkpoint_contract']['version'] == 'event-checkpoint.v5'
    assert loaded.model.config.ability_delay
    assert loaded.model.event_config.max_wait_ticks == 60
    for name, value in model.state_dict().items():
        torch.testing.assert_close(value, loaded.model.state_dict()[name], atol=0, rtol=0)


def test_full_inputs_roundtrip_and_cancelled_or_wrong_contract_rejected():
    frame, label = ability_frame(), row()
    evidence = dict(terminal_fidelity=True)
    blob = pack([frame], [label], evidence)
    frames, rows, restored = unpack(blob)
    assert rows == [label] and restored == evidence
    torch.testing.assert_close(frames[0].match_scalars, frame.match_scalars.to(torch.float16), atol=.001, rtol=.001, check_dtype=False)
    with pytest.raises(ValueError, match='contract mismatch'):
        unpack(blob, expected_contract=contract(ability_delay=False))
    label['cancelled'] = True
    with pytest.raises(ValueError, match='cancelled'):
        pack([frame], [label], evidence)


def test_full_il_updates_backbone_and_policy_from_raw_inputs():
    model = delayed_model()
    frame = ability_frame()
    label = row(1)
    label.update(card_row=0, cell=10, delay_bin=2, delay_ms=200, command_tick=104)
    before = model.card_query.weight.detach().clone()
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001)
    value, parts = loss(model, [frame], [label])
    assert torch.isfinite(value)
    value.backward()
    assert any(p.grad is not None and bool(p.grad.abs().sum()) for p in model.backbone.parameters())
    optimizer.step()
    assert not torch.equal(before, model.card_query.weight)


def test_full_skill_label_must_match_exact_receipt_and_original_source():
    model, frame = delayed_model(), ability_frame()
    label = row(2)
    label.update(ability_candidate=2, ability_id='ability-a', delay_bin=10, delay_ms=1000,
                 command_tick=120, receipt=dict(event_type='action_executed', owner=0,
                 entity_id=123, tick=121, data=dict(ability_id='ability-a', kind='activate_ability',
                                                   fair_ability_activation_exact=True)))
    blob = pack([frame], [label], dict(terminal_fidelity=True))
    frames, rows, _ = unpack(blob)
    value, parts = loss(model, frames, rows)
    assert torch.isfinite(value) and parts['delay'].item() > 0
    corrupt = deepcopy(label); corrupt['receipt']['entity_id'] = 456
    with pytest.raises(ValueError, match='exact native'):
        pack([frame], [corrupt], dict(terminal_fidelity=True))


def test_native_replay_interrupt_excludes_old_input_and_recaptures_after_event(monkeypatch):
    from types import SimpleNamespace as NS
    from native_runner.training import event_full_il_producer as producer
    from native_runner.tests.test_event_producer import _play
    from native_runner.contracts import ActionKind
    frame = ability_frame()
    card = int(frame.candidates.native_visible_card_id[0, 0])
    plays = [_play(120, 1, card, 0, (3, 4)), _play(140, 0, card, 0, (3, 4))]
    plays = [replace(t, action=replace(t.action, metadata={'native_observable_tick': t.source_tick+1})) for t in plays]
    commands = [NS(target_tick=t.source_tick+1, owner=t.owner, ability=False) for t in plays]
    def observe(t):
        return {o:NS(tick=t, terminal=NS(ended=t==250, winner=0),
                     players=[NS(owner=0,crowns=1), NS(owner=1,crowns=0)]) for o in (0,1)}
    lane = NS(current_tick=100, source_end_tick=250, observations=observe(100),
              replay_commands=tuple(commands), expert_actions=plays, replay_schedule_sequences=[0,1])
    class Environment:
        native = NS(replay_schedule_status=lambda seq: dict(state='succeeded',queuedAtTick=plays[seq].source_tick))
        def step(self, actions, **kwargs):
            delta = min(a[0].next_decision_ticks for a in actions.values())
            tick = lane.current_tick + delta
            return observe(tick), None, {0:tick==250}, {0:False}, None
    class Tensorizer:
        config=None
        perspective=NS(action_to_model=lambda action: action)
        def tensorize(self, observation, **kwargs):
            copied=deepcopy(frame); copied.match_scalars[0,0]=observation.tick
            return copied
        def record_action(self,*a,**k):pass
        def end_episode(self):pass
    monkeypatch.setattr(producer,'production_semantic_bundle',lambda:NS(card_catalog=None))
    monkeypatch.setattr(producer,'_build_il_lane',lambda *a:lane)
    monkeypatch.setattr(producer,'_initialize_il_lane',lambda *a:None)
    monkeypatch.setattr(producer,'build_expert_action_batch',lambda *a,**k:None)
    monkeypatch.setattr(producer,'legal_event_actions',lambda *a,**k:NS(
        card_mask=torch.ones(8,dtype=torch.bool),cell_masks=torch.ones(8,576,dtype=torch.bool),
        delay_masks=torch.ones(8,31,dtype=torch.bool),row_card_id=torch.tensor([card]+[-1]*7)))
    prepared=NS(replay_tag='a'*32,replay=NS(terminal={'winner':0,'players':[{'owner':0,'crowns':1},{'owner':1,'crowns':0}]}))
    frames, rows, audit = producer.produce_full_observations(Environment(),prepared,[Tensorizer(),Tensorizer()],payload={})
    assert not any(r['owner']==0 and r['tick']==100 for r in rows)
    assert any(r['owner']==0 and r['tick']==100 and r['reason']=='interrupted_before_execution' for r in audit['excluded'])
    assert any(r['owner']==0 and r['tick']==121 and r['gate']==1 for r in rows)
    for owner in (0,1):
        assert any(r['owner']==owner and r['tick']==201 and r['trigger']=='wait_timeout' for r in rows)
    for row, snapshot in zip(rows,frames):
        assert float(snapshot.match_scalars[0,0]) == row['tick']
