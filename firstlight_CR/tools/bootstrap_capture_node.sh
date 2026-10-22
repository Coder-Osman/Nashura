#!/usr/bin/env bash
# One-shot bootstrap for a fresh Clash Royale capture node.
#
# Brings up an x86_64 Linux box (bare metal or VM with nested KVM) to the same
# shape as the existing capture workers:
#   android SDK + NDK -> probe .so -> AVDs -> offline APK + probe hot-swap
#   -> emulators -> engine configuration -> HF dataset -> capture + watchdog
#
# Usage:
#   cp tools/capture_node.env.example capture_node.env
#   $EDITOR capture_node.env
#   bash tools/bootstrap_capture_node.sh capture_node.env            # all steps
#   bash tools/bootstrap_capture_node.sh capture_node.env build-probe # single step
#
# Steps: install-deps | build-probe | prepare-avds | install-app | configure | dataset | run | all
#
# The script is idempotent: each step skips work that is already done.
set -euo pipefail

CONF=${1:?usage: bootstrap_capture_node.sh CONF_FILE [step]}
STEP=${2:-all}
# shellcheck disable=SC1090
source "$CONF"

REPO_ROOT=${REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}
SDK_ROOT=${SDK_ROOT:-$HOME/android-sdk}
NDK_VERSION=${NDK_VERSION:-27.3.13750724}
SYSTEM_IMAGE=${SYSTEM_IMAGE:-"system-images;android-35;google_apis;x86_64"}
AVD_PREFIX=${AVD_PREFIX:-firstlight-x86}
AVD_COUNT=${AVD_COUNT:-4}
ENGINES_PER_AVD=${ENGINES_PER_AVD:-6}
HOST_BASE=${HOST_BASE:-30089}
DATASET_ROOT=${DATASET_ROOT:-$HOME/firstlight_data/IL_full}
OUTPUT_DIR=${OUTPUT_DIR:-$HOME/event-obs}
PARTITION_INDEX=${PARTITION_INDEX:-0}
PARTITION_COUNT=${PARTITION_COUNT:-1}
SOURCE_COUNT=${SOURCE_COUNT:-252238}
APK=${APK:-$REPO_ROOT/../render-apk/cr-ai-offline6.apk}
HF_BASE=${HF_BASE:-https://huggingface.co/datasets/VanguardX101/IL_Replay/resolve/main}
INSTALL_ROOT=${INSTALL_ROOT:-$HOME/capture-node}   # tools that are not part of the repo
PY=${PY:-$REPO_ROOT/.venv/bin/python}
ADB=$SDK_ROOT/platform-tools/adb
JAVA_HOME=${JAVA_HOME:-}

log()  { echo "[$(date -Is)] $*"; }
fail() { echo "[$(date -Is)] ERROR: $*" >&2; exit 1; }

avd_names() {
  local i
  for ((i = 0; i < AVD_COUNT; i++)); do
    if ((i == 0)); then echo "$AVD_PREFIX"; else echo "$AVD_PREFIX-$((i + 1))"; fi
  done
}
serial_for() { echo "emulator-$((5554 + 2 * $1))"; }
host_base_for() { echo $((HOST_BASE + 100 * $1)); }

step_install_deps() {
  log "step: install-deps"
  [ "$(uname -m)" = "x86_64" ] || fail "capture nodes must be x86_64 (emulator images are x86_64)"
  [ -e /dev/kvm ] || fail "missing /dev/kvm; pick a host with KVM (bare metal or nested virtualization)"
  grep -qw kvm <(id -Gn) || log "WARNING: user is not in the kvm group yet (sudo usermod -aG kvm $USER; then re-login)"
  mkdir -p "$SDK_ROOT/cmdline-tools" "$INSTALL_ROOT"
  if [ ! -x "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]; then
    log "downloading Android command line tools"
    local zip=$INSTALL_ROOT/cmdline-tools.zip
    curl -fsSL -o "$zip" https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
    rm -rf "$SDK_ROOT/cmdline-tools/latest"
    unzip -q -o "$zip" -d "$INSTALL_ROOT"
    mv "$INSTALL_ROOT/cmdline-tools" "$SDK_ROOT/cmdline-tools/latest"
  fi
  local sdkmanager=$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager
  yes | "$sdkmanager" --sdk_root="$SDK_ROOT" --licenses >/dev/null 2>&1 || true
  "$sdkmanager" --sdk_root="$SDK_ROOT" "platform-tools" "emulator" "$SYSTEM_IMAGE" "ndk;$NDK_VERSION"
  log "deps ready under $SDK_ROOT"
}

step_build_probe() {
  log "step: build-probe"
  local cxx=$SDK_ROOT/ndk/$NDK_VERSION/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android24-clang++
  [ -x "$cxx" ] || fail "missing NDK compiler: $cxx (run install-deps first)"
  local probe_dir=$REPO_ROOT/native_runner/probe
  mkdir -p "$probe_dir/out"
  "$cxx" -std=c++17 -O2 -fPIC -fvisibility=hidden -Wall -Wextra -shared \
    -Wl,-z,max-page-size=16384 -o "$probe_dir/out/libcrprobe.so" \
    "$probe_dir/cr_replay_probe.cpp" "$probe_dir/native_touch_interceptor.cpp" \
    "$probe_dir/native_call_arm64.S" "$probe_dir/remaining_runtime_arm64.S" -llog -ldl -lz
  log "probe built: $(sha256sum "$probe_dir/out/libcrprobe.so" | cut -c1-16)"
}

step_prepare_avds() {
  log "step: prepare-avds"
  mkdir -p "$HOME/.android/avd"
  if [ -n "${AVD_IMAGE_TARBALL:-}" ]; then
    log "migrating AVD images from $AVD_IMAGE_TARBALL (recommended: keeps downloaded game resources)"
    tar xzf "$AVD_IMAGE_TARBALL" -C "$HOME/.android/avd" --strip-components=1
    log "AVDs migrated; skipping creation"
    return
  fi
  log "WARNING: creating fresh AVDs. The offline APK expects an existing resource update;"
  log "         if content does not become ready, migrate ~/.android/avd from a working node instead."
  local avdmanager=$SDK_ROOT/cmdline-tools/latest/bin/avdmanager
  local name
  while read -r name; do
    if [ -d "$HOME/.android/avd/$name.avd" ]; then
      log "AVD $name exists; skipping"
      continue
    fi
    echo no | "$avdmanager" create avd -n "$name" -k "$SYSTEM_IMAGE" -d pixel_5 --force
    cat >>"$HOME/.android/avd/$name.avd/config.ini" <<EOF
hw.ramSize=4096
vm.heapSize=512
hw.cpu.ncore=4
disk.dataPartition.size=6G
hw.gpu.enabled=no
hw.gpu.mode=swiftshader_indirect
EOF
    log "created AVD $name"
  done < <(avd_names)
}

adb_for() { "$ADB" -s "$1" "${@:2}"; }

step_install_app() {
  log "step: install-app"
  [ -f "$APK" ] || fail "APK not found: $APK"
  local index=0 serial
  while read -r serial; do
    if ! "$ADB" devices | grep -q "$serial[[:space:]]*device"; then
      log "starting emulator for $serial"
      start_emulator "$index"
      wait_for_device "$serial"
    fi
    log "installing APK on $serial (non-streaming so lib/ stays writable)"
    adb_for "$serial" install --no-streaming -r -d -g "$APK" >/dev/null
    log "hot-swapping capture probe on $serial"
    "$ADB" -s "$serial" root >/dev/null 2>&1 || true
    local target probe_sha
    target=$("$ADB" -s "$serial" shell pm path nullsroyale.rel.free | tr -d '\r' | sed 's#package:##')
    target="$(dirname "$target")/lib/arm64/libcrprobe.so"
    probe_sha=$(sha256sum "$REPO_ROOT/native_runner/probe/out/libcrprobe.so" | cut -d' ' -f1)
    "$ADB" -s "$serial" push "$REPO_ROOT/native_runner/probe/out/libcrprobe.so" /data/local/tmp/crprobe.so >/dev/null
    "$ADB" -s "$serial" shell "dd if=/data/local/tmp/crprobe.so of=$target conv=fsync" >/dev/null 2>&1
    local got
    got=$("$ADB" -s "$serial" shell sha256sum "$target" | awk '{print $1}')
    [ "$got" = "$probe_sha" ] || fail "probe hash mismatch on $serial"
    log "$serial probe installed ($(echo "$probe_sha" | cut -c1-12))"
    index=$((index + 1))
  done < <(serials)
}

start_emulator() {
  local index=$1 name
  name=$(avd_names | sed -n "$((index + 1))p")
  log "launching emulator $name"
  ANDROID_SDK_ROOT="$SDK_ROOT" ANDROID_AVD_HOME="$HOME/.android/avd" \
    LD_LIBRARY_PATH="$SDK_ROOT/emulator/lib64:$SDK_ROOT/emulator/lib64/gles_swiftshader" \
    nohup "$SDK_ROOT/emulator/qemu/linux-x86_64/qemu-system-x86_64-headless" -avd "$name" \
    -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect \
    >"/tmp/emu-$name.log" 2>&1 </dev/null &
  sleep 5
}

wait_for_device() {
  local serial=$1 waited=0
  until "$ADB" devices | grep -q "$serial[[:space:]]*device"; do
    sleep 10
    waited=$((waited + 10))
    ((waited < 300)) || fail "$serial did not come online"
  done
  sleep 30   # let the guest finish booting before arming engines
}

serials() { local i; for ((i = 0; i < AVD_COUNT; i++)); do serial_for "$i"; done; }

step_configure() {
  log "step: configure"
  local i=0 serial bases=()
  while read -r serial; do
    [ -x "$PY" ] || fail "python venv missing: $PY (create it and install torch/numpy/orjson/safetensors/zstandard/pyarrow)"
    for id in $(seq 0 $((ENGINES_PER_AVD - 1))); do
      "$ADB" -s "$serial" shell am start --user 0 -f 0x18000000 \
        -n nullsroyale.rel.free/com.supercell.clashroyale.Engine${id}App >/dev/null 2>&1 || true
      sleep 1
    done
    bases+=("$(host_base_for "$i")")
    i=$((i + 1))
  done < <(serials)
  sleep 40
  CR_TRAINING_ADB="$ADB" "$PY" "$REPO_ROOT/native_runner/training/v4/configure_emulator_engine_guests.py" \
    --serials "$(serials | paste -sd, -)" --host-bases "$(printf '%s\n' "${bases[@]}" | paste -sd, -)" \
    --engines "$ENGINES_PER_AVD" --output /tmp/capture-node-configure.json
  local ready=0 port
  for ((i = 0; i < AVD_COUNT; i++)); do
    for ((e = 0; e < ENGINES_PER_AVD; e++)); do
      port=$(($(host_base_for "$i") + e))
      (echo status; sleep 0.1) | timeout 3 nc -w 2 127.0.0.1 "$port" 2>/dev/null | grep -q '"coldReady":true' && ready=$((ready + 1))
    done
  done
  log "engines ready: $ready / $((AVD_COUNT * ENGINES_PER_AVD))"
  ((ready > 0)) || fail "no engine became ready (see /tmp/capture-node-configure.json)"
}

step_dataset() {
  log "step: dataset"
  mkdir -p "$DATASET_ROOT/replays" "$DATASET_ROOT/actions"
  local i n
  for i in $(seq 0 51); do
    n=$(printf "%03d" "$i")
    for kind in replays actions; do
      local f=$DATASET_ROOT/$kind/part-000$n.parquet
      if [ ! -s "$f" ] || [ "$(stat -c%s "$f")" -lt 1000000 ]; then
        curl -fL --max-time 600 --retry 5 -o "$f" "$HF_BASE/$kind/part-000$n.parquet" || fail "download failed: $kind/$n"
      fi
    done
  done
  log "dataset ready: $(ls "$DATASET_ROOT"/replays/*.parquet | wc -l) replay shards"
}

step_run() {
  log "step: run"
  local ports="" i e
  for ((i = 0; i < AVD_COUNT; i++)); do
    for ((e = 0; e < ENGINES_PER_AVD; e++)); do
      ports+="$(($(host_base_for "$i") + e)),"
    done
  done
  ports=${ports%,}
  local conf=$INSTALL_ROOT/capture.conf
  cat >"$conf" <<EOF
SDK=$SDK_ROOT
AVD_HOME=$HOME/.android/avd
ADB=$ADB
PYTHON=$PY
REPO=$REPO_ROOT
SERIALS="$(serials | paste -sd' ' -)"
AVDS="$(avd_names | paste -sd' ' -)"
HOST_BASES="$(for ((i = 0; i < AVD_COUNT; i++)); do printf '%s,' "$(host_base_for "$i")"; done | sed 's/,$//')"
PORTS="$ports"
DATASET=$DATASET_ROOT
OUTDIR=$OUTPUT_DIR
SOURCE_COUNT=$SOURCE_COUNT
PARTITION_INDEX=$PARTITION_INDEX
PARTITION_COUNT=$PARTITION_COUNT
EOF
  cat >"$INSTALL_ROOT/watchdog_supervisor.sh" <<EOF
#!/usr/bin/env bash
while true; do
  bash $REPO_ROOT/tools/event_capture_watchdog.sh $conf >> /tmp/capture-watchdog.log 2>&1
  echo "\$(date -Is) watchdog exited; respawning" >> /tmp/capture-watchdog.log
  sleep 5
done
EOF
  chmod +x "$INSTALL_ROOT/watchdog_supervisor.sh"
  nohup bash "$INSTALL_ROOT/watchdog_supervisor.sh" >/dev/null 2>&1 &
  if [ -n "${TRAINER_TARGET:-}" ] && [ -n "${TRAINER_KEY:-}" ]; then
    nohup bash "$REPO_ROOT/tools/push_event_cache.sh" "$OUTPUT_DIR" "$TRAINER_TARGET" "$TRAINER_KEY" 30 >/dev/null 2>&1 &
    log "push loop started -> $TRAINER_TARGET"
  fi
  log "capture started (config: $conf); watch: tail -f $OUTPUT_DIR.log /tmp/capture-watchdog.log"
}

case "$STEP" in
  install-deps) step_install_deps ;;
  build-probe)  step_build_probe ;;
  prepare-avds) step_prepare_avds ;;
  install-app)  step_install_app ;;
  configure)    step_configure ;;
  dataset)      step_dataset ;;
  run)          step_run ;;
  all)
    step_install_deps
    step_build_probe
    step_prepare_avds
    step_install_app
    step_configure
    step_dataset
    step_run
    ;;
  *) fail "unknown step: $STEP" ;;
esac
log "done: $STEP"
