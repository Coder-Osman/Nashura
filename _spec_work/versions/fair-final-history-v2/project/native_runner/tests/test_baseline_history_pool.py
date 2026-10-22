"""Retiring legacy opponents preserves quotas, paired seeds and frozen plans."""
from collections import Counter, defaultdict

import pytest

from native_runner.training.event_opponents import freeze_plan, QUOTAS
from native_runner.training.event_rewards import OVERFLOW_REWARD_CONFIG
from native_runner.training.mixed_decks import (
    build_plan, select_pool, validate_plan, FIVE_DECK_IDS, RECRUITS_HOGS_POLICY,
)
from native_runner.tests.test_mixed_decks import BASELINE, CONSTRAINTS, FIXED, FLEET


def arguments(round_number=140, fixed=()):
    records = [dict(iteration=1250 + n, sha256=f'{1250+n:064x}')
               for n in range(1, round_number)]
    pool = select_pool(records, round_number=round_number, start_iteration=1250,
                       baseline=BASELINE, fixed=fixed)
    return dict(iteration=1250 + round_number, seed_base=2180920000,
                current_sha='b' * 64, pool=pool, fleet=FLEET, start_iteration=1250,
                action_constraints=CONSTRAINTS, deck_policy=RECRUITS_HOGS_POLICY,
                reward_config=OVERFLOW_REWARD_CONFIG)


@pytest.mark.parametrize('round_number', range(140, 145))
def test_baseline_only_fixed_pool_preserves_balanced_curriculum(round_number):
    args = arguments(round_number)
    plan = validate_plan(build_plan(**args))
    old = build_plan(**arguments(round_number, FIXED))
    assert plan['pool']['fixed'] == [dict(BASELINE, label='B0')]
    assert plan['pool']['recent'] == old['pool']['recent']
    assert Counter(m['kind'] for m in plan['matches']) == dict(self=72, fixed=12, recent=24)
    assert Counter(m['opponent']['iteration'] for m in plan['matches']
                   if m['kind'] == 'fixed') == {1250: 12}
    assert all(m['opponent']['iteration'] not in (600, 645) for m in plan['matches'] if m['opponent'])
    assert set(Counter(m['opponent']['iteration'] for m in plan['matches']
                       if m['kind'] == 'recent').values()) == {12}
    assert [m for m in plan['matches'] if m['kind'] != 'fixed'] == [
        m for m in old['matches'] if m['kind'] != 'fixed']
    lanes, pairs, host_counts = Counter(), defaultdict(list), Counter()
    for match in plan['matches']:
        host_counts[match['host'], match['kind'] == 'self'] += 1
        for owner in ((0, 1) if match['kind'] == 'self' else (match['learner_owner'],)):
            lanes[match['owner_deck_ids'][owner], owner] += 1
        if match['kind'] != 'self':
            pairs[match['pair_id']].append(match)
    assert lanes == Counter({(deck, owner): 18 for deck in FIVE_DECK_IDS for owner in (0, 1)})
    assert len(pairs) == 18
    for a, b in pairs.values():
        assert a['seed'] == b['seed']
        assert {a['learner_owner'], b['learner_owner']} == {0, 1}
        assert a['owner_sha256'] == b['owner_sha256'][::-1]
        assert a['owner_decks'] == b['owner_decks'][::-1]
    for host, (self_games, history_games) in QUOTAS.items():
        assert (host_counts[host, True], host_counts[host, False]) == (self_games, history_games)


def test_retry_reuses_identical_frozen_baseline_only_plan(tmp_path):
    args = arguments()
    first = freeze_plan(tmp_path/'plan.json', builder=build_plan, **args)
    assert freeze_plan(tmp_path/'plan.json', builder=build_plan, **args) == first
    assert validate_plan(first) == first


@pytest.mark.parametrize('fixed', [[FIXED[0]], list(reversed(FIXED)), [BASELINE]])
def test_invalid_partial_fixed_pool_fails(fixed):
    with pytest.raises(ValueError, match='fixed pool'):
        arguments(fixed=fixed)
