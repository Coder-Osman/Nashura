"""Require complete v3 matches and reconciled native execution before PPO."""
import json
import math
from native_runner.training.event_opponents import validate_plan, match_metadata, result_statistics

ERROR_COUNTERS = ('not_ready', 'cancelled_window', 'act_violation',
                  'cancelled_hand_slot', 'cancelled_play_kind', 'rejected', 'unconfirmed',
                  'decisions_after_gameplay', 'submissions_after_gameplay')

def validate_round(cache, expected, checkpoint_sha256, *, benchmark_bindings=None, shard_cache=None):
    plan_path = cache.parent / 'match-plan.json'
    plan = validate_plan(json.loads(plan_path.read_text())) if plan_path.exists() else None
    if plan is not None and (plan['current_sha256'] != checkpoint_sha256 or expected != plan['expected_games']):
        raise ValueError('round plan behavior mismatch')
    planned = {} if plan is None else {(m['host'], m['port']): match_metadata(plan, m) for m in plan['matches']}
    if benchmark_bindings is not None:
        if plan is None or set(benchmark_bindings) != set(planned):
            raise ValueError('benchmark physical bindings must cover the frozen logical plan')
        for key, port in benchmark_bindings.items():
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('invalid benchmark physical port')
            planned[key] = dict(planned[key], port=port)
    seen = set()
    counters = []
    player_lanes = 0
    logs = sorted(cache.glob('host-*/port-*/collector.log'))
    if len(logs) != expected:
        raise ValueError(f'expected {expected} collector logs, found {len(logs)}')
    totals = dict(rows=0, submitted=0, confirmed=0, cancelled_native_terminal=0,
                  recovery=0, cancelled_cell=0)
    credit_totals = dict(trainable_rows=0, excluded_rows=0)
    for path in logs:
        records = [json.loads(line) for line in path.read_text().splitlines() if line.startswith('{')]
        counter = records[-1]
        counters.append(counter)
        owners = [0, 1]
        if plan is not None:
            key = (path.parent.parent.name.removeprefix('host-'), int(path.parent.name.removeprefix('port-')))
            if key not in planned or key in seen or counter.get('mixed_opponents') != planned[key]:
                raise ValueError('native collector disagrees with mixed plan')
            seen.add(key)
            match = planned[key]
            owners = [0, 1] if match['kind'] == 'self' else [match['learner_owner']]
            if counter.get('recorded_owners') != owners:
                raise ValueError('collector recorded frozen opponent')
            if sum(counter['confirmed_by_owner'].values()) != counter['confirmed']:
                raise ValueError('native receipt owner totals mismatch')
        elif counter.get('mixed_opponents') is not None:
            raise ValueError('mixed collector is missing its plan')
        player_lanes += len(owners)
        if counter.get('scheduler_version') != 'event-scheduling.v3':
            raise ValueError(f'stale scheduler in {path}')
        if counter['source_checkpoint_sha256'] != checkpoint_sha256:
            raise ValueError(f'stale checkpoint in {path}')
        if not counter['terminal'] or any(counter[k] for k in ERROR_COUNTERS):
            raise ValueError(f'invalid scheduled match in {path}: '+str({k:counter[k] for k in ERROR_COUNTERS}))
        if counter['executed'] != counter['confirmed'] or counter['submitted'] != sum(
            counter[k] for k in ('confirmed', 'rejected', 'cancelled_native_terminal', 'unconfirmed')
        ):
            raise ValueError(f'unreconciled execution in {path}')
        summary_paths = list(path.parent.glob('part-*.json'))
        if len(summary_paths) != 1:
            raise ValueError(f'expected exactly one shard in {path.parent}')
        summary = json.loads(summary_paths[0].read_text())
        reward_config = None if plan is None else plan.get('reward_config')
        if counter.get('reward_config') != reward_config or summary.get('reward_config') != reward_config:
            raise ValueError('collector reward configuration differs from frozen plan')
        if reward_config is not None:
            from native_runner.training.event_rewards import reward_config as checked_config, overflow_penalty
            checked_config(reward_config)
            if 'tower_potential' in reward_config:
                from native_runner.training.event_cache import load_event_rl_shard
                from native_runner.training.event_rewards import reward_components
                from native_runner.training.event_tower_rewards import validate_tower_state
                shard = (load_event_rl_shard if shard_cache is None else shard_cache.load)(summary_paths[0], verify=True)
                derived = reward_components(shard.records, reward_config)
                if set(derived) != set(counter['reward_components']) or any(
                        not math.isclose(value, counter['reward_components'][key], abs_tol=1e-7)
                        for key, value in derived.items()):
                    raise ValueError('tower reward recomputation disagrees with collector')
                validate_tower_state(counter['tower_final_state'])
            report = counter['elixir_overflow']
            if report['config'] != reward_config or report['receipt_count'] != counter['confirmed']:
                raise ValueError('overflow receipt/configuration audit mismatch')
            for key, value in counter['reward_components'].items():
                if not math.isclose(summary['reward_components'][key], value, abs_tol=1e-7):
                    raise ValueError('collector/shard reward components mismatch')
            totals_penalty = sum(report['owners'][str(o)]['penalty'] for o in owners)
            if not math.isclose(totals_penalty, summary['reward_components']['overflow_penalty'], abs_tol=1e-7):
                raise ValueError('metered penalty is missing from recorded reward rows')
            for data in report['owners'].values():
                if (not math.isclose(data['penalty'], overflow_penalty(data['waste_raw'], reward_config), abs_tol=1e-9)
                        or data['waste_raw'] != sum(p['waste_raw'] for p in data['by_multiplier'].values())
                        or not math.isclose(data['penalty'],sum(p['penalty'] for p in data['by_multiplier'].values()),abs_tol=1e-9)):
                    raise ValueError('overflow integer/cap/phase audit failed')
        if summary['matches'] != 1 or summary['terminal_frames'] != len(owners) or summary['truncated_frames'] != 0:
            raise ValueError(f'expected complete recorded player lanes in {path}')
        states = summary['execution_status_counts']
        recorded_confirmed = counter['confirmed'] if plan is None else sum(counter['confirmed_by_owner'][str(o)] for o in owners)
        if states.get('executed', 0) != recorded_confirmed or states.get('unconfirmed', 0):
            raise ValueError(f'execution evidence disagrees with native receipts in {path}')
        trainable = summary['trainable_frames']
        excluded = summary['excluded_frames']
        if (trainable != states.get('executed', 0) + states.get('wait', 0)
                or trainable + excluded != summary['frames']
                or set(states) - {'executed', 'wait', 'cancelled'}):
            raise ValueError(f'invalid execution credit partition in {path}')
        credit_totals['trainable_rows'] += trainable
        credit_totals['excluded_rows'] += excluded
        for key in totals:
            totals[key] += counter[key]
    if plan is not None and (seen != set(planned) or player_lanes != plan['expected_player_lanes']):
        raise ValueError('mixed round quota mismatch')
    mixed = {} if plan is None else dict(mixed_opponents=dict(version=plan['version'], plan_sha256=plan['sha256'],
        pool=plan['pool'], self_games=sum(m['kind']=='self' for m in plan['matches']),
        history_games=sum(m['kind']!='self' for m in plan['matches']), player_lanes=player_lanes,
        statistics=result_statistics(counters)))
    shaped = {}
    if plan is not None and plan.get('reward_config') is not None:
        from native_runner.training.event_reward_stats import summarize_overflow
        shaped = dict(reward_config=plan['reward_config'], elixir_overflow=summarize_overflow(counters))
    return dict(scheduler_version='event-scheduling.v3', games=len(logs), **totals, **credit_totals, **mixed, **shaped,
                execution_credit_contract='executed-act-or-wait.v1',
                **{k:0 for k in ERROR_COUNTERS}, passed=True)
