"""Per-match native/reward audit, overlapping collection and reusing verified shards.

The round barrier still checks complete quotas, all native receipts, ownership,
reward conservation and the exact on-disk evidence before the learner runs.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import multiprocessing
from types import SimpleNamespace
from pathlib import Path
import gzip
import hashlib
import json
import math
import threading
import time

from native_runner.training.event_rollout import training_segments
from native_runner.training.event_rewards import (
    UNCAPPED_OVERFLOW_REWARD_CONFIG as V2,
    TOWER_OVERFLOW_REWARD_CONFIG as WEAK,
    STRONG_TOWER_OVERFLOW_REWARD_CONFIG as OLD, reward_components,
    LOW_OVERFLOW_TOWER_REWARD_CONFIG,
    MODERATE_TOWER_OVERFLOW_REWARD_CONFIG,
)
from native_runner.training import event_rewards
V4 = getattr(event_rewards, "SETTLEMENT_REWARD_CONFIG", None)
TOWER_CONFIGS = (OLD, LOW_OVERFLOW_TOWER_REWARD_CONFIG, MODERATE_TOWER_OVERFLOW_REWARD_CONFIG)
from native_runner.training.event_opponents import validate_plan
from native_runner.training.event_shard_cache import VerifiedShardCache
from tools.event_rl_round_validation import validate_round


def evidence_digest(folder, *, settlement=True):
    summaries = list(folder.glob('part-*.json'))
    if len(summaries) != 1:
        raise ValueError('expected exactly one shard per completed match')
    summary = json.loads(summaries[0].read_text())
    name = summary['data_file']
    if Path(name).name != name:
        raise ValueError('invalid shard basename')
    paths = [summaries[0], folder/name, folder/'collector.log']
    if settlement:
        paths.append(folder/'settlement-audit.json.gz')
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def audit_match(folder, match, plan, cache):
    initial_evidence = evidence_digest(folder, settlement=plan['reward_config'] not in TOWER_CONFIGS)
    shard = cache.load(next(folder.glob('part-*.json')), verify=True)
    return audit_parsed_match(folder, match, plan, shard.records, shard.manifest['contract'], initial_evidence)


def audit_tower_match(folder, match, plan, records, contract, initial_evidence):
    """Keep the tower audit and its same-trace beta=.1 control."""
    factor = plan['reward_config']['tower_potential']['coefficient'] / WEAK['tower_potential']['coefficient']
    owners = (0, 1) if match['kind'] == 'self' else (match['learner_owner'],)
    if {r.owner for r in records} != set(owners) or contract['reward_config'] != plan['reward_config']:
        raise ValueError('tower audit owner or reward contract mismatch')
    fields = ('owner', 'observation_tick', 'decision_ticks', 'execution_status', 'act',
              'card_row', 'cell', 'delay_bin', 'log_prob', 'base_reward', 'overflow_penalty')
    trace = hashlib.sha256(json.dumps([
        {k: getattr(row, k) for k in fields} for row in records], sort_keys=True).encode()).hexdigest()
    totals, components = Counter(), Counter()
    error_max = 0.
    for owner in owners:
        rows = [r for r in records if r.owner == owner]
        plain = training_segments(rows, reward_config=V2)
        weak = training_segments(rows, reward_config=WEAK)
        shaped = training_segments(rows, reward_config=plan['reward_config'])
        if not ([i for i, _ in plain] == [i for i, _ in weak] == [i for i, _ in shaped]):
            raise ValueError('tower audit credit partition mismatch')
        for (_, a), (_, b), (_, old) in zip(plain, shaped, weak):
            if not (a.trainable and b.trainable
                    and all(getattr(a, k) == getattr(b, k) for k in fields)
                    and math.isclose(b.tower_shaping, factor * old.tower_shaping, abs_tol=1e-9)
                    and math.isclose(b.reward - a.reward, b.tower_shaping, abs_tol=1e-9)):
                raise ValueError('tower audit reward conservation mismatch')
            totals['effective_decisions'] += 1
            totals['zero_reward_control'] += int(abs(a.reward) < 1e-8)
            totals['zero_reward_tower'] += int(abs(b.reward) < 1e-8)
        totals['lanes'] += 1
        totals['cancelled_decisions'] += sum(not r.trainable for r in rows)
        values = reward_components(rows, plan['reward_config'])
        error_max = max(error_max, values['tower_telescoping_error_max'])
        components.update({k: v for k, v in values.items() if k != 'tower_telescoping_error_max'})
    if evidence_digest(folder, settlement=False) != initial_evidence:
        raise ValueError('match evidence changed during audit')
    components['tower_telescoping_error_max'] = error_max
    return dict(totals=dict(totals), components=dict(components), diagnostics={},
                traces={match['game_id']: trace}, terminal_reconciliations={}, evidence=initial_evidence)


def audit_parsed_match(folder, match, plan, records, contract, initial_evidence):
    if plan['reward_config'] in TOWER_CONFIGS:
        return audit_tower_match(folder, match, plan, records, contract, initial_evidence)
    # Only small Python evidence crosses the process boundary; tensors stay in
    # the learner's SHA cache, avoiding tensor IPC/storage duplication.
    shard = SimpleNamespace(records=records, manifest={'contract': contract})
    totals, components, diagnostics = Counter(), Counter(), Counter()
    error_max = 0.
    traces, terminal_reconciliations = {}, {}
    owners = (0,1) if match['kind']=='self' else (match['learner_owner'],)
    assert {r.owner for r in shard.records} == set(owners)
    assert shard.manifest['contract']['reward_config'] == plan['reward_config'] == V4
    traces[match['game_id']] = hashlib.sha256(json.dumps(
        [r.as_dict for r in shard.records],sort_keys=True).encode()).hexdigest()
    with gzip.open(folder/'settlement-audit.json.gz','rt') as f: native = json.load(f)
    assert native
    if native[-1]['event'] != 'TERMINAL':
        from native_runner.training.settlement_reward.terminal import reconcile_terminal
        counter = json.loads((folder/'collector.log').read_text().splitlines()[-1])
        proof = reconcile_terminal(native,counter)
        for owner in owners:
            rows=[r for r in shard.records if r.owner==owner]
            assert rows[-1].done and not rows[-1].truncated
            assert rows[-1].observation_tick+rows[-1].decision_ticks==proof['tick']
        proof['original_audit_sha256']=hashlib.sha256((folder/'settlement-audit.json.gz').read_bytes()).hexdigest()
        terminal_reconciliations[match['game_id']]=proof
        diagnostics['TERMINAL_INFERRED_DEAD_KING'] += 1
        diagnostics['TERMINAL'] += 1
    assert all(e['native_snapshot']['native_order_complete'] for e in native)
    diagnostics['sampled_frames'] += len(native)
    for e in native:
        event=e['event']
        if event:diagnostics[event] += 1
        if event=='FULL_SETTLEMENT':
            assert e['verdict']['eligible']
            diagnostics['settlements_with_residuals'] += int(e['verdict']['unit_count']>0)
    for before,after in zip(native,native[1:]):
        diagnostics['native_hp_intervals'] += 1
        hp_before=[v for s in before['native_snapshot']['sides'] for v in [*s['princess_hp'],s['king_hp']]]
        hp_after=[v for s in after['native_snapshot']['sides'] for v in [*s['princess_hp'],s['king_hp']]]
        diagnostics['native_hp_changed_intervals'] += int(hp_before!=hp_after)
    for owner in owners:
        rows=[r for r in shard.records if r.owner==owner]
        plain=training_segments(rows,reward_config=V2)
        shaped=training_segments(rows,reward_config=V4)
        old=training_segments(rows,reward_config=OLD)
        from native_runner.training.settlement_reward.audit import decision_audit
        counterfactual=decision_audit(rows,V4)
        diagnostics['old_overflow_abs_same_trace'] += counterfactual['absolute_reward_mass']['old_overflow_penalty']
        assert [i for i,_ in plain]==[i for i,_ in shaped]==[i for i,_ in old]
        for (i,a),(j,b),(_,c) in zip(plain,shaped,old):
            assert a.trainable and b.trainable and b.tower_shaping==0
            assert math.isclose(b.reward-a.reward,b.settlement_shaping,abs_tol=1e-9)
            totals['effective_decisions'] += 1
            diagnostics['old_tower_abs_same_trace'] += abs(c.tower_shaping)
        totals['lanes'] += 1
        totals['cancelled_decisions'] += sum(not r.trainable for r in rows)
        values=reward_components(rows,V4)
        error_max=max(error_max,values['settlement_telescoping_error_max'])
        components.update({k:v for k,v in values.items() if k!='settlement_telescoping_error_max'})
    if evidence_digest(folder) != initial_evidence:
        raise ValueError('match evidence changed during audit')
    components['settlement_telescoping_error_max'] = error_max
    return dict(totals=dict(totals), components=dict(components), diagnostics=dict(diagnostics),
                traces=traces, terminal_reconciliations=terminal_reconciliations, evidence=initial_evidence)


class RoundAuditPool:
    def __init__(self, directory, *, shard_cache=None, workers=4, processes=True):
        self.directory = Path(directory).resolve()
        self.plan_bytes = (self.directory/'match-plan.json').read_bytes()
        self.plan = validate_plan(json.loads(self.plan_bytes))
        if self.plan.get('reward_config') not in TOWER_CONFIGS and (V4 is None or self.plan.get('reward_config') != V4):
            raise ValueError('round auditor requires an exact supported reward contract')
        self.settlement = self.plan['reward_config'] not in TOWER_CONFIGS
        self.error_key = 'settlement_telescoping_error_max' if self.settlement else 'tower_telescoping_error_max'
        self.cache = shard_cache if shard_cache is not None else VerifiedShardCache()
        self.matches = {('host-'+m['host'], 'port-'+str(m['port'])): m for m in self.plan['matches']}
        self.processes = processes
        self.pool = (ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'))
                     if processes else ThreadPoolExecutor(max_workers=workers, thread_name_prefix='round-audit'))
        self.futures = {}
        self.lock = threading.Lock()
        self.started = time.perf_counter()
        self.closed = False
        self.evidence = {}

    def submit(self, folder):
        folder = Path(folder).resolve()
        if folder.parent.parent != self.directory/'collected':
            raise ValueError('audit folder outside frozen round')
        key = (folder.parent.name, folder.name)
        if key not in self.matches:
            raise ValueError('audit match outside frozen plan')
        with self.lock:
            if self.closed:
                raise ValueError('round audit is already closed')
            if key not in self.futures:
                if self.processes:
                    initial_evidence = evidence_digest(folder, settlement=self.settlement)
                    shard = self.cache.load(next(folder.glob('part-*.json')), verify=True)
                    self.futures[key] = self.pool.submit(audit_parsed_match, folder, self.matches[key], self.plan,
                        shard.records, shard.manifest['contract'], initial_evidence)
                else:
                    self.futures[key] = self.pool.submit(audit_match, folder, self.matches[key], self.plan, self.cache)
            elif self.futures[key].done():
                proof = self.futures[key].result()
                if evidence_digest(folder, settlement=self.settlement) != proof['evidence']:
                    raise ValueError('duplicate match changed its evidence')
        return dict(queued=True, game_id=self.matches[key]['game_id'])

    def finish(self):
        started = time.perf_counter()
        if (self.directory/'match-plan.json').read_bytes() != self.plan_bytes:
            raise ValueError('frozen plan changed during collection')
        # Also handles restart/offline replay: completed artifacts are re-audited.
        for host, port in self.matches:
            self.submit(self.directory/'collected'/host/port)
        totals, components, diagnostics = Counter(), Counter(), Counter()
        traces, terminal_reconciliations = {}, {}
        error_max = 0.
        try:
            for host, port in self.matches:
                value = self.futures[(host, port)].result()
                if evidence_digest(self.directory/'collected'/host/port, settlement=self.settlement) != value['evidence']:
                    raise ValueError('match evidence changed after audit')
                self.evidence[(host, port)] = value['evidence']
                totals.update(value['totals'])
                diagnostics.update(value['diagnostics'])
                error_max = max(error_max, value['components'][self.error_key])
                components.update({k:v for k,v in value['components'].items() if k != self.error_key})
                traces.update(value['traces'])
                terminal_reconciliations.update(value['terminal_reconciliations'])
            validation = validate_round(self.directory/'collected', self.plan['expected_games'],
                                        self.plan['current_sha256'], shard_cache=self.cache)
            if (totals['lanes'] != self.plan['expected_player_lanes']
                    or totals['effective_decisions'] != validation['trainable_rows']
                    or totals['cancelled_decisions'] != validation['excluded_rows']):
                raise ValueError('round audit quota or credit partition mismatch')
            components[self.error_key] = error_max
            return dict(validation=validation, totals=dict(totals), components=dict(components),
                diagnostics=dict(diagnostics), same_trace_counterfactual_verified=True, traces=traces,
                terminal_reconciliations=terminal_reconciliations,
                pipeline=dict(processes=self.processes, workers=self.pool._max_workers, barrier_seconds=time.perf_counter()-started,
                              elapsed_seconds=time.perf_counter()-self.started, shard_cache=self.cache.as_dict()))
        finally:
            self.close()

    def verify_evidence(self):
        if not self.closed or set(self.evidence) != set(self.matches):
            raise ValueError('complete audited round required before learning')
        if (self.directory/'match-plan.json').read_bytes() != self.plan_bytes:
            raise ValueError('frozen plan changed after audit')
        for (host, port), digest in self.evidence.items():
            if evidence_digest(self.directory/'collected'/host/port, settlement=self.settlement) != digest:
                raise ValueError('match evidence changed before learning')

    def close(self):
        if not self.closed:
            self.closed = True
            self.pool.shutdown(wait=True, cancel_futures=True)
