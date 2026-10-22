#!/usr/bin/env bash
# Keep one capture worker alive for unattended multi-hour runs.
#
# Layered recovery, cheapest first:
#   1. per-guest health probe; a wedged guest gets `adb reconnect` instead of a
#      whole-server restart;
#   2. only when every guest is unresponsive is the adb server restarted;
#   3. missing emulator processes are relaunched;
#   4. engine activities are re-armed and, if too few ports answer, the engine
#      configurator is re-run under a hard timeout;
#   5. the capture process is relaunched with --resume, which skips finished
#      shards so restarts are lossless.
#
# Usage: event_capture_watchdog.sh CONF_FILE
set -u

CONF=${1:?watchdog config file is required}
# shellcheck disable=SC1090
source "$CONF"
LOG=${WATCHDOG_LOG:-/tmp/capture-watchdog.log}
INTERVAL=${WATCHDOG_INTERVAL:-60}
PACKAGE=${PACKAGE:-nullsroyale.rel.free}

log() { echo "$(date -Is) $*" >>"$LOG"; }

guest_alive() { timeout 12 "$ADB" -s "$1" shell true >/dev/null 2>&1; }

arm_one() {
  local s=$1
  for id in 0 1 2 3 4 5; do
    timeout 12 "$ADB" -s "$s" shell am start --user 0 -f 0x18000000 \
      -n "$PACKAGE/com.supercell.clashroyale.Engine${id}App" >/dev/null 2>&1
    sleep 1
  done
}

count_ready() {
  local ready=0
  for p in $(echo "$PORTS" | tr ',' ' '); do
    if (echo status; sleep 0.1) | timeout 3 nc -w 2 127.0.0.1 "$p" 2>/dev/null | grep -q '"coldReady":true'; then
      ready=$((ready + 1))
    fi
  done
  echo $ready
}

launch_capture() {
  nohup "$PYTHON" "$REPO/tools/build_event_observations.py" "$DATASET" "$OUTDIR" \
    --ports "$PORTS" --source-count "$SOURCE_COUNT" --partition-index "$PARTITION_INDEX" \
    --partition-count "$PARTITION_COUNT" --replays-per-shard 1 --step-until-event \
    >"$OUTDIR.log" 2>&1 </dev/null &
  log "capture relaunched pid=$!"
}

ensure_adb() {
  local serial_count bad=0
  serial_count=$(echo "$SERIALS" | wc -w)
  if ! timeout 20 "$ADB" devices >/tmp/wd-devices.txt 2>/dev/null; then
    log "adb server unresponsive; restarting"
    timeout 20 "$ADB" kill-server >/dev/null 2>&1
    sleep 2
    timeout 30 "$ADB" start-server >/dev/null 2>&1
  fi
  for s in $SERIALS; do
    if ! guest_alive "$s"; then
      bad=$((bad + 1))
      log "$s unresponsive; reconnecting guest"
      timeout 15 "$ADB" -s "$s" reconnect >/dev/null 2>&1 || true
      timeout 15 "$ADB" reconnect >/dev/null 2>&1 || true
    fi
  done
  if [ "$bad" -ge "$serial_count" ]; then
    log "every guest unresponsive; restarting adb server"
    timeout 20 "$ADB" kill-server >/dev/null 2>&1
    sleep 2
    timeout 30 "$ADB" start-server >/dev/null 2>&1
    sleep 5
  fi
  for s in $SERIALS; do
    if guest_alive "$s" && ! timeout 12 "$ADB" -s "$s" shell pidof "${PACKAGE}:engine0" >/dev/null 2>&1; then
      log "$s engine0 missing; arming activities"
      arm_one "$s"
    fi
  done
}

ensure_emulators() {
  for a in $AVDS; do
    if ! pgrep -f "avd $a( |$)" >/dev/null; then
      log "emulator $a missing; restarting"
      ANDROID_SDK_ROOT="$SDK" ANDROID_AVD_HOME="$AVD_HOME" \
        LD_LIBRARY_PATH="$SDK/emulator/lib64:$SDK/emulator/lib64/gles_swiftshader" \
        nohup "$SDK/emulator/qemu/linux-x86_64/qemu-system-x86_64-headless" -avd "$a" \
        -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect \
        >"/tmp/emu-$a.log" 2>&1 </dev/null &
      sleep 25
    fi
  done
}

