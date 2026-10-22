from copy import deepcopy

import pytest
import torch

from native_runner.tests.test_event_ability_il import features
from native_runner.tests.test_event_ability_actions import ability_model
from native_runner.training.event_ability_il import make_ability_optimizer
from native_runner.training.event_ability_il_data import read_features, replay_partition, validate_label, write_features


def row(positive=True):
    value = dict(replay_tag='test-replay', payload_sha256='a' * 64, owner=0, tick=100, trusted=True,
        partition=replay_partition('test-replay'), expert_skill_next_tick=positive,
        legal_sources=[dict(candidate=2, source_entity=123, ability_id='ability-a')])
    if positive:
        value.update(source_resolution='unique-native-ready', receipt=dict(event_type='action_executed',
            owner=0, entity_id=123, tick=101, data=dict(ability_id='ability-a', kind='activate_ability',
            fair_ability_activation_exact=True)))
    return value


@pytest.mark.parametrize('bad', ['entity', 'tick', 'ability', 'queued', 'ambiguous', 'partition', 'untrusted', 'negative'])
def test_reject_guessed_sources_queued_actions_future_skills_and_leaked_partitions(bad):
    value = row()
    if bad == 'entity': value['receipt']['entity_id'] = 124
    elif bad == 'tick': value['receipt']['tick'] = 103
    elif bad == 'ability': value['receipt']['data']['ability_id'] = 'other'
    elif bad == 'queued': value['receipt']['event_type'] = 'action_queued'
    elif bad == 'ambiguous': value['legal_sources'].append(dict(candidate=1, source_entity=124, ability_id='ability-a'))
    elif bad == 'partition': value['partition'] = 'invalid'
    elif bad == 'untrusted': value['trusted'] = False
    with pytest.raises(ValueError): validate_label(value, -1 if bad == 'negative' else 2)


def test_audited_shard_roundtrip_parent_binding_and_no_overwrite(tmp_path):
    model = ability_model()
    make_ability_optimizer(model)
    batch = features(model, 2)
    path = tmp_path/'features.pt'
    rows = [row(), row(False)]
    sha = write_features(path, [batch], rows, parent_sha256='parent')
    assert len(sha) == 64
    loaded, metadata = read_features(path, parent_sha256='parent')
    assert metadata == rows and torch.equal(loaded.policy_context, batch.policy_context)
    with pytest.raises(ValueError, match='parent'):
        read_features(path, parent_sha256='other')
    with pytest.raises(FileExistsError):
        write_features(path, [batch], rows, parent_sha256='parent')
    raw = torch.load(path, weights_only=True)
    raw['features']['ability_mask'][0, 1] = True
    torch.save(raw, tmp_path/'tampered.pt')
    with pytest.raises(ValueError, match='masks disagree'):
        read_features(tmp_path/'tampered.pt', parent_sha256='parent')


def test_known_reward_extension_projection_keeps_policy_and_unknown_fields():
    from native_runner.training.event_ability_il_producer import skill_policy_atomic_view
    source = dict(rich=dict(generation=2, stateEpoch=3, tick=100, objects=[], unknown='still-validated',
        settlementInventory=dict(schema='native-settlement-inventory.v1', generation=2, stateEpoch=3, tick=100)))
    projected = skill_policy_atomic_view(source)
    assert 'settlementInventory' not in projected['rich']
    assert 'settlementInventory' in source['rich']
    assert projected['rich']['unknown'] == 'still-validated'
    source['rich']['settlementInventory']['tick'] += 1
    with pytest.raises(ValueError, match='identity mismatch'):
        skill_policy_atomic_view(source)


def test_expert_source_command_maps_to_exact_next_native_tick():
    from dataclasses import replace
    from types import SimpleNamespace
    from native_runner.contracts import ActionKind, ActionV1
    from native_runner.training.v4.expert import TimedExpertActionV4
    from native_runner.training.event_ability_il_producer import native_expert_timeline
    action = ActionV1(owner=0, kind=ActionKind.ACTIVATE_ABILITY, source_entity=0,
                      metadata=dict(source_command_tick=1431, native_observable_tick=1432))
    expert = TimedExpertActionV4(source_tick=1431, action=action, source_index=14)
    command = SimpleNamespace(target_tick=1432, owner=0, ability=True)
    timeline = native_expert_timeline([expert], [command])
    assert timeline == {1432: [expert]}
    assert 1431 not in timeline
    with pytest.raises(ValueError, match='commands disagree'):
        native_expert_timeline([expert], [])
    with pytest.raises(ValueError, match='boundary'):
        native_expert_timeline([replace(expert, action=replace(action, metadata={}))], [command])
