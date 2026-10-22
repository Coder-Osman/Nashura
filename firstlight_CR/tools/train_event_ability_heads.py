"""Train only the two new skill heads; preserve all existing policy state."""
from __future__ import annotations

import argparse
import copy
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from native_runner.training.event_checkpoint import load_event_checkpoint, save_event_checkpoint
from native_runner.training.event_ability_migration import migrate_ability_checkpoint
from native_runner.training.event_ability_il import (
    ABILITY_PARAMETER_NAMES, ability_il_loss, ability_il_step, frozen_state_digests,
    make_ability_optimizer, tensor_digest, training_state_digest, verify_frozen_state,
)
from native_runner.training.event_ability_il_data import concatenate_features, read_features


@torch.no_grad()
def evaluate(model, features, *, batch_size, device):
    total = {}
    for start in range(0, len(features.target), batch_size):
        batch = features.select(slice(start, start + batch_size)).to(device)
        loss, metrics = ability_il_loss(model, batch)
        n = len(batch.target)
        total["loss_sum"] = total.get("loss_sum", 0.) + float(loss) * n
        for key, value in metrics.items():
            if key.endswith("loss"):
                value = value * n
            total[key] = total.get(key, 0.) + float(value)
    n = total['rows']
    for key in ("gate_loss", "source_loss"):
        total[key] /= n
    total['loss'] = total.pop('loss_sum') / n
    total['precision'] = total['true_positive'] / max(1., total['true_positive'] + total['false_positive'])
    total['recall'] = total['true_positive'] / max(1., total['positives'])
    total['source_accuracy'] = total['source_correct'] / max(1., total['positives'])
    total['mean_skill_probability'] = total.pop('probability_sum') / n
    total['positive_fraction'] = total['positives'] / n
    return total


