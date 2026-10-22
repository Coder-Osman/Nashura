"""Continue a completed run in a separate directory with bounded worker caches.

The copied fleet_runtime.py supplies the previously validated collection/update
implementation. All learner checkpoints and rollout data remain intact.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import traceback


def cache_candidates(paths, keep):
    """Only content-addressed checkpoints from this run are eligible."""
    keep = set(keep)
    for path in paths:
        if path.suffix != '.pt' or len(path.stem) != 64 or any(c not in '0123456789abcdef' for c in path.stem):
            raise ValueError(f'non-content-addressed cache entry: {path.name}')
    return [path for path in paths if path.name not in keep]


def resume_point(root, cfg):
    """Resume only complete contiguous rounds; never reuse an uncertain shard."""
    iteration = cfg['start_iteration']
    weight = root/'weights'/f"{cfg['initial_checkpoint_sha256']}.pt"
    for path in sorted(root.glob('iteration-*/success.json')):
        record = json.loads(path.read_text())
        if record['iteration'] != iteration + 1 or path.parent.name != f"iteration-{record['iteration']:06d}":
            raise ValueError('resume requires contiguous completed iterations')
        iteration = record['iteration']
        weight = Path(record['checkpoint'])
        if weight.parent != root/'weights':
            raise ValueError('resume checkpoint belongs to another run')
    for path in root.glob('iteration-*'):
        if path.is_dir() and int(path.name.split('-')[-1]) > iteration:
            raise ValueError('incomplete iteration exists; inspect it before resuming')
    if hashlib.sha256(weight.read_bytes()).hexdigest() != weight.stem:
        raise ValueError('resume checkpoint SHA mismatch')
    return iteration, weight


def training_schedule(cfg, previous=None, now=None):
    """An explicit continuous run has neither a round cap nor a time deadline."""
    started_at = previous['started_at'] if previous is not None else (time.time() if now is None else now)
    continuous = cfg.get('training_mode') == 'continuous'
    if continuous:
        if any(cfg.get(key) is not None for key in ('target_iteration', 'additional_iterations', 'duration_seconds')):
            raise ValueError('continuous training must not have a round or time limit')
        duration, deadline = None, None
    else:
        duration = cfg['duration_seconds']
        deadline = previous['deadline'] if previous is not None else started_at + duration
    return dict(started_at=started_at, deadline=deadline, duration_seconds=duration,
                finish_current_iteration=True, training_mode='continuous' if continuous else 'bounded')


def training_should_continue(iteration, first, additional_limit, deadline, now=None):
    current_time = time.time() if now is None else now
    return ((deadline is None or current_time < deadline)
            and (additional_limit is None or iteration < first + additional_limit))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--resume', action='store_true', help='continue after a completed-round boundary')
    args = parser.parse_args()
    root = args.run_root.resolve()
    spec = importlib.util.spec_from_file_location('fleet_runtime', root/'fleet_runtime.py')
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    cfg = fleet.CFG
    # The parent lock also excludes earlier evaluators from the same ports.
    parent_lock = (Path(cfg['parent_run'])/'orchestrator.lock').open('a')
    fcntl.flock(parent_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock = (root/'orchestrator.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not args.check_only:
        (root/'orchestrator.pid').write_text(str(os.getpid()))
    initial = root/'weights'/f"{cfg['initial_checkpoint_sha256']}.pt"
    completed_iteration, weight = resume_point(root, cfg) if args.resume else (cfg['start_iteration'], initial)
    completed_steps = cfg['start_optimizer_steps'] + completed_iteration - cfg['start_iteration']
    verify = '''import sys,json,hashlib,torch
from native_runner.training.event_checkpoint import load_event_checkpoint
p=sys.argv[1]; x=load_event_checkpoint(p,device='cpu'); s=x.payload['event_ppo_training']
assert x.checkpoint_sha256==sys.argv[2]
assert s['iterations']==int(sys.argv[3]) and s['optimizer_steps']==int(sys.argv[4])
assert s['optimizer']['state'] and x.model.backbone.config.snapshot_only
assert all(torch.isfinite(p).all() for p in x.model.parameters())
print(json.dumps({'event':'resume-verified','checkpoint_sha256':x.checkpoint_sha256,'iterations':s['iterations'],'optimizer_steps':s['optimizer_steps'],'decision_rows':s['decision_rows'],'learning_rates':[g['lr'] for g in s['optimizer']['param_groups']]}),flush=True)
'''
    fleet.checked([str(fleet.PY),'-c',verify,str(weight),weight.stem,
                   str(completed_iteration),str(completed_steps)],
                  cwd=fleet.CODE,env={**fleet.ENV,'CUDA_VISIBLE_DEVICES':''})
    if args.check_only:
        print('Continuation precheck passed',flush=True)
        return
    first = cfg['start_iteration'] + 1
    previous_schedule = json.loads((root/'schedule.json').read_text()) if args.resume else None
    schedule = training_schedule(cfg, previous_schedule)
    started_at, deadline = schedule['started_at'], schedule['deadline']
    schedule_temp = root/'schedule.json.tmp'
    schedule_temp.write_text(json.dumps(schedule, indent=2))
    schedule_temp.replace(root/'schedule.json')
    recent = [initial.name, weight.name]
    expected = sum(len(f['ports']) for f in fleet.FLEET.values())
    collector = None
    learner = None
    if cfg.get('collector_mode') == 'persistent':
        code = cfg.get('learner_code', 'code') if cfg.get('learner_mode') == 'persistent' else cfg.get('collector_code', 'collector-code')
        sys.path.insert(0, str(root/code))
        from tools.event_rl_persistent_fleet import PersistentFleet
        collector = PersistentFleet(fleet)
    try:
        if cfg.get('learner_mode') == 'persistent':
            if collector is None:
                raise ValueError('persistent learner requires persistent collection')
            from tools.event_rl_persistent_learner import PersistentLearner
            learner = PersistentLearner(fleet, weight)
        iteration = completed_iteration + 1
        additional_limit = cfg.get('additional_iterations')
        if additional_limit is not None and (type(additional_limit) is not int or additional_limit <= 0):
            raise ValueError('additional_iterations must be a positive integer')
        while training_should_continue(iteration, first, additional_limit, deadline):
            fleet.stop_check('before-collection')
            # If the learner fills faster than forecast, release older local
            # weight copies only after verifying their durable .37 archives.
            if shutil.disk_usage(root).free < 8_000_000_000:
                for success_path in sorted(root.glob('iteration-*/success.json')):
                    if shutil.disk_usage(root).free >= 12_000_000_000:
                        break
                    success=json.loads(success_path.read_text())
                    old=Path(success['checkpoint'])
                    if old.name in recent or old==initial or not old.exists():
                        continue
                    assert old.parent==root/'weights'
                    digest=hashlib.sha256(old.read_bytes()).hexdigest()
                    assert digest==old.stem
                    archive=success['archive_checkpoint']
                    check='import pathlib,hashlib; p=pathlib.Path('+repr(archive)+'); assert hashlib.sha256(p.read_bytes()).hexdigest()=='+repr(digest)
                    fleet.ssh('37','python3 -c '+shlex.quote(check),timeout=60)
                    updated=success_path.parent/'updated.pt'
                    assert not old.is_symlink() and not updated.is_symlink()
                    if updated.exists():
                        assert os.path.samefile(old,updated)
                        updated.unlink()
                    old.unlink()
                    with (root/'cache-cleanup.jsonl').open('a') as stream:
                        stream.write(json.dumps({'host':'34','iteration':iteration,'removed_checkpoint_sha256':digest,'preserved_archive_host':'37','archive_checkpoint':archive})+'\n')
            fleet.event('iteration-start',iteration=iteration,checkpoint=str(weight),deadline=deadline)
            if collector is not None:
                if learner is None:
                    collector.prepare_iteration(weight, **(dict(iteration=iteration) if cfg.get('mixed_opponents') else {}))
                else:
                    collector.prepare_iteration(weight, learner.prepare_iteration(weight),
                                                **(dict(iteration=iteration) if cfg.get('mixed_opponents') else {}))
            with concurrent.futures.ThreadPoolExecutor(len(fleet.FLEET)) as pool:
                rollout_host = fleet.rollout_host if collector is None else collector.rollout_host
                futures = [pool.submit(rollout_host,h,iteration,weight) for h in fleet.FLEET]
                for future in futures:
                    future.result()
            it = root/f'iteration-{iteration:06d}'
            cache = it/'collected'
            summaries = list(cache.glob('**/part-*.json'))
            assert len(summaries) == expected, (len(summaries), expected)
            fleet.event('fleet-barrier',iteration=iteration,shards=len(summaries))
            fleet.stop_check('before-update')
            fleet.event('update-start',iteration=iteration,checkpoint=str(weight))
            update = fleet.update if learner is None else learner.update
            child = update(cache,weight,it/'updated.pt',expected,it/'update.log')
            # Archive every completed weight on the large-capacity worker before
            # releasing any .39 inference cache. Learner retains every weight.
            archive = fleet.FLEET['37']['run']+'/weights/'+child.name
            fleet.transport('37',str(child),archive)
            check = 'import pathlib,hashlib; p=pathlib.Path('+repr(archive)+'); assert hashlib.sha256(p.read_bytes()).hexdigest()=='+repr(child.stem)
            fleet.ssh('37','python3 -c '+shlex.quote(check),timeout=60)
            recent = (recent + [child.name])[-2:]
            # Only .39 is capacity constrained. Its cache contains no unique
            # model; validate each file against the learner before removal.
            h='39'; remote_weights=fleet.FLEET[h]['run']+'/weights'
            listing=fleet.ssh(h,'python3 -c '+shlex.quote('import pathlib,json; print(json.dumps([p.name for p in pathlib.Path('+repr(remote_weights)+').glob("*.pt")]))'),capture_output=True,text=True,timeout=30)
            candidates=cache_candidates([Path(name) for name in json.loads(listing.stdout)],recent)
            digests={}
            for p in candidates:
                master=root/'weights'/p.name
                digest=hashlib.sha256(master.read_bytes()).hexdigest()
                assert digest==p.stem
                digests[p.name]=digest
            cleanup='''import pathlib,hashlib,json
r=pathlib.Path(ROOT); expected=EXPECTED
freed=0
for name,digest in expected.items():
 p=r/name
 assert p.is_file() and not p.is_symlink()
 assert hashlib.sha256(p.read_bytes()).hexdigest()==digest
 freed+=p.stat().st_size
 p.unlink()
print(json.dumps({'removed':list(expected),'freed_bytes':freed}))
'''.replace('ROOT',repr(remote_weights)).replace('EXPECTED',repr(digests))
            result=fleet.ssh(h,'python3 -c '+shlex.quote(cleanup),capture_output=True,text=True,timeout=60)
            cleanup_record={'iteration':iteration,'host':h,'preserved_on_learner':True,**json.loads(result.stdout)}
            with (root/'cache-cleanup.jsonl').open('a') as stream:
                stream.write(json.dumps(cleanup_record)+'\n')
            weight=child
            (it/'success.json').write_text(json.dumps({'checkpoint':str(weight),'iteration':iteration,'shards':expected,'archive_host':'37','archive_checkpoint':archive}))
            fleet.event('iteration-success',iteration=iteration,checkpoint=str(weight),shards=expected,deadline=deadline)
            iteration += 1
        if additional_limit is not None and iteration-first != additional_limit:
            raise RuntimeError('duration guard reached before requested iteration count')
        fleet.event('completed',iterations=iteration-1,additional_iterations=iteration-first,checkpoint=str(weight),started_at=started_at,deadline=deadline,elapsed_seconds=time.time()-started_at)
    except SystemExit as error:
        if error.code not in (None, 0):
            fleet.event('fatal',traceback=traceback.format_exc())
        raise
    except BaseException:
        fleet.event('fatal',traceback=traceback.format_exc())
        raise
    finally:
        if learner is not None:
            learner.close()
        if collector is not None:
            collector.close()


if __name__=='__main__':
    main()
