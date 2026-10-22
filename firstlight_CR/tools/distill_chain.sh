#!/usr/bin/env bash
# Distillation chain: waits for the resident self-play run to finish, collects
# expert rollouts into the IL cache format, then behavior-clones the widened
# student from the migrated legacy checkpoint.
set -u
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${CR_AI_PYTHON:-$repo_root/.venv/bin/python}"
expert_ckpt="${1:-checkpoints/General/checkpoint-step-00000460.pt}"
output="${2:-runs/distill}"
log_dir="$output"
mkdir -p "$log_dir"

# Wait for the running master (v2 5h self-play) to exit.
while pgrep -f "selfplay_resident.py --hours" >/dev/null 2>&1; do
  sleep 60
done
sleep 15
echo "[$(date '+%H:%M:%S')] self-play finished; starting distillation collection" | tee -a "$log_dir/chain.log"

if ! "$python_bin" tools/distill_collect.py \
    --checkpoint "$expert_ckpt" \
    --matches-per-lane 1 \
    --output "$output" >> "$log_dir/collect.log" 2>&1; then
  echo "[$(date '+%H:%M:%S')] DISTILL COLLECT FAILED" | tee -a "$log_dir/chain.log"
  exit 1
fi

if [ ! -f "$output/cache/index.json" ] || [ ! -f "$output/cache/train-tags.txt" ]; then
  echo "[$(date '+%H:%M:%S')] cache index/tags missing after collection" | tee -a "$log_dir/chain.log"
  exit 1
fi
echo "[$(date '+%H:%M:%S')] collection done; starting behavior cloning" | tee -a "$log_dir/chain.log"

IL_WORLD_SIZE="${IL_WORLD_SIZE:-8}" \
CR_AI_PYTHON="$python_bin" \
CR_IL_SHARDS_PER_RANK="${CR_IL_SHARDS_PER_RANK:-1}" \
CR_IL_TORCH_THREADS="${CR_IL_TORCH_THREADS:-1}" \
bash native_runner/training/v4/launch_stateful_il.sh \
  --index "$output/cache/index.json" \
  --train-tags "$output/cache/train-tags.txt" \
  --output-dir "$output/train" \
  --resume "$expert_ckpt" \
  --epochs 1 --gamma 0.99 >> "$log_dir/train.log" 2>&1

status=$?
echo "[$(date '+%H:%M:%S')] distillation training exit=$status" | tee -a "$log_dir/chain.log"
exit $status
