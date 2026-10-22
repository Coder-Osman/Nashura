import argparse
import hashlib
import json
from pathlib import Path
import signal
import time
import traceback

import torch

from .belief import IncrementalBeliefProvider
from .config import SearchConfig
from .contracts import semantic_digest
from .evaluate_teacher import sample_visit_action
from .evaluation import paired_confidence
from .inference import SocketInferenceClient
from .model import load_checkpoint
from .native import NativeMatchRuntime, NativeReplayFactory, install_puct
from .search import InformationSetSearch
from .storage import atomic_write


def publish(path, value):
    atomic_write(Path(path), json.dumps(value, sort_keys=True, allow_nan=False).encode(), replace=True)


def stream_seed(stream, pair, model_role, ordinal):
    payload = f'teacher-r0-vs-m0.v1:{stream}:{pair}:{model_role}:{ordinal}'
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], 'big')


def choose_actions(frozen, searches, *, pair, candidate_owner, ordinals, deadline):
    selected, records = {}, []
    for owner, information in sorted(frozen.items()):
        role = 'A' if owner == candidate_owner else 'M0'
        ordinal = ordinals[owner]
        search_seed = stream_seed('search', pair, role, ordinal)
        action_seed = stream_seed('execute', pair, role, ordinal)
        result = searches[owner].run(information, seed=search_seed, training=False, deadline=deadline)
        action = sample_visit_action(result.visits, seed=action_seed)
        if not information.legal(action) or result.particles != 1:
            raise ValueError('teacher action or belief contract differs')
        selected[owner] = action
        records.append(dict(owner=owner, model_role=role, ordinal=ordinal, tick=information.tick,
            information_sha256=information.sha256, search_seed=search_seed, action_seed=action_seed,
            visits=sum(result.visits.values()), visit_counts=result.visits, selected=action,
            seconds=result.seconds, timings=result.timings, particles=result.particles,
            root_noise=0., temperature=1.))
    return selected, records


def terminal_score(terminal, candidate_owner):
    if (not terminal['ended'] or terminal['truncated'] or terminal['rejected'] or terminal['unconfirmed']
            or terminal['winner'] not in (None, 0, 1)):
        raise ValueError('invalid native evaluation terminal')
    return .5 if terminal['winner'] is None else float(terminal['winner'] == candidate_owner)


def validate_result(row, manifest, job):
    expected = dict(job_id=job['id'], pair=job['pair'], candidate_owner=job['owner'], seed=job['seed'],
        candidate_sha256=manifest['candidate_sha256'], baseline_sha256=manifest['baseline_sha256'],
        evaluation_sha256=manifest['evaluation_sha256'], config_sha256=manifest['config_sha256'])
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError('evaluation result identity differs')
    if (type(row.get('attempt')) is not int or not 0 <= row['attempt'] < 3
            or row['score'] != terminal_score(row['terminal'], job['owner'])
            or set(row['roots']) != {'0', '1'} or any(count <= 0 for count in row['roots'].values())):
        raise ValueError('evaluation result contract differs')
    return row


def summarize(rows, manifest):
    expected = {job['id']: job for job in manifest['jobs']}
    results = {}
    for row in rows:
        if row['job_id'] not in expected or row['job_id'] in results:
            raise ValueError('duplicate or unknown evaluation task')
        results[row['job_id']] = validate_result(row, manifest, expected[row['job_id']])
    paired = {(row['pair'], row['candidate_owner']): row['score'] for row in results.values()}
    if len(paired) != len(results):
        raise ValueError('duplicate paired evaluation result')
    pairs = [[paired[pair, owner] for owner in (0, 1)] for pair in range(64)
             if all((pair, owner) in paired for owner in (0, 1))]
    interval = paired_confidence(pairs) if pairs else None
    complete = len(results) == 128 and len(pairs) == 64
    conclusion = 'incomplete'
    if complete:
        conclusion = ('A_stronger' if interval['lower95'] > .5 else
                      'A_weaker' if interval['upper95'] < .5 else 'inconclusive')
    scores = [row['score'] for row in results.values()]
    return dict(games=len(results), required=128, complete_pairs=len(pairs), complete=complete,
        wins=scores.count(1.), draws=scores.count(.5), losses=scores.count(0.),
        score=sum(scores) / len(scores) if scores else None, paired=interval, conclusion=conclusion,
        positions={str(owner): dict(games=sum(row['candidate_owner'] == owner for row in results.values()),
            points=sum(row['score'] for row in results.values() if row['candidate_owner'] == owner))
            for owner in (0, 1)}, promoted=False)


