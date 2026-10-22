#!/bin/bash
# Produce one rendered match per deck pairing from a single event checkpoint.
#
# Reads the checkpoint's contract fields (snapshot_only / model_scale) so the
# generator rebuilds the same architecture, runs the three pairings on three
# engine slots in parallel, then renders them one after another (the local AVD
# is single-instance and plays in real time).
#
# usage: tools/demo_match_set.sh <remote-checkpoint> <label> [seed-base]
# env:   CKPT_SRC=user@host:/path/event-pretrain.pt   copy the checkpoint if absent
#        REMOTE/RDIR/RDATA/LOCAL_ROOT/ADB overrides
set -uo pipefail

CKPT=${1:?remote checkpoint path}
LABEL=${2:?label}
SEED=${3:-20260926}

REMOTE=${REMOTE:-zhichun@172.16.48.37}
RDIR=${RDIR:-/data/firstlight/home/firstlight_CR}
RDATA=${RDATA:-/data/firstlight}
LOCAL_ROOT=${LOCAL_ROOT:-/Users/gujiga/Documents/ChatGPT/CR}
LDIR=$LOCAL_ROOT/demo-$LABEL
ADB=${ADB:-/Users/gujiga/Library/Android/sdk/platform-tools/adb}

PAIRS=(
  "golem-vs-golem:golem-beatdown:golem-beatdown:28489"
  "golem-vs-hog:golem-beatdown:hog-cycle:28589"
  "hog-vs-hog:hog-cycle:hog-cycle:28689"
)

echo "== 1/4 准备 checkpoint 与契约字段 =="
if ! ssh -o BatchMode=yes "$REMOTE" "test -f $CKPT"; then
  if [ -z "${CKPT_SRC:-}" ]; then
    echo "  $REMOTE 上没有 $CKPT，且未提供 CKPT_SRC"; exit 1
  fi
  STAGE=$RDATA/ckpt-$LABEL.pt
  tmp=/tmp/ckpt-$LABEL.pt
  echo "  经本机中转: $CKPT_SRC -> $REMOTE:$STAGE"
  rm -f "$tmp"
  scp -q -o BatchMode=yes "$CKPT_SRC" "$tmp" || { echo "  拉取失败"; exit 1; }
  scp -q -o BatchMode=yes "$tmp" "$REMOTE:$STAGE" || { echo "  推送失败"; exit 1; }
  local_md5=$(md5 -q "$tmp")
  remote_md5=$(ssh -o BatchMode=yes "$REMOTE" "md5sum $STAGE | cut -c1-32")
  if [ "$local_md5" != "$remote_md5" ]; then echo "  校验失败: $local_md5 vs $remote_md5"; exit 1; fi
  echo "  校验通过 md5=$local_md5"
  CKPT=$STAGE
fi

# The contract must be stated by the caller: the checkpoint records it, but
# reading it back needs torch, so the driver does not probe for it here.
GEN_ENV=${GEN_ENV:-}
echo "  契约环境变量: '${GEN_ENV:-（无 = 旧契约）}'"
[ -n "${CHECK_CKPT_MD5:-}" ] && ssh -o BatchMode=yes "$REMOTE" "md5sum $CKPT | cut -c1-32"

echo "== 2/4 三局并行生成（seed $SEED） =="
for pair in "${PAIRS[@]}"; do
  IFS=: read -r name d0 d1 port <<< "$pair"
  ssh -o BatchMode=yes "$REMOTE" "cd $RDIR && rm -rf $RDATA/demo-$LABEL-$name && mkdir -p $RDATA/demo-$LABEL-$name && \
    $GEN_ENV setsid nohup .venv/bin/python tools/demo_event_selfplay.py \
      --checkpoint $CKPT --deck0 $d0 --deck1 $d1 --seed $SEED \
      --out-dir $RDATA/demo-$LABEL-$name --port $port --env-id 0 --device cpu \
      --max-ticks 5800 --max-seconds 1200 --max-wait-ticks 60 \
      > $RDATA/demo-$LABEL-$name/gen.log 2>&1 < /dev/null & echo '  $name 启动: $d0 vs $d1 (port $port)'" || true
done

echo "== 3/4 等生成完成 =="
for _ in $(seq 1 120); do
  n=$(ssh -o BatchMode=yes "$REMOTE" "ls $RDATA/demo-$LABEL-*/stats.json 2>/dev/null | wc -l")
  [ "$n" = "3" ] && break
  sleep 10
done
mkdir -p "$LDIR"
for pair in "${PAIRS[@]}"; do
  IFS=: read -r name _ _ _ <<< "$pair"
  for f in trajectory.jsonl render-match.json stats.json; do
    scp -q -o BatchMode=yes "$REMOTE:$RDATA/demo-$LABEL-$name/$f" "$LDIR/$name-$f"
  done
  python3 - "$LDIR/$name-stats.json" "$name" <<'EOPY'
import json, sys
d = json.load(open(sys.argv[1])); r = d["result"]
print("  %s: 出牌 %d 终局 tick %d 胜者 %s 皇冠 %s 生成 %.1fs"
      % (sys.argv[2], d["scheduled_plays"], r["tick"], r["winner"], r["crowns"], d["wall_seconds"]))
EOPY
done

echo "== 4/4 本机依次渲染 =="
for pair in "${PAIRS[@]}"; do
  IFS=: read -r name _ _ _ <<< "$pair"
  seconds=$(python3 -c "import json;t=json.load(open('$LDIR/$name-stats.json'))['result']['tick'];print(int(t/20)+25)")
  rec=/sdcard/$LABEL-$name.mp4
  "$ADB" shell rm -f "$rec" >/dev/null 2>&1
  echo "  --- $name: 录制上限 ${seconds}s ---"
  python3 "$LOCAL_ROOT/firstlight_CR/tools/replay_driver.py" \
    --trajectory "$LDIR/$name-trajectory.jsonl" \
    --match-json "$LDIR/$name-render-match.json" \
    --record "$rec" --record-limit "$seconds" --max-tick 5800 2>&1 | tail -2
  "$ADB" pull "$rec" "$LDIR/$name.mp4" >/dev/null 2>&1 && ls -la "$LDIR/$name.mp4" | awk '{printf "    %s  %.1f MB\n", $9, $5/1048576}'
done
echo "完成: $LDIR"
