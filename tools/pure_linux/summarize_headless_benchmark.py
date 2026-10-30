"""Validate complete fixed-fixture runs before summarizing timed step loops."""
import argparse
import hashlib
import json
import re
import statistics
from pathlib import Path


def records(text, marker):
    return [dict(re.findall(r'(\w+)=([^ ]+)', line.split(marker, 1)[1]))
            for line in text.splitlines() if line.startswith(marker + ' ')]


def summarize(text, repeats=30, expected_tick=3601, expected_digest='6c959326dd2f09cc',
              completion='HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1'):
    metrics = [{key: int(value) for key, value in row.items()}
               for row in records(text, 'HEADLESS_BATTLE_METRICS')]
    memory = [{key: int(value) for key, value in row.items()}
              for row in records(text, 'HEADLESS_BATTLE_MEMORY')]
    resets = records(text, 'HEADLESS_BATTLE_RESET')
    ends = records(text, 'HEADLESS_GAME_STATE_STEP_RETURN')
    if repeats < 1 or not all(len(rows) == repeats for rows in (metrics, memory, resets, ends)):
        raise ValueError('incomplete or duplicated match records')
    for index, (metric, reset, end) in enumerate(zip(metrics, resets, ends), 1):
        if metric['completed'] != expected_tick or metric['trace'] != 0:
            raise ValueError('wrong tick count or trace enabled')
        if metric['wall_ns'] <= 0 or not 0 < metric['step_total_ns'] <= metric['wall_ns']:
            raise ValueError('invalid timing')
        if metric['end_ns'] - metric['start_ns'] != metric['wall_ns']:
            raise ValueError('inconsistent timing window')
        if index > 1 and metric['start_ns'] < metrics[index - 2]['end_ns']:
            raise ValueError('overlapping match windows')
        if not metric['step_min_ns'] <= metric['p50_ns'] <= metric['p95_ns'] <= metric['p99_ns'] <= metric['step_max_ns']:
            raise ValueError('invalid step percentiles')
        if (int(reset['iteration']) != index or int(reset['repeats']) != repeats
                or reset['digest'] != expected_digest or reset['equal'] != '1'
                or reset['validated'] != '1' or reset['bytes'] != '3091'):
            raise ValueError('terminal serialization validation failed')
        if (end['tick_before'] != '0' or int(end['tick_after']) != expected_tick
                or int(end['completed']) != expected_tick or end['finalized'] != '1'):
            raise ValueError('native finalization missing')
    if (completion not in text.splitlines()
            or 'HEADLESS_REJECTED_PM_CALLS=0' not in text.splitlines()
            or 'HEADLESS_BATTLE_READY=UNVERIFIED' not in text.splitlines()):
        raise ValueError('normal completion or explicit readiness boundary missing')
    total_ns = sum(row['wall_ns'] for row in metrics)
    def sampled_peak(key):
        available = [row[key] for row in memory if row[key] >= 0]
        return max(available) / 1024 if available else None
    return dict(schema='nashura.headless-benchmark.v1', matches=repeats, tick=expected_tick,
                terminal_digest=expected_digest, terminal_bytes=3091, trace=False,
                median_match_ms=statistics.median(row['wall_ns'] for row in metrics) / 1e6,
                timed_seconds=total_ns / 1e9, ticks_per_second=repeats * expected_tick * 1e9 / total_ns,
                matches_per_second=repeats * 1e9 / total_ns,
                step_percentiles_us={key: statistics.median(row[key + '_ns'] for row in metrics) / 1000
                                     for key in ('p50', 'p95', 'p99')},
                peak_rss_mib=max(row['peak_rss_kb'] for row in metrics) / 1024,
                sampled_rss_peak_mib=sampled_peak('rss_kb'),
                sampled_pss_peak_mib=sampled_peak('pss_kb'),
                memory_sample_counts={key: sum(row[key] >= 0 for row in memory)
                                      for key in ('rss_kb', 'pss_kb')},
                samples=metrics, memory_samples=memory,
                scope='step loop only; initialization, reset and terminal validation excluded',
                readiness='UNVERIFIED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--repeats', type=int, default=30)
    parser.add_argument('--completion', default='HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    content = args.log.read_bytes()
    result = summarize(content.decode(), args.repeats, completion=args.completion)
    result['log_sha256'] = hashlib.sha256(content).hexdigest()
    if args.output:
        with args.output.open('x') as stream:
            json.dump(result, stream, indent=2)
            stream.write(chr(10))
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ('samples', 'memory_samples')}, indent=2))


if __name__ == '__main__':
    main()
