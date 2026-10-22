from types import SimpleNamespace

import pytest

from native_runner.training.fair_search.evaluate_teacher import sample_visit_action
from native_runner.training.fair_search.evaluate_teachers import (
    choose_actions, stream_seed, summarize, terminal_score, validate_result,
)
from native_runner.training.fair_search.inference import BatchInference
from native_runner.training.fair_search.config import SearchConfig


@pytest.mark.parametrize('candidate_owner', (0, 1))
@pytest.mark.parametrize('tick', (0, 1200, 4000))
def test_both_search_frozen_views_and_execute_untempered_visits(candidate_owner, tick):
    views = {owner: SimpleNamespace(owner=owner, tick=tick, sha256=str(owner)*64,
             legal=lambda action: action in ('wait', 'play')) for owner in (0, 1)}
    ordinals = {0: 7, 1: 11}
    calls = []
    visits = {'wait': 8, 'play': 24, 'zero': 0}
    def run(view, **kwargs):
        assert view is views[view.owner]
        assert kwargs['training'] is False
        assert kwargs['deadline'] == 1234
        calls.append(view.owner)
        return SimpleNamespace(visits=visits, selected='ignored-argmax', particles=1, seconds=.1, timings={})
    selected, records = choose_actions(views, {owner: SimpleNamespace(run=run) for owner in (0, 1)},
        pair=4, candidate_owner=candidate_owner, ordinals=ordinals, deadline=1234)
    assert calls == [0, 1]
    assert ordinals == {0: 7, 1: 11}
    for row in records:
        assert row['model_role'] == ('A' if row['owner'] == candidate_owner else 'M0')
        assert selected[row['owner']] == sample_visit_action(visits, seed=row['action_seed'])
        assert row['search_seed'] != row['action_seed']
        assert row['temperature'] == 1 and row['root_noise'] == 0


def test_pair_random_streams_follow_model_role_across_seat_swap():
    assert stream_seed('search', 4, 'A', 10) == stream_seed('search', 4, 'A', 10)
    assert len({stream_seed(stream, pair, role, ordinal) for stream in ('search','execute')
                for pair in (4, 5) for role in ('A', 'M0') for ordinal in (10, 11)}) == 16


@pytest.mark.parametrize('visits', (31, 33))
def test_dual_search_incomplete_root_is_rejected(visits):
    view = SimpleNamespace(sha256='a'*64, tick=0, legal=lambda action: True)
    search = SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(visits={'wait': visits}))
    with pytest.raises(ValueError, match='32'):
        choose_actions({0:view}, {0:search}, pair=0, candidate_owner=0, ordinals={0:0}, deadline=None)


@pytest.mark.parametrize('winner,owner,score', [(0,0,1.),(1,0,0.),(0,1,0.),(1,1,1.),(None,0,.5),(None,1,.5)])
def test_native_terminal_score_is_relative_to_candidate(winner, owner, score):
    assert terminal_score(dict(ended=True, truncated=False, rejected=False, unconfirmed=False,
                               winner=winner),owner) == score


@pytest.mark.parametrize('flag', ('truncated','rejected','unconfirmed'))
def test_invalid_terminal_is_not_a_draw(flag):
    terminal = dict(ended=True, truncated=False, rejected=False, unconfirmed=False, winner=None)
    terminal[flag] = True
    with pytest.raises(ValueError, match='terminal'):
        terminal_score(terminal, 0)


def fixture_rows():
    jobs = [dict(id=f'{pair}-{owner}', pair=pair, owner=owner, seed=1000+pair)
            for pair in range(64) for owner in (0,1)]
    manifest = dict(jobs=jobs,candidate_sha256='a'*64,baseline_sha256='b'*64,
                    evaluation_sha256='c'*64,config_sha256='d'*64)
    rows = [dict(job_id=job['id'],pair=job['pair'],candidate_owner=job['owner'],seed=job['seed'],
        attempt=0,score=1.,terminal=dict(ended=True,truncated=False,rejected=False,unconfirmed=False,winner=job['owner']),
        roots={'0':1,'1':1},**{key:manifest[key] for key in ('candidate_sha256','baseline_sha256',
                                                          'evaluation_sha256','config_sha256')}) for job in jobs]
    return manifest,rows


def test_paired_report_requires_all_jobs_and_rejects_duplicates():
    manifest,rows = fixture_rows()
    report = summarize(rows,manifest)
    assert report['complete'] and report['complete_pairs'] == 64
    assert report['conclusion'] == 'A_stronger' and report['paired']['lower95'] == 1.
    partial = summarize(rows[:1],manifest)
    assert not partial['complete'] and partial['paired'] is None
    with pytest.raises(ValueError, match='duplicate'):
        summarize(rows+[rows[0]],manifest)


def test_result_recovery_checks_model_seed_attempt_and_terminal():
    manifest,rows = fixture_rows()
    for patch in ({'candidate_sha256':'b'*64},{'seed':1},{'attempt':3},{'score':.5}):
        with pytest.raises(ValueError):
            validate_result(dict(rows[0],**patch),manifest,manifest['jobs'][0])


def test_batch_inference_separates_model_versions():
    calls = []
    class Backend:
        def __init__(self, identity):
            self.identity = identity
        def infer_batch(self, requests):
            calls.append((self.identity,requests))
            return [self.identity for request in requests]
    identities = ['a'*64,'b'*64]
    batch = BatchInference({identity:Backend(identity) for identity in identities},
                           config=SearchConfig(inference_wait_ms=20))
    try:
        futures = [batch.submit(identity,'predict',None,0) for identity in identities]
        assert [future.result(timeout=5) for future in futures] == identities
        assert {identity for identity,requests in calls} == set(identities)
        with pytest.raises(ValueError):
            batch.submit('c'*64,'predict',None,0)
    finally:
        batch.close()
