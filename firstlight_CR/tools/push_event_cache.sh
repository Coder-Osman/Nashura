#!/usr/bin/env bash
# Push finished event-cache shards from a capture worker to the trainer staging dir.
# Usage: push_event_cache.sh LOCAL_DIR user@host:STAGING_DIR SSH_KEY [INTERVAL]
set -u
SRC=${1:?local capture dir}
DEST=${2:?user@host:staging dir}
KEY=${3:?ssh key}
INTERVAL=${4:-30}
while true; do
  rsync -a --exclude=index.json --exclude=stats.json --exclude=failures.json \
    -e "ssh -i $KEY -o StrictHostKeyChecking=no -o BatchMode=yes" \
    "$SRC"/ "$DEST"/ >/dev/null 2>&1
  sleep "$INTERVAL"
done
