"""Migrate a frozen v2 checkpoint and verify real-row policy equivalence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import torch
from native_runner.training.event_checkpoint import load_event_checkpoint, save_event_checkpoint
from native_runner.training.event_cache import load_event_rl_shard
from native_runner.training.event_rollout import lanes_from_shard
from native_runner.training.event_ppo import _encode_rows, _slice_action
from native_runner.training.event_residual_migration import migrate_residual_checkpoint
from tools.event_rl_update import restore_optimizer


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint', type=Path, required=True)
    p.add_argument('--cache-root', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--blocks', type=int, default=2)
    p.add_argument('--hidden', type=int, default=512)
    args = p.parse_args()
    torch.set_num_threads(1)
    torch.manual_seed(2026091803)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    loaded = load_event_checkpoint(args.checkpoint)
    source = loaded.model.eval()
    target, extra = migrate_residual_checkpoint(loaded, blocks=args.blocks, hidden=args.hidden)
    target.eval()
    temp = args.output_dir / 'migrated.pt'
    digest = save_event_checkpoint(temp, target, extra=extra)
    restored = load_event_checkpoint(temp)
    restored.model.eval()
    optimizer, progress = restore_optimizer(restored.model, restored.payload)
    assert progress['iterations'] == extra['event_ppo_training']['iterations']
    maxima = {k: 0. for k in ['policy_context', 'value', 'gate', 'card', 'cell', 'delay', 'log_prob']}
    rows = acts = 0
    for path in sorted(args.cache_root.glob('**/part-*.json'))[:12]:
        for lane in lanes_from_shard(load_event_rl_shard(path, verify=True)):
            indices = {0, lane.time_steps // 2, lane.time_steps - 1}
            for flag in [False, True]:
                matches = (lane.actions.act == flag).nonzero()
                if matches.numel():
                    indices.add(int(matches[0, 0]))
            for i in sorted(indices):
                with torch.inference_mode():
                    models = [source, restored.model]
                    contexts = [_encode_rows(m, [lane.observations[i]]) for m in models]
                    action = _slice_action(lane.actions, i, i + 1)
                    outputs = [m.heads(c, card_row=action.card_row.clamp_min(0), cell=action.cell.clamp_min(0))
                               for m, c in zip(models, contexts)]
                    scores = [m.evaluate_actions(c, action, gate_mask=lane.gate_mask[i:i+1],
                              card_mask=lane.card_mask[i:i+1], cell_masks=lane.cell_masks[i:i+1],
                              delay_mask=lane.delay_mask[i:i+1], temperatures=lane.temperatures).log_prob
                              for m, c in zip(models, contexts)]
                    values = [[c.policy_context, c.value, o.gate_logits, o.card_logits,
                               o.cell_logits, o.delay_logits, score]
                              for c, o, score in zip(contexts, outputs, scores)]
                    for key, a, b in zip(maxima, *values):
                        maxima[key] = max(maxima[key], float((a-b).abs().max()))
                        torch.testing.assert_close(a, b, atol=2e-5, rtol=2e-5)
                rows += 1
                acts += int(bool(action.act[0]))
    assert rows >= 50 and 0 < acts < rows
    assert all(torch.isfinite(p).all() for p in restored.model.parameters())
    result = {**extra['architecture_migration'], 'checkpoint_sha256': digest,
              'contract_sha256': restored.contract_sha256, 'tested_rows': rows, 'act_rows': acts,
              'wait_rows': rows-acts, 'max_absolute_differences': maxima, 'rtol': 2e-5, 'atol': 2e-5,
              'optimizer_loaded': True, 'passed': True}
    (args.output_dir / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    content_path = args.output_dir / (digest + '.pt')
    temp.rename(content_path)
    assert hashlib.sha256(content_path.read_bytes()).hexdigest() == digest
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
