"""Capture skill-only IL features from a bounded, selected raw replay JSONL."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch
from native_runner.cr_native_env import NativeClashEnv
from native_runner.royaleapi_replay import prepare_collected_replay, calibrate_collected_replay_deal
from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.event_ability_migration import migrate_ability_checkpoint
from native_runner.training.event_ability_il import freeze_for_ability_il, frozen_state_digests, verify_frozen_state
from native_runner.training.event_ability_il_data import write_features
from native_runner.training.event_ability_il_producer import produce_ability_features, skill_policy_atomic_view
from native_runner.training.v4.factory import build_collected_replay_environment_v4, build_episode_tensorizers_v4
from native_runner.training.v4.cache_builder import _headless_calibration_probe


class SkillAuditNative(NativeClashEnv):
    def _validate_atomic_result(self, result):
        return super()._validate_atomic_result(skill_policy_atomic_view(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--payloads', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--negative-stride', type=int, default=20)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--stop-dir', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output directory must be new; completed shards must not be regenerated')
    torch.set_num_threads(args.threads)
    torch.manual_seed(20260925)
    loaded = load_event_checkpoint(args.source)
    model, _ = migrate_ability_checkpoint(loaded)
    model.to(args.device)
    freeze_for_ability_il(model)
    before = frozen_state_digests(model)
    args.output.mkdir(parents=True)
    native = SkillAuditNative(host=args.host, port=args.port, timeout=30.)
    # The invoking job must hold the endpoint's ownership lock for the entire
    # run. This tool resets matches, but never starts or shuts down a simulator.
    native.stop_resident_mode()
    reports = []
    stop_dir = args.stop_dir or args.output.parent
    with args.payloads.open() as stream:
        for index, line in enumerate(stream):
            if args.limit and index >= args.limit:
                break
            if (stop_dir/'STOP').exists() or (stop_dir/'BOUNDARY_STOP').exists():
                break
            payload = json.loads(line)
            started = time.monotonic()
            phase, environment = 'prepare', None
            result = dict(index=index, accepted=False)
            try:
                prepared = prepare_collected_replay(payload)
                result['replay_tag'] = prepared.replay_tag
                phase = 'calibrate'
                calibrated = calibrate_collected_replay_deal(prepared, _headless_calibration_probe(native))
                phase = 'native_features'
                environment = build_collected_replay_environment_v4(calibrated, native=native)
                tensorizers = build_episode_tensorizers_v4(calibrated.replay.episode_config)
                def progress(values):
                    state = dict(index=index, replay_tag=prepared.replay_tag, phase=phase,
                                 seconds=time.monotonic() - started, **values)
                    (args.output/'progress.json').write_text(json.dumps(state, indent=2))
                    print(json.dumps(dict(progress=state)), flush=True)
                features, rows, metrics = produce_ability_features(environment, calibrated, tensorizers, model,
                    payload=payload, negative_stride=args.negative_stride, progress=progress)
                phase = 'write_readback'
                file = args.output/f'replay-{index:05d}.pt'
                sha = write_features(file, features, rows, parent_sha256=loaded.checkpoint_sha256)
                result.update(metrics, accepted=True, feature_sha256=sha, file=str(file.resolve()))
            except Exception as error:
                result.update(phase=phase, error=f'{type(error).__name__}: {error}')
            finally:
                if environment is not None:
                    environment.close()
            result['seconds'] = time.monotonic() - started
            reports.append(result)
            (args.output/f'replay-{index:05d}.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result), flush=True)
            (args.output/'status.json').write_text(json.dumps(dict(completed=len(reports),
                accepted=sum(row['accepted'] for row in reports), latest=result), indent=2))
    verify_frozen_state(model, before)
    native.close_transport()
    report = dict(schema='event-ability-head-native-capture.v1', parent_sha256=loaded.checkpoint_sha256,
        payloads_sha256=hashlib.sha256(args.payloads.read_bytes()).hexdigest(), results=reports,
        accepted=sum(row['accepted'] for row in reports), frozen_encoder_unchanged=True)
    (args.output/'report.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
