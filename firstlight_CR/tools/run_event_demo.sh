#!/bin/bash
# One-command event-policy self-play demo.
#
# The match is generated headless on the GPU host (both owners = one checkpoint)
# and rendered here, because only the local AVD can draw the real game.  Swap the
# checkpoint argument to compare training phases.
#
# usage: tools/run_event_demo.sh <remote-checkpoint> <label> [seed] [wait-ticks]
#   e.g. tools/run_event_demo.sh /home/gujiga/event-pretrain-run/event-pretrain.pt phaseA
#        tools/run_event_demo.sh /home/gujiga/event-pretrain-run-b/event-pretrain.pt phaseB 20260921
#
# env overrides: GEN_MAX_TICKS (default 5800), REC_LIMIT (200), MAX_TICK (5800),
#                DECK0 (golem-beatdown), DECK1 (pekka-bridge-spam), PORT (26789),
#                REMOTE/RDIR/RDATA/LOCAL_ROOT
set -euo pipefail

CKPT=${1:?remote checkpoint path}
LABEL=${2:?label, e.g. phaseA}
SEED=${3:-20260914}
WAIT=${4:-60}

REMOTE=${REMOTE:-gujiga@172.16.48.34}
RDIR=${RDIR:-/home/gujiga/firstlight_CR}
RDATA=${RDATA:-/home/gujiga/demo-$LABEL}
LOCAL_ROOT=${LOCAL_ROOT:-/Users/gujiga/Documents/ChatGPT/CR}
LDIR=$LOCAL_ROOT/demo-$LABEL
REC=/sdcard/demo-$LABEL.mp4
PORT=${PORT:-26789}

GEN_MAX_TICKS=${GEN_MAX_TICKS:-5800}
# Defaults now cover the whole match: the earlier "stop before finalization"
# rule came from renders with wrong command coordinates, and a 3-crown ending
# finalizes without killing the recorder.  Raise REC_LIMIT to 300 for a match
# that runs to the time limit.
REC_LIMIT=${REC_LIMIT:-200}
MAX_TICK=${MAX_TICK:-5800}
DECK0=${DECK0:-golem-beatdown}
DECK1=${DECK1:-pekka-bridge-spam}
ADB=${ADB:-/Users/gujiga/Library/Android/sdk/platform-tools/adb}

echo "== 1/4 远端 headless 自博弈: $DECK0(0) vs $DECK1(1), seed $SEED =="
ssh -o BatchMode=yes "$REMOTE" "cd $RDIR && rm -rf $RDATA && mkdir -p $RDATA && \
  setsid nohup .venv/bin/python tools/demo_event_selfplay.py \
    --checkpoint $CKPT --deck0 $DECK0 --deck1 $DECK1 --seed $SEED \
    --out-dir $RDATA --port $PORT --env-id 0 --device cpu \
    --max-ticks $GEN_MAX_TICKS --max-seconds 1500 --max-wait-ticks $WAIT \
    > $RDATA/gen.log 2>&1 < /dev/null & echo launched"

echo "== 2/4 等待生成完成 =="
for _ in $(seq 1 240); do
  if ssh -o BatchMode=yes "$REMOTE" "test -f $RDATA/stats.json" 2>/dev/null; then break; fi
  sleep 5
done
ssh -o BatchMode=yes "$REMOTE" "test -f $RDATA/stats.json" || { echo "生成超时"; exit 1; }

echo "== 3/4 拉回轨迹 =="
mkdir -p "$LDIR"
for f in trajectory.jsonl render-match.json stats.json; do
  scp -q -o BatchMode=yes "$REMOTE:$RDATA/$f" "$LDIR/"
done
python3 - "$LDIR/stats.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
r = s["result"]
print(json.dumps({
    "plays": s["scheduled_plays"], "tick": r["tick"], "winner": r["winner"],
    "crowns": r["crowns"], "ended": r["ended"], "decode": s["decode_stats"],
}, ensure_ascii=False))
PY

echo "== 4/4 本机渲染（录制上限 $REC_LIMIT s，避免终局崩溃丢文件）=="
python3 "$LOCAL_ROOT/firstlight_CR/tools/replay_driver.py" \
  --trajectory "$LDIR/trajectory.jsonl" \
  --match-json "$LDIR/render-match.json" \
  --record "$REC" --record-limit "$REC_LIMIT" --max-tick "$MAX_TICK"
"$ADB" pull "$REC" "$LDIR/demo-$LABEL.mp4" >/dev/null
ls -la "$LDIR/demo-$LABEL.mp4"
echo "完成：$LDIR/demo-$LABEL.mp4（轨迹 $LDIR/trajectory.jsonl，统计 $LDIR/stats.json）"