ensure_engines() {
  local total ready waited
  total=$(echo "$PORTS" | tr ',' ' ' | wc -w)
  ready=$(count_ready)
  if [ "$ready" -lt $((total / 2)) ]; then
    if pgrep -f "build_event_observations" >/dev/null; then
      # Configure force-stops the engine activities.  Never do that while a
      # capture session is live: interrupting an in-flight native match is what
      # produced the null-pointer crashes, and the session recovers on its own.
      log "engines low ($ready/$total) but capture is active; deferring reconfigure"
      return
    fi
    log "engines down ($ready/$total); re-arming and configuring"
    for s in $SERIALS; do arm_one "$s"; done
    sleep 45
    CR_TRAINING_ADB="$ADB" timeout 480 "$PYTHON" \
      "$REPO/native_runner/training/v4/configure_emulator_engine_guests.py" \
      --serials "$(echo "$SERIALS" | tr ' ' ',')" --host-bases "$HOST_BASES" --engines 6 \
      --output /tmp/cfg-watchdog.json >>"$LOG" 2>&1 || log "configure timed out or failed"
    waited=0
    while [ "$(count_ready)" -lt $((total / 2)) ] && [ "$waited" -lt 180 ]; do
      sleep 15
      waited=$((waited + 15))
    done
    log "post-configure ready=$(count_ready)/$total after ${waited}s"
  fi
}

ensure_capture() {
  if ! pgrep -f "build_event_observations" >/dev/null; then
    if [ "$(count_ready)" -ge 1 ]; then
      log "capture process missing; relaunching"
      launch_capture
      sleep 20
    fi
  fi
}

ensure_guests() {
  local index=0 serial avd state count
  for serial in $SERIALS; do
    index=$((index + 1))
    avd=$(echo "$AVDS" | cut -d' ' -f"$index")
    state=$(timeout 12 "$ADB" -s "$serial" get-state 2>/dev/null || echo offline)
    if [ "$state" != "device" ]; then
      count=$(cat "/tmp/wd-offline-$serial" 2>/dev/null || echo 0)
      count=$((count + 1))
      echo "$count" >"/tmp/wd-offline-$serial"
      log "$serial state=$state (strike $count)"
      timeout 10 "$ADB" -s "$serial" reconnect >/dev/null 2>&1 || true
      if [ "$count" -ge 3 ] && [ -n "$avd" ]; then
        log "$serial still offline; restarting emulator $avd"
        pkill -f "avd $avd( |$)" >/dev/null 2>&1 || true
        sleep 6
        ANDROID_SDK_ROOT="$SDK" ANDROID_AVD_HOME="$AVD_HOME" \
          LD_LIBRARY_PATH="$SDK/emulator/lib64:$SDK/emulator/lib64/gles_swiftshader" \
          nohup "$SDK/emulator/qemu/linux-x86_64/qemu-system-x86_64-headless" -avd "$avd" \
          -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect \
          >"/tmp/emu-$avd.log" 2>&1 </dev/null &
        rm -f "/tmp/wd-offline-$serial"
        sleep 60
      fi
    else
      rm -f "/tmp/wd-offline-$serial"
    fi
  done
}

offline_count() {
  local count=0 state
  for serial in $SERIALS; do
    state=$(timeout 12 "$ADB" -s "$serial" get-state 2>/dev/null || echo offline)
    [ "$state" = "device" ] || count=$((count + 1))
  done
  echo $count
}

log "watchdog started"
while true; do
  ensure_adb
  ensure_guests
  if [ "$(offline_count)" -eq 0 ]; then
    ensure_engines
    ensure_capture
  else
    log "guests offline; deferring engine and capture recovery"
  fi
  sleep "$INTERVAL"
done
