#!/usr/bin/env bash
# Promote complete staged shard pairs (data + summary) into the live cache dir.
# Usage: promote_event_cache.sh STAGING_DIR LIVE_DIR [MIN_AGE_SECONDS]
set -u
STAGE=${1:?staging dir}
LIVE=${2:?live dir}
AGE=${3:-30}
mkdir -p "$LIVE"
while true; do
  now=$(date +%s)
  for j in "$STAGE"/*-shard-*.json; do
    [ -e "$j" ] || continue
    d="${j%.json}.crev.zst"
    [ -f "$d" ] || continue
    m=$(stat -c %Y "$j")
    (( now - m >= AGE )) || continue
    mv -f "$d" "$LIVE"/ 2>/dev/null && mv -f "$j" "$LIVE"/ 2>/dev/null && echo "promoted $(basename "$j")"
  done
  sleep 15
done
