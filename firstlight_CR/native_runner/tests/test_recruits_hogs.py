from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.training.event_opponents import QUOTAS, digest, freeze_plan, match_metadata, validate_shard_ownership
from native_runner.training.mixed_decks import (
    DECKS, FIVE_DECK_IDS, RECRUITS_HOGS_POLICY, build_plan, row_counts, stage, validate_plan,
)
from native_runner.training.event_ppo import EventPpoConfigV1, event_ppo_update, objective_row_weights
from native_runner.tests.test_mixed_decks import plan, FLEET, CONSTRAINTS, mixed_lanes
from native_runner.tests.test_event_ppo import _play_lane
from native_runner.tests.test_event_ppo_weighting import RecordingSGD


def five_plan(n=67):
    old = plan(min(n, 100))
    return build_plan(iteration=1250+n, seed_base=old['seed_base'], current_sha=old['current_sha256'],
                      pool=old['pool'], fleet=FLEET, start_iteration=1250,
                      action_constraints=CONSTRAINTS, deck_policy=RECRUITS_HOGS_POLICY)


@pytest.mark.parametrize('n,sha', [(1, 'ca29f7a6c486467b31d48ce20a4576503cd89ebf67d0d353bf29cc19b1eb41b7'),
    (21, '833cefee5c67c8fdcb43c30959e66de1ea53447011832ccce3786e813b1e34cf'),
    (67, '4e9488cecf950b626d2e46a8c2f12982892d833e3a85649be3e74153cf9c01a2')])
def test_archived_four_deck_plans_keep_identical_sha(n, sha):
    assert validate_plan(plan(n))['sha256'] == sha


def test_five_decks_balance_owners_fleet_history_and_rotating_matchups():
    cells_total = Counter()
    for n in range(61, 66):
        p = validate_plan(five_plan(n))
        assert p['learning_weights'] == dict.fromkeys(FIVE_DECK_IDS, .2)
        assert len(p['matches']) == 108
        train_sides, pairs, cells, quotas, opponent_shas = Counter(), defaultdict(list), Counter(), Counter(), Counter()
        for m in p['matches']:
            assert m['owner_decks'] == [list(DECKS[d]) for d in m['owner_deck_ids']]
            quotas[m['host'], m['kind'] == 'self'] += 1
            for owner in ((0, 1) if m['kind'] == 'self' else (m['learner_owner'],)):
                train_sides[m['owner_deck_ids'][owner], owner] += 1
                assert m['owner_sha256'][owner] == p['current_sha256']
            if m['kind'] == 'self':
                cells[tuple(m['owner_deck_ids'])] += 1
            else:
                pairs[m['pair_id']].append(m)
                opponent_shas[m['kind'], m['opponent']['sha256']] += 1
        assert len(train_sides) == 10 and set(train_sides.values()) == {18}
        assert len(cells) == 25 and Counter(cells.values()) == {2: 3, 3: 22}
        assert sum(cells.values()) == 72 and len(pairs) == 18
        assert Counter(v for (kind, _), v in opponent_shas.items() if kind == 'fixed') == {4: 3}
        assert Counter(v for (kind, _), v in opponent_shas.items() if kind == 'recent') == {12: 2}
        for h, (own, historical) in QUOTAS.items():
            assert (quotas[h, True], quotas[h, False]) == (own, historical)
        for a, b in pairs.values():
            assert a['seed'] == b['seed'] and {a['learner_owner'], b['learner_owner']} == {0, 1}
            assert a['owner_decks'] == b['owner_decks'][::-1]
            assert a['owner_sha256'] == b['owner_sha256'][::-1]
        cells_total.update(cells)
    assert {cells_total[d, d] for d in FIVE_DECK_IDS} == {12}
    assert {v for (a, b), v in cells_total.items() if a != b} == {15}