def play_game(args, manifest, job):
    config = SearchConfig(**manifest['config'])
    if (config.sha256 != manifest['config_sha256'] or config.particles != 1
            or config.root_noise != 0 or config.late_temperature != 1):
        raise ValueError('dual teacher evaluation configuration differs')
    models, identities = {}, {}
    for owner in (0, 1):
        role = 'candidate' if owner == job['owner'] else 'baseline'
        model, payload, identity = load_checkpoint(getattr(args, role),
            expected_sha256=manifest[role + '_sha256'], device='cpu')
        if payload['config_sha256'] != config.sha256:
            raise ValueError('checkpoint search configuration differs')
        models[owner] = model.eval().requires_grad_(False)
        identities[owner] = identity
    rollout_type = install_puct(args.project / 'research/puct-optimized')
    runtime, providers, clients = None, {}, {}
    roots, timings = {0: 0, 1: 0}, {0: 0., 1: 0.}
    started = time.monotonic()
    try:
        runtime = NativeMatchRuntime(args.port, models, identities, rollout_type=rollout_type,
            seed=job['seed'], match_id=job['id'], transport='compact-delta', execution_only=True)
        searches = {}
        for owner in (0, 1):
            factory = NativeReplayFactory([args.port + 1 + owner], models, identities, rollout_type,
                search_only=True, transport='compact-delta', conditional_draws=True,
                diagnostic_directory=args.output / f'diagnostics-{owner}')
            providers[owner] = IncrementalBeliefProvider(factory, attempts=32)
            clients[owner] = SocketInferenceClient(args.inference_socket, identities[owner])
            searches[owner] = InformationSetSearch(providers[owner], clients[owner], config=config)
        with (args.output / 'roots.jsonl').open('a', buffering=1) as log, torch.inference_mode():
            while not runtime.ended:
                if time.monotonic() >= started + args.deadline:
                    raise TimeoutError('teacher evaluation game deadline')
                frozen = runtime.freeze_due_information()
                if not frozen:
                    runtime.advance()
                    continue
                selected, records = choose_actions(frozen, searches, pair=job['pair'],
                    candidate_owner=job['owner'], ordinals=roots, deadline=started + args.deadline)
                runtime.commit_simultaneous(selected, frozen)
                for record in records:
                    owner = record['owner']
                    roots[owner] += 1
                    timings[owner] += record['seconds']
                    record.update(checkpoint_sha256=identities[owner], config_sha256=config.sha256)
                    log.write(json.dumps(record) + '\n')
                publish(args.output / 'progress.json', dict(job_id=job['id'], roots=roots,
                    tick=max(info.tick for info in frozen.values()), elapsed=time.monotonic() - started,
                    last_roots=records, evaluation_sha256=manifest['evaluation_sha256']))
        terminal = runtime.terminal_evidence()
        row = dict(job_id=job['id'], pair=job['pair'], candidate_owner=job['owner'], seed=job['seed'],
            attempt=args.attempt, score=terminal_score(terminal, job['owner']), terminal=terminal,
            candidate_sha256=manifest['candidate_sha256'], baseline_sha256=manifest['baseline_sha256'],
            evaluation_sha256=manifest['evaluation_sha256'], config_sha256=config.sha256,
            roots={str(owner): count for owner, count in roots.items()},
            search_seconds=timings, seconds=time.monotonic() - started)
    finally:
        failures = []
        for resource in [*providers.values(), *clients.values(), *([runtime] if runtime else [])]:
            try:
                resource.close()
            except Exception as error:
                failures.append(repr(error))
        if failures:
            raise RuntimeError('evaluation cleanup failed: ' + str(failures))
    validate_result(row, manifest, job)
    publish(args.output / 'completed.json', row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ('manifest', 'candidate', 'baseline', 'project', 'output', 'inference-socket'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--attempt', type=int, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--deadline', type=float, default=43200.)
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    manifest = json.loads(args.manifest.read_text())
    identity = dict(manifest)
    identity.pop('evaluation_sha256')
    if semantic_digest(identity) != manifest['evaluation_sha256']:
        raise ValueError('frozen evaluation manifest identity differs')
    job = next(job for job in manifest['jobs'] if job['id'] == args.job_id)
    args.output.mkdir(parents=True, exist_ok=True)
    def stop(*unused):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        play_game(args, manifest, job)
    except BaseException as error:
        publish(args.output / 'failure.json', dict(job_id=job['id'], attempt=args.attempt,
            error=repr(error), kind='interrupted' if isinstance(error, KeyboardInterrupt) else
            'transient' if isinstance(error, (TimeoutError, ConnectionError)) else 'contract_or_runtime',
            traceback=traceback.format_exc(), time=time.time()))
        raise


if __name__ == '__main__':
    main()
