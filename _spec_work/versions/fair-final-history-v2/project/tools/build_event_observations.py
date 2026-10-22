"""Capture event-boundary observations from collected replays.

Replays collected RoyaleAPI replays in the native engine, lands on every
scheduled command, and tensorizes the per-owner semantic observation at each
public event observation tick (one tick after a recorded card play, matching
the native ring).  Every frame is paired with the expert next-action label
(card, cell, 100ms-quantized delay, or WAIT).  Output shards feed the
event-policy pretraining entry.

Usage:
    .venv/bin/python tools/build_event_observations.py \
        /data/firstlight/datasets/IL_Replay /data/firstlight/cache/event-obs \
        --ports 26789,26790,26791,26792 --source-count 200 --replays-per-shard 1
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import islice
import json
import multiprocessing as mp
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.cr_native_env import NativeClashEnv  # noqa: E402
from native_runner.royaleapi_replay import (  # noqa: E402
    calibrate_collected_replay_deal,
    prepare_collected_replay,
)
from native_runner.training.event_cache import (  # noqa: E402
    EventObservationRecordV1,
    _atomic_write_json,
    default_event_contract,
    load_event_cache_shard,
    validate_event_supervised_contract,
    write_event_cache_index,
    write_event_cache_shard,
)
from native_runner.training.event_producer import (  # noqa: E402
    produce_event_observation_batch,
    produce_event_observation_batch_native,
)
from native_runner.training.v4.cache_builder import (  # noqa: E402
    _headless_calibration_probe,
    _source_stream,
)
from native_runner.training.v4.expert import (  # noqa: E402
    replay_expert_actions,
    require_accepted_timeline,
    screen_expert_timeline,
)
from native_runner.training.v4.factory import (  # noqa: E402
    build_episode_tensorizers_v4,
    build_resident_collected_replay_batch_v4,
)


def _convert_batch(
    *,
    native: NativeClashEnv,
    payload_batch,
    host: str,
    port: int,
    timeout: float,
    capture_mode: str,
    step_until_event: bool = False,
    max_advance_ticks: int = 300,
) -> tuple[list[EventObservationRecordV1], list, list[dict[str, object]], list[dict[str, object]]]:
    native.stop_resident_mode()
    probe = _headless_calibration_probe(native)
    prepared_rows: list[tuple[object, object, int]] = []
    failures: list[dict[str, object]] = []
    for source_index, payload in payload_batch:
        prepared = None
        phase = "prepare"
        try:
            prepared = prepare_collected_replay(payload)
            phase = "calibrate"
            calibrated = calibrate_collected_replay_deal(prepared, probe)
            phase = "screen"
            actions = replay_expert_actions(calibrated.replay)
            require_accepted_timeline(screen_expert_timeline(actions), replay_id=calibrated.replay_tag)
            phase = "tensorizers"
            tensorizers = build_episode_tensorizers_v4(calibrated.replay.episode_config)
            prepared_rows.append((calibrated, tensorizers, source_index))
        except Exception as error:
            tag = getattr(prepared, "replay_tag", None) or f"source-{source_index}"
            failures.append(
                {
                    "replay_tag": str(tag),
                    "source_index": source_index,
                    "phase": phase,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    records: list[EventObservationRecordV1] = []
    observations: list = []
    stats: list[dict[str, object]] = []
    if not prepared_rows:
        return records, observations, failures, stats
    prepared_rows.sort(key=lambda item: item[2])
    environments: tuple = ()
    try:
        environments, coordinator = build_resident_collected_replay_batch_v4(
            tuple(item[0] for item in prepared_rows),
            env_ids=tuple(range(len(prepared_rows))),
            host=host,
            port=port,
            timeout=timeout,
            capture_mode=capture_mode,
            step_until_event=step_until_event,
        )
        producer_args = dict(
            batch_coordinator=coordinator,
        )
        if step_until_event:
            producer_args["max_advance_ticks"] = max_advance_ticks
        results = (
            produce_event_observation_batch_native if step_until_event else produce_event_observation_batch
        )(
            environments,
            tuple(item[0] for item in prepared_rows),
            tuple(item[1] for item in prepared_rows),
            **producer_args,
        )
        for result in results:
            records.extend(result.records)
            observations.extend(result.observations)
            stats.append(
                {
                    "replay_tag": result.replay_tag,
                    "completed": result.completed,
                    "plays": result.play_count,
                    "captured_events": result.captured_events,
                    "legal_acts": sum(record.legal_at_trigger for record in result.records),
                    "act_frames": sum(not record.wait for record in result.records),
                    "in_hand": sum(record.card_in_hand for record in result.records),
                    "ring_mismatches": int(result.ring_mismatches),
                    "simulated_end_tick": int(result.simulated_end_tick),
                    "source_end_tick": int(result.source_end_tick),
                }
            )
            if not result.completed:
                failures.append(
                    {
                        "replay_tag": result.replay_tag,
                        "phase": "produce",
                        "error": result.failure_reason or "incomplete event replay",
                        "captured_events": result.captured_events,
                        "simulated_end_tick": result.simulated_end_tick,
                        "source_end_tick": result.source_end_tick,
                    }
                )
    except Exception as error:
        failures.append({"phase": "batch", "error": f"{type(error).__name__}: {error}"})
    finally:
        for environment in environments:
            try:
                environment.native.close()
            except Exception:
                pass
    return records, observations, failures, stats


def _run_port(
    *,
    port: int,
    host: str,
    chunks,
    cache_root: str,
    contract: dict,
    timeout: float,
    capture_mode: str,
    compression_level: int,
    partition_index: int,
    step_until_event: bool = False,
    max_advance_ticks: int = 300,
    resume: bool = True,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    validate_event_supervised_contract(contract)
    native = NativeClashEnv(host=host, port=port, timeout=timeout)
    native.wait_ready(timeout=timeout)
    summaries: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    stats: list[dict[str, object]] = []
    root = Path(cache_root)
    try:
        for shard_index, payload_batch in chunks:
            shard_id = f"part-{partition_index:02d}-shard-{shard_index:06d}"
            data_path = root / f"{shard_id}.crev.zst"
            summary_path = root / (data_path.name[: -len(".crev.zst")] + ".json")
            if resume and data_path.is_file() and summary_path.is_file():
                # Never publish a new contract over legacy/native label shards.
                existing = load_event_cache_shard(summary_path, verify=True)
                validate_event_supervised_contract(existing.manifest.get("contract"))
                del existing
                cached = json.loads(summary_path.read_text(encoding="utf-8"))
                cached["summary_file"] = summary_path.name
                cached["port"] = port
                summaries.append(cached)
                continue
            records, observations, batch_failures, batch_stats = _convert_batch(
                native=native,
                payload_batch=payload_batch,
                host=host,
                port=port,
                timeout=timeout,
                capture_mode=capture_mode,
                step_until_event=step_until_event,
                max_advance_ticks=max_advance_ticks,
            )
            for failure in batch_failures:
                failure["shard"] = shard_id
                failure["port"] = port
            for entry in batch_stats:
                entry["shard"] = shard_id
                entry["port"] = port
            failures.extend(batch_failures)
            stats.extend(batch_stats)
            if not records:
                print(f'{{"event":"event-shard-empty","shard":"{shard_id}","port":{port}}}', flush=True)
                continue
            summary = write_event_cache_shard(
                data_path,
                records,
                observations,
                shard_id=shard_id,
                contract=contract,
                compression_level=compression_level,
            )
            summary["summary_file"] = data_path.name[: -len(".crev.zst")] + ".json"
            summary["port"] = port
            summaries.append(summary)
            print(
                f'{{"event":"event-shard","shard":"{shard_id}","port":{port},'
                f'"frames":{summary["frames"]},"act_frames":{summary["act_frames"]},'
                f'"legal_act_frames":{summary["legal_act_frames"]},"replays":{summary["replays"]}}}',
                flush=True,
            )
    finally:
        try:
            native.stop_resident_mode()
        except Exception:
            pass
    return summaries, failures, stats


def _iter_partition_chunks(
    dataset_roots,
    *,
    source_count: int,
    partition_index: int,
    partition_count: int,
    replays_per_shard: int,
):
    """Yield ``(shard_index, batch)`` for one partition without buffering it.

    ``_select_partition`` returns every selected replay at once and the caller
    then hands each worker its entire share through the pickled task payload.
    On the full 252k-replay dataset that is tens of gigabytes: six workers each
    held ~4 GB, which on a 36 GB host exhausted swap.  This yields the same
    batches in the same order while keeping a single batch alive, so both the
    parent and the workers stay bounded.

    ``shard_index`` is still the batch's position in the selected list, so
    shard ids and ``--resume`` behave exactly as before.
    """

    stream = _source_stream(tuple(dataset_roots))
    selected_source = islice(stream, source_count) if source_count else stream
    seen_tags: set[str] = set()
    buffered: list = []
    shard_index = 0
    for source_index, _dataset_root, payload, report in selected_source:
        if source_index % partition_count != partition_index:
            continue
        if report.replay_tag in seen_tags:
            raise ValueError(f"duplicate replay tag in source selection: {report.replay_tag}")
        seen_tags.add(report.replay_tag)
        buffered.append((source_index, payload))
        if len(buffered) >= replays_per_shard:
            yield shard_index, buffered
            shard_index += 1
            buffered = []
    if buffered:
        yield shard_index, buffered


def _expected_partition_chunks(
    dataset_roots,
    *,
    source_count: int,
    partition_index: int,
    partition_count: int,
    replays_per_shard: int,
) -> int:
    """Expected shard count for one partition, read from Parquet footers only.

    Used for the informational ``selected_replays`` contract field before the
    stream is consumed; it never parses a replay.
    """

    import pyarrow.dataset as arrow_dataset
    import pyarrow.parquet as arrow_parquet

    total = 0
    for root in dataset_roots:
        dataset = arrow_dataset.dataset(Path(root) / "replays", format="parquet")
        for fragment in dataset.get_fragments():
            total += arrow_parquet.ParquetFile(fragment.path).metadata.num_rows
    considered = min(source_count, total) if source_count else total
    if considered <= partition_index:
        return 0
    payloads = (considered - partition_index + partition_count - 1) // partition_count
    return (payloads + replays_per_shard - 1) // replays_per_shard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("cache_root", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ports", required=True)
    parser.add_argument("--source-count", type=int, default=20)
    parser.add_argument("--partition-index", type=int, default=0)
    parser.add_argument("--partition-count", type=int, default=1)
    parser.add_argument("--replays-per-shard", type=int, default=1)
    parser.add_argument("--capture-mode", choices=("zlib-json", "json", "compact"), default="compact")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--compression-level", type=int, default=-5)
    parser.add_argument("--max-shards", type=int, default=0)
    parser.add_argument("--max-advance-ticks", type=int, default=300)
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="reuse existing finished shards instead of recomputing them",
    )
    parser.add_argument(
        "--step-until-event",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="advance lanes with the native step-until-event primitive",
    )
    parser.add_argument("--workers", type=int, default=0, help="0 uses one worker per port")
    parser.add_argument(
        "--round-shards",
        type=int,
        default=0,
        help="shards dispatched per pool round; bounds capture memory (0 = auto)",
    )
    parser.add_argument(
        "--pool-shards",
        type=int,
        default=1200,
        help="recycle the worker pool after this many shards (0 = never)",
    )
    args = parser.parse_args()

    ports = tuple(int(value) for value in args.ports.split(",") if value)
    if (
        not ports
        or len(set(ports)) != len(ports)
        or args.source_count <= 0
        or args.replays_per_shard <= 0
        or args.replays_per_shard > 16
        or (args.step_until_event and args.replays_per_shard != 1)
        or not 0 <= args.partition_index < args.partition_count
    ):
        raise ValueError(
            "event capture arguments are invalid (native step-until-event needs --replays-per-shard 1)"
        )

    cache_root = args.cache_root.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    dataset_roots = (args.dataset_root.resolve(),)
    contract = default_event_contract()
    contract["dataset_root"] = str(dataset_roots[0])
    expected_replays = _expected_partition_chunks(
        dataset_roots,
        source_count=args.source_count,
        partition_index=args.partition_index,
        partition_count=args.partition_count,
        replays_per_shard=args.replays_per_shard,
    )
    if args.max_shards > 0:
        expected_replays = min(expected_replays, args.max_shards)
    contract["selected_replays"] = expected_replays

    started = time.perf_counter()
    summaries: list[dict[str, object]] = []
    all_failures: list[dict[str, object]] = []
    all_stats: list[dict[str, object]] = []
    workers = args.workers or len(ports)
    # One round keeps at most this many replays alive in the parent, and it is
    # also all a worker is ever handed at once.
    round_shards = args.round_shards or max(workers * 50, 200)
    context = mp.get_context("spawn")
    selected_total = 0

    # Cumulative counters kept O(1) so a progress file can be rewritten every
    # round.  `index.json` and `stats.json` are only written when the whole run
    # ends, which leaves a multi-day capture with no on-disk record of the
    # correctness counters in between -- notably ring_mismatches.
    progress: dict[str, object] = {
        "schema": "event-observation-capture-progress.v1",
        "shards": 0,
        "empty_shards": 0,
        "frames": 0,
        "act_frames": 0,
        "wait_frames": 0,
        "legal_act_frames": 0,
        "replays": 0,
        "failures": 0,
        "ring_mismatches": 0,
        "rounds": 0,
    }
    progress_path = cache_root / "progress.json"

    def publish_progress(started_at: float) -> None:
        progress["elapsed_s"] = round(time.perf_counter() - started_at, 3)
        progress["selected_replays"] = selected_total
        _atomic_write_json(progress_path, progress)

    def run_round(executor: ProcessPoolExecutor, assignments: list[list]) -> None:
        futures = {
            executor.submit(
                _run_port,
                port=port,
                host=args.host,
                chunks=batches,
                cache_root=str(cache_root),
                contract=contract,
                timeout=args.timeout,
                capture_mode=args.capture_mode,
                compression_level=args.compression_level,
                step_until_event=args.step_until_event,
                max_advance_ticks=args.max_advance_ticks,
                resume=args.resume,
                partition_index=args.partition_index,
            ): port
            for port, batches in zip(ports, assignments, strict=True)
            if batches
        }
        dispatched = sum(len(batches) for batches in assignments)
        produced = 0
        for future in as_completed(futures):
            port_summaries, port_failures, port_stats = future.result()
            summaries.extend(port_summaries)
            all_failures.extend(port_failures)
            all_stats.extend(port_stats)
            produced += len(port_summaries)
            for summary in port_summaries:
                progress["shards"] += 1
                for key in ("frames", "act_frames", "wait_frames", "legal_act_frames", "replays"):
                    progress[key] += int(summary.get(key, 0))
            progress["failures"] += len(port_failures)
            for entry in port_stats:
                progress["ring_mismatches"] += int(entry.get("ring_mismatches", 0))
        # A dispatched chunk that produced no shard is an empty one: its replay
        # failed deal calibration.  Such shards write no file and are only
        # retried by a later pass.
        progress["empty_shards"] += dispatched - produced
        progress["rounds"] += 1

    # Worker RSS creeps up as torch and the tensorizer warm up, so the pool is
    # recycled every --pool-shards shards.  Recreating it costs a few seconds of
    # re-import per cycle, which is cheap next to a multi-day run.
    def make_pool() -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=workers, mp_context=context)

    executor = make_pool()
    try:
        assignments: list[list] = [[] for _ in ports]
        in_round = 0
        since_pool = 0
        shards_this_round = 0
        for shard_index, chunk in _iter_partition_chunks(
            dataset_roots,
            source_count=args.source_count,
            partition_index=args.partition_index,
            partition_count=args.partition_count,
            replays_per_shard=args.replays_per_shard,
        ):
            if args.max_shards > 0 and shard_index >= args.max_shards:
                break
            assignments[shard_index % len(ports)].append((shard_index, chunk))
            selected_total += 1
            in_round += 1
            if in_round >= round_shards:
                run_round(executor, assignments)
                assignments = [[] for _ in ports]
                since_pool += in_round
                in_round = 0
                publish_progress(started)
                print(
                    f'{{"event":"event-round","rounds":{progress["rounds"]},'
                    f'"shards":{progress["shards"]},"empty":{progress["empty_shards"]},'
                    f'"ring_mismatches":{progress["ring_mismatches"]},'
                    f'"selected":{selected_total}}}',
                    flush=True,
                )
                if args.pool_shards > 0 and since_pool >= args.pool_shards:
                    executor.shutdown(wait=True)
                    executor = make_pool()
                    since_pool = 0
        if any(assignments):
            run_round(executor, assignments)
        publish_progress(started)
    finally:
        executor.shutdown(wait=True)

    if selected_total != contract["selected_replays"]:
        # The contract value comes from Parquet footers; a row rejected during
        # streaming is the only way they can disagree.  The index owns the
        # authoritative count.
        print(
            f'{{"event":"selected-replays-mismatch","expected":{contract["selected_replays"]},'
            f'"observed":{selected_total}}}',
            flush=True,
        )
        contract["selected_replays"] = selected_total
    summaries.sort(key=lambda item: str(item["shard_id"]))
    index = write_event_cache_index(cache_root / "index.json", summaries, contract=contract)
    if all_failures:
        (cache_root / "failures.json").write_text(
            json.dumps(all_failures, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (cache_root / "stats.json").write_text(
        json.dumps(all_stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    elapsed = time.perf_counter() - started
    acts = int(index["act_frames"])
    print(
        json.dumps(
            {
                "ok": True,
                "cache_root": str(cache_root),
                "elapsed_s": round(elapsed, 3),
                "ports": len(ports),
                "shards": index["shard_count"],
                "frames": index["frames"],
                "act_frames": acts,
                "wait_frames": index["wait_frames"],
                "legal_act_frames": index["legal_act_frames"],
                "legal_act_fraction": round(index["legal_act_frames"] / max(1, acts), 6),
                "captured_events": sum(int(entry["captured_events"]) for entry in all_stats),
                "plays": sum(int(entry["plays"]) for entry in all_stats),
                "completed": sum(bool(entry["completed"]) for entry in all_stats),
                "failures": len(all_failures),
                "frames_per_second": round(index["frames"] / elapsed, 3) if elapsed else 0.0,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
