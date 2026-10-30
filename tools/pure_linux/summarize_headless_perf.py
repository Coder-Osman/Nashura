"""Attribute software CPU-clock samples only inside validated warm step windows.

Inputs are retained perf script (--ns), MMAP2 text, evlist (-v), report,
runner plan and stdout. This is attribution, never an unprofiled speed result.
Only the pinned libg is mapped; normalized addresses are ELF file offsets.
"""
import argparse
from bisect import bisect_right
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from run_headless_match import ENGINE_SHA256, verify_completion
from summarize_headless_benchmark import summarize as validate_benchmark


ENGINE_PATH = '/data/local/tmp/libg-input/libg.so'
HEADER = re.compile(r'^(.*?)\s+(\d+)/(\d+)\s+(\d+\.\d{9}):\s+cpu-clock:u:\s*$')
FRAME = re.compile(r'^\s+([0-9a-f]+)\s+.+\s+\((.+)\)\s*$')
MMAP = re.compile(r'(\d+\.\d{9}): PERF_RECORD_MMAP2 (\d+)/\d+: '
                  r'\[(0x[0-9a-f]+)\((0x[0-9a-f]+)\) @ (0x[0-9a-f]+|0) [^]]+\]: '
                  r'\S*x\S* (/\S+)')


def timestamp_ns(text):
    seconds, fraction = text.split('.')
    return int(seconds) * 10**9 + int(fraction)


def sample_records(text):
    samples = []
    current = None
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            comm, pid, tid, time = header.groups()
            current = dict(comm=comm.strip(), pid=int(pid), tid=int(tid),
                           time_ns=timestamp_ns(time), frames=[])
            samples.append(current)
        elif line.strip():
            frame = FRAME.fullmatch(line)
            if current is None or frame is None:
                raise ValueError('unexpected perf script format; require --ns and specified fields')
            current['frames'].append((int(frame[1], 16), frame[2]))
        else:
            current = None
    if not samples or any(not row['frames'] for row in samples):
        raise ValueError('empty or incomplete sample callchains')
    return samples


def engine_mappings(text):
    mappings = []
    for match in MMAP.finditer(text):
        time, pid, address, size, offset, path = match.groups()
        if path == ENGINE_PATH:
            mappings.append((timestamp_ns(time), int(pid), int(address, 16),
                             int(size, 16), int(offset, 0)))
    if not mappings:
        raise ValueError('missing executable pinned libg MMAP2 records')
    return mappings


def file_offset(ip, pid, time_ns, mappings):
    candidates = [row for row in mappings if row[1] == pid and row[0] <= time_ns
                  and row[2] <= ip < row[2] + row[3]]
    if not candidates:
        raise ValueError('sample libg IP has no preceding process mapping')
    latest = max(candidates, key=lambda row: row[0])
    return ip - latest[2] + latest[4]


