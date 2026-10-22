#!/usr/bin/env bash
# Engine cluster control: restart one engine, check port status.
# Usage:
#   enginectl.sh status SERIAL BASE          (default BASE 26789)
#   enginectl.sh restart SERIAL ENGINE_ID
set -u
ADB="${CR_TRAINING_ADB:-adb}"
ACTION="${1:?action required}"
SERIAL="${2:?serial required}"
ARG3="${3:-26789}"

case "$ACTION" in
  restart)
    ENGINE_ID="$ARG3"
    "$ADB" -s "$SERIAL" shell am force-stop nullsroyale.rel.free
    sleep 1
    "$ADB" -s "$SERIAL" shell am start --user 0 -f 0x18000000 \
      -n nullsroyale.rel.free/com.supercell.clashroyale.Engine${ENGINE_ID}App
    ;;
  status)
    BASE="$ARG3"
    for offset in 0 1 2 3 4 5; do
      port=$((BASE + offset))
      out=$(python3 - "$port" <<'PYEOF'
import socket, sys
port = int(sys.argv[1])
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(b"multi-status\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    print(f"port {port}: {buf.decode().strip()[:120]}")
except Exception as error:
    print(f"port {port}: DOWN ({error})")
PYEOF
)
      echo "$out"
    done
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 2
    ;;
esac
