"""End-to-end checkpoint preservation with a deliberately synthetic IL fixture."""
from dataclasses import replace
from types import SimpleNamespace

import torch

from native_runner.training import event_checkpoint as cp
from native_runner.training.event_ability_il import AbilityILFeatures, frozen_state_digests, training_state_digest
from native_runner.training.event_ability_il_data import replay_partition, write_features
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4
from native_runner.tests.test_event_ability_il_data import row
from tools.event_rl_update import restore_optimizer, optimizer_checkpoint
from tools.train_event_ability_heads import train


def test_training_cli_saves_new_checkpoint_preserves_ppo_and_every_old_tensor(tmp_path):
    torch.manual_seed(8)
    model = cp._build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                           EventModelConfigV1(), EventPolicyConfigV1())
    optimizer, progress = restore_optimizer(model, {})
    model.card_query.weight.grad = torch.ones_like(model.card_query.weight)
    optimizer.step()
    saved = optimizer_checkpoint(model, optimizer, progress, SimpleNamespace(optimizer_steps=1, steps=17))
    source = tmp_path/'parent.pt'
    parent = cp.save_event_checkpoint(source, model, extra=dict(event_ppo_training=saved, kl_coefficient=.037,
            mixed_deck_controller=dict(mixed_round=200), rl_action_constraints={'delay':{'minimum_ms':100}},
            rl_steps=1753, policy_version='parent-1753', rl_reward_components={'terminal':1.}))
    before = source.read_bytes()
    cache = tmp_path/'cache'
    chosen = {}
    for i in range(100):
        tag = f'fixture-{i}'
        chosen.setdefault(replay_partition(tag), tag)
    for split, tag in chosen.items():
        n = 64
        target = torch.tensor([2, -1] * (n // 2))
        policy = torch.zeros(n, model.backbone.config.lstm_hidden_dim)
        policy[:, 0] = torch.where(target >= 0, 5., -5.)
        features = AbilityILFeatures(policy, torch.zeros(n, 3, model.card_dim),
            torch.tensor([[False, False, True]]*n), torch.zeros(n, 2), torch.ones(n, 2, dtype=torch.bool), target)
        rows = []
        for i, label in enumerate(target.tolist()):
            audit = row(label >= 0)
            audit.update(replay_tag=tag, partition=split, tick=100+i)
            if label >= 0: audit['receipt']['tick'] = 101+i
            rows.append(audit)
        write_features(cache/f'{split}.pt', [features], rows, parent_sha256=parent)
    args = SimpleNamespace(source=source, cache=cache, output=tmp_path/'trained', seed=8, threads=1,
        device='cpu', learning_rate=.1, minimum_positives=20, batch_size=32, epochs=4, patience=3)
    report = train(args)
    result = cp.load_event_checkpoint(report['checkpoint'])
    assert report['old_tensors_unchanged'] and report['ppo_optimizer_and_kl_unchanged']
    assert source.read_bytes() == before
    assert result.payload['event_ppo_training']['iterations'] == saved['iterations']
    assert result.payload['event_ppo_training']['decision_rows'] == saved['decision_rows']
    assert result.payload['kl_coefficient'] == .037
    assert result.payload['rl_action_constraints'] == {'delay':{'minimum_ms':100}}
    assert result.payload['rl_steps'] == 1753 and result.payload['policy_version'] == 'parent-1753'
    assert result.payload['rl_reward_components'] == {'terminal':1.}
    assert report['validation']['loss'] < report['baseline_validation']['loss']
    assert frozen_state_digests(result.model) == frozen_state_digests(model)
    assert not result.model.event_initialization['needs_recalibration_or_finetune']
    assert result.model.event_initialization['needs_evaluation']
    assert result.model.event_initialization['ability_source_needs_supervision']
    assert not result.payload['event_ability_il_training']['source_selection_supervised']
    assert report['multi_source_positive_rows'] == 0
    assert not report['source_selection_learned_from_ambiguous_states']