def summarize(log, script, mappings, evlist, report, plan, exits, *, repeats,
              warmup_matches, expected_tick=3601, expected_digest='6c959326dd2f09cc',
              expected_bytes=3091, minimum_samples=100):
    if len(exits) != 3 or any(code != 0 for code in exits):
        raise ValueError('runner, perf and wrapper must all exit normally')
    if minimum_samples < 1:
        raise ValueError('positive sample minimum required')
    verify_completion(exits[0], log, repeats)
    benchmark = validate_benchmark(log, repeats, expected_tick, expected_digest,
                                   warmup_matches=warmup_matches, expected_bytes=expected_bytes)
    env = plan['environment']
    if (plan.get('benchmark_repeats') != repeats or plan.get('snapshot_trace') is not False
            or env.get('NASHURA_RESET_REPEATS') != str(repeats)
            or any(env.get(key) != '1' for key in ('NASHURA_BENCHMARK_MINIMAL', 'NASHURA_QUIET',
                                                 'NASHURA_QUIET_SNAPSHOTS', 'NASHURA_BENCHMARK_VALIDATE'))
            or any(key in env for key in ('NASHURA_PHASE_TIMING', 'NASHURA_TRACE_SNAPSHOTS',
                                         'NASHURA_BATTLE_OBSERVATIONS'))):
        raise ValueError('not a minimal benchmark plan')
    engine_ids = [value for key, value in plan['identities'].items() if key.endswith(ENGINE_PATH)]
    if engine_ids != [ENGINE_SHA256]:
        raise ValueError('wrong pinned engine identity')
    if (len(evlist.strip().splitlines()) != 1 or not evlist.startswith('cpu-clock:u:')
            or not re.search(r'\buse_clockid: 1(?:,|\s|$)', evlist)
            or not re.search(r'\bclockid: 1(?:,|\s|$)', evlist)):
        raise ValueError('require one cpu-clock:u event explicitly using CLOCK_MONOTONIC')
    lost = re.findall(r'^# Total Lost Samples: (\d+)\s*$', report, re.MULTILINE)
    if lost != ['0']:
        raise ValueError('lost samples or missing perf lost-sample accounting')
    samples = sample_records(script)
    maps = engine_mappings(mappings)
    windows = benchmark['samples']
    starts = [row['start_ns'] for row in windows]
    regions = Counter()
    threads = Counter()
    dsos = Counter()
    leaf = Counter()
    inclusive = Counter()
    query_callchains = 0
    by_match = [0] * (repeats - warmup_matches)
    mainloop_pids = set()
    for row in samples:
        index = bisect_right(starts, row['time_ns']) - 1
        inside = index >= 0 and row['time_ns'] < windows[index]['end_ns']
        if not inside:
            regions['outside_step_windows'] += 1
            continue
        if index < warmup_matches:
            regions['warmup_step_windows'] += 1
            continue
        regions['measured_step_windows'] += 1
        by_match[index - warmup_matches] += 1
        threads[row['comm']] += 1
        dsos[Path(row['frames'][0][1]).name] += 1
        if row['comm'] != 'Mainloop':
            continue
        mainloop_pids.add(row['pid'])
        offsets = []
        for ip, path in row['frames']:
            if path == ENGINE_PATH:
                offsets.append(file_offset(ip, row['pid'], row['time_ns'], maps))
        if row['frames'][0][1] == ENGINE_PATH:
            leaf[offsets[0]] += 1
        inclusive.update(set(offsets))
        query_callchains += any(0xf34b3c <= offset < 0xf35064 for offset in offsets)
    count = regions['measured_step_windows']
    if count < minimum_samples or threads['Mainloop'] < minimum_samples or not leaf or len(mainloop_pids) != 1:
        raise ValueError('insufficient measured-window samples or non-unique Mainloop process')

    def top(counter):
        return [{'file_offset': hex(offset), 'samples': number}
                for offset, number in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))[:25]]

    return dict(schema='nashura.headless-warm-perf.v1', whole_goal_complete=False,
                engine_sha256=ENGINE_SHA256, clock='CLOCK_MONOTONIC', event='cpu-clock:u',
                raw_sample_count=len(samples), lost_samples=0, warmup_matches=warmup_matches,
                measured_matches=repeats - warmup_matches, sample_regions=dict(regions),
                measured_samples_by_match=by_match, measured_thread_samples=dict(threads),
                measured_leaf_dso_samples=dict(dsos), mainloop_libg_leaf_samples=sum(leaf.values()),
                libg_leaf_top=top(leaf), libg_callchain_top=top(inclusive),
                spatial_query_leaf_samples=sum(n for offset, n in leaf.items() if 0xf34b3c <= offset < 0xf35064),
                spatial_query_callchain_samples=query_callchains,
                scope='only samples in measured warm step-loop windows; initialization, warmup, reset and terminal serialization excluded',
                limitations=['sample shares are not per-function latency or causal speedup',
                             'callchain addresses are return PCs; inclusive totals can overlap',
                             'file offsets require ELF PT_LOAD verification before interpreting as virtual offsets',
                             'profiled timing is not a performance baseline'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('log', 'script', 'mappings', 'evlist', 'report', 'plan',
                 'runner-exit', 'perf-exit', 'wrapper-exit'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--repeats', type=int, required=True)
    parser.add_argument('--warmup-matches', type=int, required=True)
    parser.add_argument('--minimum-samples', type=int, default=100)
    parser.add_argument('--expected-tick', type=int, default=3601)
    parser.add_argument('--expected-digest', default='6c959326dd2f09cc')
    parser.add_argument('--expected-bytes', type=int, default=3091)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names = ('log', 'script', 'mappings', 'evlist', 'report', 'plan',
             'runner_exit', 'perf_exit', 'wrapper_exit')
    blobs = {name: getattr(args, name).read_bytes() for name in names}
    result = summarize(*(blobs[name].decode() for name in names[:5]),
                       json.loads(blobs['plan']), [int(blobs[name]) for name in names[6:]],
                       repeats=args.repeats, warmup_matches=args.warmup_matches,
                       expected_tick=args.expected_tick, expected_digest=args.expected_digest,
                       expected_bytes=args.expected_bytes, minimum_samples=args.minimum_samples)
    result['source_sha256'] = {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()}
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
