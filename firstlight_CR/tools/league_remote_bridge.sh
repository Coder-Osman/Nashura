#!/usr/bin/env bash
# Bridge a worker-only league producer into the authoritative learner.
# Runs on 172.16.48.34. Pulls complete atomic rollout files from .37,
# removes them at source only after a successful transfer, publishes them into
# the local trainer inbox, and pushes the latest policy checkpoints back.
set -u

KEY="${FIRSTLIGHT_REMOTE_KEY:-$HOME/.ssh/firstlight-worker-link}"
REMOTE="${FIRSTLIGHT_REMOTE_HOST:-zhichun@172.16.48.37}"
REMOTE_ROOT="${FIRSTLIGHT_REMOTE_ROOT:-/data/firstlight/home/firstlight_CR/runs/league-producer}"
LOCAL_ROOT="${FIRSTLIGHT_LOCAL_ROOT:-$HOME/firstlight_CR/runs/league-1x}"
INTERVAL="${FIRSTLIGHT_BRIDGE_INTERVAL:-15}"
STOP="$LOCAL_ROOT/STOP"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
RSYNC_SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
POLICIES=(hog-cycle golem-beatdown pekka-bridge-spam)

mkdir -p "$LOCAL_ROOT/remote-staging"
while [ ! -f "$STOP" ]; do
  transferred=0
  for policy in "${POLICIES[@]}"; do
    staging="$LOCAL_ROOT/remote-staging/$policy"
    inbox="$LOCAL_ROOT/rollout/$policy"
    mkdir -p "$staging" "$inbox"
    rsync -a --remove-source-files --include='m-*.pt' --exclude='*' \
      -e "$RSYNC_SSH" "$REMOTE:$REMOTE_ROOT/rollout/$policy/" "$staging/" >/dev/null 2>&1 || true
    for path in "$staging"/m-*.pt; do
      [ -e "$path" ] || continue
      target="$inbox/$(basename "$path")"
      if [ -e "$target" ]; then
        rm -f "$path"
      else
        mv "$path" "$target"
        transferred=$((transferred + 1))
      fi
    done
  done

  # rsync's default temporary-file + rename publication keeps new workers from
  # reading partially written checkpoints. Base student remains as fallback.
  if compgen -G "$LOCAL_ROOT/policy/*.pt" >/dev/null; then
    "${SSH[@]}" "$REMOTE" "mkdir -p '$REMOTE_ROOT/policy'" >/dev/null 2>&1 || true
    rsync -a -e "$RSYNC_SSH" "$LOCAL_ROOT/policy/" "$REMOTE:$REMOTE_ROOT/policy/" >/dev/null 2>&1 || true
  fi
  printf '{"wall":%s,"last_transferred":%s}\n' "$(date +%s)" "$transferred" \
    > "$LOCAL_ROOT/remote-bridge-status.json"
  sleep "$INTERVAL"
done