def test_resume_freezes_policy_and_rejects_relabeling(tmp_path):
    p = five_plan()
    args = dict(builder=build_plan, iteration=p['iteration'], seed_base=p['seed_base'],
        current_sha=p['current_sha256'], pool=p['pool'], fleet=FLEET, start_iteration=1250,
        action_constraints=CONSTRAINTS, deck_policy=RECRUITS_HOGS_POLICY)
    path = tmp_path/'plan.json'
    assert freeze_plan(path, **args) == p
    assert freeze_plan(path, **args) == p
    with pytest.raises(ValueError, match='curriculum'):
        freeze_plan(path, **dict(args, deck_policy=None))
    for value in (None, 'unknown-policy'):
        altered = deepcopy(p)
        altered['deck_policy'] = value
        altered['sha256'] = digest({k: v for k, v in altered.items() if k != 'sha256'})
        with pytest.raises(ValueError):
            validate_plan(altered)


@pytest.mark.parametrize('n', [101, 1001, 100000])
def test_five_deck_policy_has_no_round_ceiling(n):
    assert len(validate_plan(five_plan(n))['matches']) == 108
    assert stage(n, RECRUITS_HOGS_POLICY)[1]['recruits-hogs'] == .2


@pytest.mark.parametrize('owner', [0, 1])
def test_new_deck_history_never_admits_frozen_rows(owner):
    p = five_plan()
    m = next(m for m in p['matches'] if m['kind'] != 'self' and m['learner_owner'] == owner
             and m['owner_deck_ids'][owner] == 'recruits-hogs')
    metadata = match_metadata(p, m)
    assert metadata['deck_policy'] == RECRUITS_HOGS_POLICY
    row = SimpleNamespace(owner=owner, match_id=m['game_id'], behavior_checkpoint_sha256=p['current_sha256'],
                          deck=m['owner_decks'][owner], opponent_deck=m['owner_decks'][1-owner])
    shard = SimpleNamespace(manifest={'contract': dict(mixed_opponents=metadata, action_constraints=CONSTRAINTS)}, records=[row])
    validate_shard_ownership(shard, metadata)
    shard.records.append(SimpleNamespace(**{**vars(row), 'owner': 1-owner}))
    with pytest.raises(ValueError, match='frozen opponent'):
        validate_shard_ownership(shard, metadata)


def test_fifth_deck_contributes_equal_objective_mass_and_real_ppo_update():
    model, lanes = mixed_lanes()
    with torch.no_grad():
        lanes.append(replace(_play_lane(model, 7, rewards=[0.]*6+[1.]), deck_id='recruits-hogs'))
    weights = stage(67, RECRUITS_HOGS_POLICY)[1]
    assert set(row_counts(lanes, weights)) == set(FIVE_DECK_IDS)
    mass = objective_row_weights(lanes, weights, device='cpu')
    assert [float(x.sum()) for x in mass.split([l.time_steps for l in lanes])] == pytest.approx([.2]*5)
    with pytest.raises(ValueError, match='every'):
        row_counts(lanes[:-1], weights)
    optimizer = RecordingSGD(model.parameters(), lr=0.)
    stats = event_ppo_update(model, lanes, optimizer,
        config=EventPpoConfigV1(minibatch_lanes=0, sequence_chunk_steps=3, deck_weights=weights))
    assert stats.optimizer_steps == 1 and stats.steps == sum(l.time_steps for l in lanes)
    assert all(torch.isfinite(g).all() for g in optimizer.gradients if g is not None)


def test_continuous_run_preserves_explicit_no_limit_and_evaluation_disabled():
    from tools.event_mixed_train import validate_run_config
    config = dict(start_iteration=1250, training_mode='continuous', additional_iterations=None,
        target_iteration=None, duration_seconds=None, evaluation={'enabled': False},
        mixed_opponents=dict(version='mixed-deck.v1', start_iteration=1250, deck_policy=RECRUITS_HOGS_POLICY))
    assert validate_run_config(config) == (None, False)
    for key in ('additional_iterations', 'target_iteration', 'duration_seconds'):
        with pytest.raises(ValueError, match='no limits'):
            validate_run_config(dict(config, **{key: 100}))
    old = dict(config, training_mode='bounded', additional_iterations=100, evaluation={'enabled': True},
               mixed_opponents=dict(version='mixed-deck.v1', start_iteration=1250))
    assert validate_run_config(old) == (100, True)