def train(args):
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    if args.output.exists():
        raise ValueError('output directory must be new')
    loaded = load_event_checkpoint(args.source)
    parent = loaded.checkpoint_sha256
    model, extra = migrate_ability_checkpoint(loaded)
    preserved = {key: training_state_digest(value) for key, value in extra.items()}
    before = frozen_state_digests(model)
    verify_frozen_state(loaded.model, before)
    model.to(args.device)
    optimizer = make_ability_optimizer(model, learning_rate=args.learning_rate)
    initial_heads = {k: tensor_digest(v) for k, v in model.state_dict().items() if k in ABILITY_PARAMETER_NAMES}
    parts = {'train': [], 'validation': []}
    metadata = {'train': [], 'validation': []}
    fingerprints = set()
    sources = []
    for path in sorted(args.cache.glob('*.pt')):
        features, rows = read_features(path, parent_sha256=parent)
        sources.append(dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        for row in rows:
            key = (row['replay_tag'], row['owner'], row['tick'])
            if key in fingerprints:
                raise ValueError('duplicate replay decision in skill cache')
            fingerprints.add(key)
        for partition in parts:
            ids = torch.tensor([i for i, row in enumerate(rows) if row['partition'] == partition], dtype=torch.long)
            if len(ids):
                parts[partition].append(features.select(ids))
                metadata[partition].extend(rows[i] for i in ids.tolist())
    data = {key: concatenate_features(value) for key, value in parts.items()}
    for key, batch in data.items():
        pos = int((batch.target >= 0).sum())
        if pos < args.minimum_positives or len(batch.target) - pos < args.minimum_positives:
            raise ValueError(f'{key} needs at least {args.minimum_positives} verified positive and negative rows')
    args.output.mkdir(parents=True)
    baseline = evaluate(model, data['validation'], batch_size=args.batch_size, device=args.device)
    best_loss, best_heads, best_epoch = baseline['loss'], None, 0
    history = []
    for epoch in range(1, args.epochs + 1):
        order = torch.randperm(len(data['train'].target))
        for ids in order.split(args.batch_size):
            ability_il_step(model, optimizer, data['train'].select(ids).to(args.device))
        verify_frozen_state(model, before)
        stats = dict(epoch=epoch, training=evaluate(model, data['train'], batch_size=args.batch_size, device=args.device),
                     validation=evaluate(model, data['validation'], batch_size=args.batch_size, device=args.device))
        history.append(stats)
        print(json.dumps(stats), flush=True)
        if stats['validation']['loss'] < best_loss:
            best_loss, best_epoch = stats['validation']['loss'], epoch
            best_heads = {k: v.detach().cpu().clone() for k, v in model.state_dict().items() if k in ABILITY_PARAMETER_NAMES}
            best_optimizer = copy.deepcopy(optimizer.state_dict())
        if epoch - best_epoch >= args.patience:
            break
    report = dict(schema='event-ability-head-il-report.v1', parent_sha256=parent, sources=sources,
        baseline_validation=baseline, history=history, best_epoch=best_epoch, frozen_state_sha256=before,
        hyperparameters={name: getattr(args, name) for name in
                         ('seed', 'learning_rate', 'batch_size', 'epochs', 'patience', 'minimum_positives')},
        trainable_parameters=sorted(ABILITY_PARAMETER_NAMES),
        partitions={key: dict(replays=len({row['replay_tag'] for row in rows}), rows=len(rows)) for key, rows in metadata.items()})
    report['positive_ability_counts'] = {
        split: dict(Counter(row['receipt']['data']['ability_id'] for row in rows
                            if row['expert_skill_next_tick']))
        for split, rows in metadata.items()
    }
    if best_heads is None:
        report.update(status='validation_did_not_improve', checkpoint=None)
        (args.output/'report.json').write_text(json.dumps(report, indent=2))
        raise ValueError('skill validation did not improve; no trained checkpoint accepted')
    state = model.state_dict()
    for name, value in best_heads.items():
        state[name].copy_(value.to(state[name]))
    verify_frozen_state(model, before)
    report['changed_skill_tensors'] = sorted(k for k, v in best_heads.items() if tensor_digest(v) != initial_heads[k])
    report['multi_source_positive_rows'] = int(((data['train'].target >= 0) & (data['train'].ability_mask.sum(-1) > 1)).sum())
    report['source_selection_learned_from_ambiguous_states'] = report['multi_source_positive_rows'] > 0
    report['validation'] = evaluate(model, data['validation'], batch_size=args.batch_size, device=args.device)
    model.event_initialization['needs_recalibration_or_finetune'] = False
    model.event_initialization['needs_evaluation'] = True
    model.event_initialization['ability_source_needs_supervision'] = not report['source_selection_learned_from_ambiguous_states']
    extra['event_ability_il_training'] = dict(schema='event-ability-head-il-training.v1',
        parent_sha256=parent, parameter_names=sorted(ABILITY_PARAMETER_NAMES),
        optimizer_type='AdamW', optimizer=best_optimizer, epoch=best_epoch,
        seed=args.seed, frozen_state_sha256=before, validation=report['validation'],
        multi_source_positive_rows=report['multi_source_positive_rows'],
        source_selection_supervised=report['source_selection_learned_from_ambiguous_states'])
    output = args.output/'skill-heads.pt'
    digest = save_event_checkpoint(output, model.cpu(), extra=extra)
    readback = load_event_checkpoint(output)
    verify_frozen_state(readback.model, before)
    if any(training_state_digest(readback.payload[key]) != digest for key, digest in preserved.items()):
        raise ValueError('pre-existing optimizer, controller, constraints or metadata changed')
    for name, value in model.state_dict().items():
        if tensor_digest(readback.model.state_dict()[name]) != tensor_digest(value):
            raise ValueError(f'checkpoint readback mismatch: {name}')
    if hashlib.sha256(args.source.read_bytes()).hexdigest() != parent:
        raise ValueError('parent file changed')
    report.update(status='il_complete_pending_battle_evaluation', checkpoint=str(output.resolve()),
                  checkpoint_sha256=digest, old_tensors_unchanged=True, parent_file_unchanged=True,
                  preserved_training_state_digests=preserved, ppo_optimizer_and_kl_unchanged=True)
    (args.output/'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ('status','checkpoint','checkpoint_sha256','old_tensors_unchanged')}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--patience', type=int, default=4)
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--learning-rate', type=float, default=.001)
    parser.add_argument('--minimum-positives', type=int, default=20)
    parser.add_argument('--seed', type=int, default=20260925)
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    if min(args.epochs, args.patience, args.batch_size, args.minimum_positives, args.threads) < 1:
        parser.error('counts must be positive')
    train(args)


if __name__ == '__main__':
    main()
