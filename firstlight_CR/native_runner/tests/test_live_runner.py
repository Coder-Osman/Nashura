"""Exercise the live controller while an input worker is still in flight."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from native_runner.live import run
from native_runner.live.events import PublicEventCursor
from native_runner.live.touch import TouchCancelled, TouchEnded


def run_scenario(tmp_path, monkeypatch, *, committed=False, confirmation=False, terminal=False,
                 delay_ms=100, input_lead_ms=1000, terminal_error=False, terminal_epoch=None,
                 terminal_winner=None):
    base = json.loads((Path(__file__).parent/'fixtures/live-chef-towers.json').read_text())
    base['public_events'] = dict(schema='cr-live-public-events.v1', first_sequence=1,
                                next_sequence=1, started_tick=0, complete=True, events=[])
    rows = []
    for sequence, tick in enumerate((150, 152, 153, 155, 156), 1):
        row = copy.deepcopy(base)
        row.update(tick=tick, sequence=sequence, ended=sequence >= (3 if terminal else 5))
        if sequence >= 3 and not terminal:
            row['public_events']['events'] = [dict(sequence=1, tick=153,
                owner=1-row['own_side'], card_id=28000011, cost=2, form_code=0,
                source='native-consume-card.v1', card_identity='consumed-descriptor')]
            row['public_events']['next_sequence'] = 2
        rows.append(row)
    calls, sessions, workers = [], [], []
    cursor = PublicEventCursor()
    current = dict(index=0)

    def latest(_path):
        assert current['index'] < len(rows), 'controller did not stop at terminal'
        row = rows[current['index']]
        current['index'] += 1
        row['received_monotonic_ns'] = time.monotonic_ns()
        return row

    class Policy:
        def __init__(self, *_args, **_kwargs):
            self.loaded = SimpleNamespace(checkpoint_sha256='a'*64)
            self.seed, self.lightning, self.log = 42, None, None
            self.model = SimpleNamespace(event_config=SimpleNamespace(max_wait_ticks=300))
            self.adapter = SimpleNamespace(fork=lambda:None)

        def observe(self, row):
            calls.append(('observe', row['tick']))
            return cursor.update(row)

        def infer(self, row):
            calls.append(('infer', row['tick']))
            card = row['own']['hand'][0]['card']['card_id']
            return dict(event='decision', tick=row['tick'], battle_epoch=row['battle_epoch'],
                        act=row['tick'] == 150, delay_ms=delay_ms, card_id=card, slot=0,
                        native_cell=[9, 20], own_side=row['own_side'], event_watermark=0)

    class Session:
        def __init__(self, **_kwargs):
            self.closed = False
            sessions.append(self)
        def close(self):
            self.closed = True

    class Worker:
        def __init__(self, **_kwargs):
            self.closed = False
            workers.append(self)
        def submit(self, _fn, **kwargs):
            calls.append(('dispatch', kwargs['decision_timing']))
            self.transaction = kwargs['transaction']
            if committed:
                self.transaction.commit(lambda: rows[1])
            return self
        def done(self):
            return current['index'] >= 3
        def result(self):
            if not committed:
                assert self.transaction.reason is not None
                raise TouchCancelled(self.transaction.reason)
            if terminal_error:
                after = copy.deepcopy(rows[2])
                after['ended'] = True
                after['winner'] = after['own_side'] if terminal_winner is None else terminal_winner
                if terminal_epoch is not None:
                    after['battle_epoch'] = terminal_epoch
                raise TouchEnded(after)
            if not confirmation:
                raise run.LiveStateError('no matching native play receipt')
            return dict(after_tick=154, target_touch_tick=152, touch_sent_tick=152,
                        native_receipt=dict(sequence=2, tick=154), hand_touch_ms=22, target_touch_ms=22)
        def shutdown(self, **_kwargs):
            self.closed = True

    monkeypatch.setattr(run, 'PartialLivePolicy', Policy)
    monkeypatch.setattr(run, 'TouchSession', Session)
    monkeypatch.setattr(run, 'ThreadPoolExecutor', Worker)
    monkeypatch.setattr(run, 'latest_snapshot', latest)
    monkeypatch.setattr(run, 'action_constraints_contract', lambda *_args:{})
    projection = SimpleNamespace(max_residual_px=1, screen=lambda *_args, **_kwargs:(540, 1000))
    monkeypatch.setattr(run.TouchProjection, 'from_files', lambda _files:projection)
    args = SimpleNamespace(calibration=[], checkpoint=Path('unused'), output=tmp_path/'policy.jsonl',
                           serial='test', capture=tmp_path/'capture.jsonl', execute=True, seconds=5,
                           input_lead_ms=input_lead_ms)
    if committed and not confirmation and (not terminal_error or terminal_epoch not in (None, base['battle_epoch'])):
        with pytest.raises(RuntimeError, match='unconfirmed committed touch'):
            run.run_match(args)
    else:
        run.run_match(args)
    assert sessions[0].closed and workers[0].closed
    return calls, [json.loads(line) for line in args.output.read_text().splitlines()]


def test_controller_keeps_observing_and_cancels_uncommitted_worker(tmp_path, monkeypatch):
    calls, logs = run_scenario(tmp_path, monkeypatch)
    assert ('observe', 153) in calls
    assert any(x['event'] == 'public_card_plays' for x in logs)
    assert any(x['event'] == 'cancelled' and 'new public card play' in x['reason'] for x in logs)
    assert logs[-1]['event'] == 'terminal'
    assert logs[-1]['counts'] == dict(decisions=2, submitted=0, confirmed=0, cancelled=1)


def test_committed_worker_observes_new_play_then_confirms_once(tmp_path, monkeypatch):
    calls, logs = run_scenario(tmp_path, monkeypatch, committed=True, confirmation=True)
    assert ('observe', 153) in calls
    assert [x['event'] for x in logs].count('submitted') == 1
    assert [x['event'] for x in logs].count('confirmed') == 1
    assert logs[-1]['counts'] == dict(decisions=2, submitted=1, confirmed=1, cancelled=0)


@pytest.mark.parametrize('terminal', [False, True])
def test_uncertain_committed_touch_stops_without_retry(tmp_path, monkeypatch, terminal):
    calls, logs = run_scenario(tmp_path, monkeypatch, committed=True, terminal=terminal)
    assert logs[-1]['event'] == 'failed'
    assert [tick for kind, tick in calls if kind == 'infer'] == [150]
    assert logs[-1]['counts'] == dict(decisions=1, submitted=1, confirmed=0, cancelled=0)


@pytest.mark.parametrize('winner', [None, -1, 0, 1])
def test_committed_touch_pending_at_terminal_preserves_result(tmp_path, monkeypatch, winner):
    calls, logs = run_scenario(tmp_path, monkeypatch, committed=True, terminal=True,
                              terminal_error=True, terminal_winner=winner)
    assert [e['event'] for e in logs][-2:] == ['unconfirmed_at_terminal', 'terminal']
    assert logs[-1]['same_battle'] is True
    assert logs[-1]['winner'] == (logs[-1]['own_side'] if winner is None else winner)
    assert logs[-1]['counts'] == dict(decisions=1, submitted=1, confirmed=0, cancelled=0)
    assert len([x for x in calls if x[0] == 'dispatch']) == 1


def test_another_battles_terminal_does_not_resolve_pending_touch(tmp_path, monkeypatch):
    _calls, logs = run_scenario(tmp_path, monkeypatch, committed=True, terminal=True,
                               terminal_error=True, terminal_epoch=999)
    assert logs[-1]['event'] == 'failed'
    assert not any(e['event'] == 'terminal' for e in logs)


def test_confirmed_touch_at_terminal_still_counts_as_confirmed(tmp_path, monkeypatch):
    _calls, logs = run_scenario(tmp_path, monkeypatch, committed=True, confirmation=True, terminal=True)
    assert logs[-1]['event'] == 'terminal'
    assert logs[-1]['counts']['confirmed'] == 1
    assert not any(e['event'] == 'unconfirmed_at_terminal' for e in logs)


def test_terminal_cancels_uncommitted_worker_and_closes_input(tmp_path, monkeypatch):
    _calls, logs = run_scenario(tmp_path, monkeypatch, terminal=True)
    assert logs[-1]['event'] == 'terminal'
    assert logs[-1]['counts'] == dict(decisions=1, submitted=0, confirmed=0, cancelled=1)


@pytest.mark.parametrize('delay_ms, lead_ms, click_tick, policy_tick', [
    (100, 1000, 150, 152), (1000, 1000, 150, 170), (1100, 1000, 152, 172),
    (100, 0, 152, 152),
])
def test_runner_dispatches_compensated_plan_with_original_timing(tmp_path, monkeypatch,
        delay_ms, lead_ms, click_tick, policy_tick):
    calls, logs = run_scenario(tmp_path, monkeypatch, committed=True, confirmation=True,
                               delay_ms=delay_ms, input_lead_ms=lead_ms)
    timing, = [value for kind, value in calls if kind == 'dispatch']
    decision = next(row for row in logs if row['event'] == 'decision')
    assert timing['selected_delay_ms'] == decision['delay_ms'] == delay_ms
    assert timing['scheduled_tick'] == decision['scheduled_tick'] == click_tick
    assert timing['policy_scheduled_tick'] == decision['policy_scheduled_tick'] == policy_tick
    assert timing['input_lead_ms'] == logs[0]['input_lead_ms'] == lead_ms
    assert timing['scheduled_delay_ms'] == (click_tick-150)*50
    assert timing['due_observed_tick'] == 152
    assert logs[-1]['counts']['confirmed'] == 1
