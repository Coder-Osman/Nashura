"""Content-addressed reuse, anchor removal and row mapping failure gates."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import shutil

import pytest
import torch

from native_runner.tests.test_event_batches import fixture_lanes, clone_model, RecordingAdamW
from native_runner.tests.test_event_persistent_learner import initial, config, task_for
from native_runner.tests.test_event_optimizer_resume import _collect
from native_runner.training.event_shard_cache import VerifiedShardCache
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.training.event_ppo import event_ppo_update, EventPpoConfigV1
from native_runner.training.event_preflight import preflight_event_rollout
from native_runner.training.event_checkpoint import load_event_checkpoint
from tools.event_rl_learner import Learner, verify_optimizer_signature


def test_legacy_anchor_is_not_loaded_or_restored(initial, tmp_path):
    settings = config(initial, tmp_path)
    settings['argv'] += ['--reference-checkpoint', str(tmp_path/'missing-anchor.pt'), '--kl-coefficient', '3']
    worker = Learner(settings)
    # Model and AdamW may be restored from a legacy run with any anchor coefficient.
    worker.session.loaded.payload['kl_coefficient'] = 4.
    worker.session.loaded.payload['mixed_deck_controller'] = dict(coefficient=4., deck_id='golem')
    assert worker.session.reference_model is None
    task = task_for(worker, initial, tmp_path, 1)
    worker.update(task)
    child = load_event_checkpoint(task['output'])
    assert child.payload['kl_coefficient'] == 0
    assert child.payload['rl_reference_policy'] == dict(version='reference-disabled.v1', enabled=False)
    update = next(json.loads(l) for l in open(task['log']) if '"event": "ppo-update"' in l)
    assert update['kl_reference'] == update['kl_coefficient'] == update['kl_coefficient_next'] == 0
    assert update['reference'] is None
    assert json.loads(open(task['log']).readlines()[-1])['event'] == 'saved'


def test_verified_cache_reuses_content_but_detects_changes(initial, tmp_path, monkeypatch):
    loaded = load_event_checkpoint(initial)
    source = tmp_path/'part-0.crev.zst'
    _collect(loaded.model, source, loaded.checkpoint_sha256, loaded.contract_sha256)
    summary = tmp_path/'part-0.json'
    from native_runner.training import event_cache
    calls = []
    original = event_cache.load_event_rl_shard
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(event_cache, 'load_event_rl_shard', counted)
    cache = VerifiedShardCache()
    with ThreadPoolExecutor(4) as pool:
        values = list(pool.map(cache.load, [summary]*8))
    assert len(calls) == cache.misses == 1 and cache.hits == 7
    assert all(v.records is values[0].records for v in values)
    copied = tmp_path/'copy';copied.mkdir()
    shutil.copy2(summary, copied/summary.name);shutil.copy2(source, copied/source.name)
    assert cache.load(copied/summary.name).path == copied/source.name
    assert len(calls) == 1
    source.write_bytes(source.read_bytes()+b'corruption')
    with pytest.raises(ValueError, match='checksum'):
        cache.load(summary)
    copied_summary = json.loads((copied/summary.name).read_text());copied_summary['container_sha256'] = '0'*64
    (copied/summary.name).write_text(json.dumps(copied_summary))
    with pytest.raises(ValueError, match='container checksum'):
        cache.load(copied/summary.name)
    tiny = VerifiedShardCache(max_bytes=0)
    # Restore the good summary; eviction/fallback must keep all verification.
    shutil.copy2(summary, copied/summary.name)
    tiny.load(copied/summary.name);tiny.load(copied/summary.name)
    assert tiny.bytes == 0 and tiny.misses == 2


@pytest.mark.parametrize('component', [False, True])
def test_bucket_value_restore_and_objective_weights(component):
    model, lanes = fixture_lanes()
    lanes = [replace(l, deck_id=['golem','hog','hog'][i]) for i,l in enumerate(lanes)]
    runs = []
    for buckets in (False, True):
        clone = clone_model(model)
        cache = PreparedEventRollout(clone, lanes, row_batch_size=4, bucket_by_entities=buckets)
        rows = torch.arange(10, dtype=torch.float32)
        if component:rows = torch.stack([rows,rows+10,rows+20],dim=1)
        packed = [b.select(rows) for b in cache.batches]
        torch.testing.assert_close(cache.restore_order(packed), rows)
        preflight_event_rollout(clone, lanes, prepared=cache)
        optimizer = RecordingAdamW(clone.parameters(), lr=1e-5)
        stats = event_ppo_update(clone, lanes, optimizer, prepared=cache,
            config=EventPpoConfigV1(minibatch_lanes=0, deck_weights={'golem':.2,'hog':.8}, target_kl=.01))
        runs.append((cache, optimizer, stats))
    assert any(tuple(b.flat_indices.tolist()) != tuple(range(b.start,b.stop)) for b in runs[1][0].batches)
    for p,q in zip(runs[0][1].gradients,runs[1][1].gradients):
        if p is None:assert q is None
        else:torch.testing.assert_close(p,q,atol=3e-5,rtol=3e-4)
    assert runs[0][2].post_step_kl == pytest.approx(runs[1][2].post_step_kl,abs=2e-6)
    assert runs[1][0].as_dict()['repeated_transfers'] == 0


def test_optimizer_step_and_moment_signature_cannot_reset():
    parent = dict(names=['w'],groups=[dict(params=[0], lr=1e-5)],state={0:dict(step=12.,shapes={'exp_avg':(2,)})})
    good = dict(parent,state={0:dict(step=13.,shapes={'exp_avg':(2,)})})
    assert verify_optimizer_signature(parent, good, 1) == 1
    for bad in (dict(step=1.,shapes={'exp_avg':(2,)}),dict(step=14.,shapes={'exp_avg':(2,)}),
                dict(step=13.,shapes={'exp_avg':(3,)})):
        with pytest.raises(ValueError):verify_optimizer_signature(parent,dict(parent,state={0:bad}),1)
