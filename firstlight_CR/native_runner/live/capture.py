"""Read bounded original-client snapshots, without loading the offline probe.

Prepare a matching Frida server on the dedicated AVD, then:
  python -m native_runner.live.capture --serial emulator-5570 --seconds 300 \
    --output artifacts/live-capture.jsonl --libg artifacts/live-null-20260920/libg.so
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ADB = Path.home() / "Library/Android/sdk/platform-tools/adb"
ANCHORS = {"manager": 0xBB6508, "battle": 0xBB93E0, "tick": 0x1062BC0,
           "account_context": 0xC85C28, "local_account": 0xC8A954, "selection": 0xE2D1BC,
           "consume_card": 0xF38A68, "attack_start": 0xF23110, "attack_release": 0xF5F100,
           "target_reset": 0xF5C894, "buff_added": 0xF5A2D4, "attack_scale": 0xF5B4AC,
           "movement_scale": 0xF5B3C8, "effective_speed": 0xF1D004, "movement_delta": 0xF68808,
           "own_command_submit": 0xC893D0, "own_command_outbound": 0xC89778,
           "own_command_validate": 0xD30A00,
           "sync_fault_report": 0x106482C, "sync_clock_fault_call": 0x1063138,
           "sync_step_checksum_fault": 0x1064520, "sync_update_checksum_fault": 0x1064D54,
           "sync_update_fault_call": 0x1064F9C, "sync_explicit_fault": 0x10646C8,
           "card_command_kind": 0xD31204, "hp_component_kind": 0xF62F80}


def verified_config(libg: Path, interval_ms: int) -> dict:
    supported = json.loads((ROOT / "native_runner/supported_engine.json").read_text())
    binary = libg.read_bytes()
    if hashlib.sha256(binary).hexdigest() != supported["libg_sha256"]:
        raise ValueError("unsupported libg build")
    if not 50 <= interval_ms <= 1000:
        raise ValueError("interval must be between 50 and 1000 ms")
    # These anchors are in the pinned ELF's first PT_LOAD: vaddr == file offset.
    return {"interval_ms": interval_ms, "anchors": [
        {"name": name, "offset": offset, "hex": binary[offset:offset + 16].hex()}
        for name, offset in ANCHORS.items()
    ]}


def game_pid(adb) -> int:
    pids = adb("shell", "pidof", "nullsroyale.rel.free").split()
    if len(pids) != 1:
        raise RuntimeError("expected exactly one running game process before Frida attach")
    return int(pids[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--port", type=int, default=27742)
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--libg", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0 or not 1024 <= args.port <= 65535:
        parser.error("positive duration and an unprivileged local port are required")
    config = verified_config(args.libg, args.interval_ms)
    supported = json.loads((ROOT / "native_runner/supported_engine.json").read_text())

    def adb(*values: str) -> str:
        return subprocess.check_output([str(args.adb), "-s", args.serial, *values], text=True, timeout=20).strip()

    fingerprint = adb("shell", "sha256sum", "/data/data/nullsroyale.rel.free/update/fingerprint.json").split()[0]
    if fingerprint != supported["runtime_fingerprint_sha256"]:
        raise ValueError("runtime resources do not match the training contract")
    pid = game_pid(adb)
    adb("forward", f"tcp:{args.port}", "tcp:27042")
    if game_pid(adb) != pid:
        raise RuntimeError("game process changed before Frida attach; retry after it stabilizes")
    import frida  # Optional dependency, isolated in .venv-live.
    device = frida.get_device_manager().add_remote_device(f"127.0.0.1:{args.port}")
    if game_pid(adb) != pid:
        raise RuntimeError("game process changed while preparing Frida attach; retry after it stabilizes")
    session = device.attach(pid)
    stopped = threading.Event()
    detach_reason = []
    def detached(reason, *_):
        detach_reason.append(str(reason))
        stopped.set()
    session.on("detached", detached)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    lock = threading.Lock()
    try:
        with args.output.open("x") as output:
            def write(payload: dict) -> None:
                payload = {**payload, "received_monotonic_ns": time.monotonic_ns()}
                with lock:
                    output.write(json.dumps(payload, allow_nan=False) + "\n")
                    output.flush()
                    kind = payload.get("event", "unknown")
                    counts[kind] = counts.get(kind, 0) + 1

            def message(msg: dict, _data: bytes | None) -> None:
                if msg["type"] == "send":
                    write(msg["payload"])
                else:
                    write({"event": "script_error", "detail": msg})
                    stopped.set()

            source = (Path(__file__).with_name("timing-reader.js").read_text() + "\n"
                      + Path(__file__).with_name("phase-reader.js").read_text() + "\n"
                      + Path(__file__).with_name("reader.js").read_text())
            script = session.create_script(source)
            script.on("message", message)
            script.load()
            ready = script.exports_sync.start(config)
            write({**ready, "serial": args.serial, "pid": pid, "capture_id": uuid.uuid4().hex,
                   "libg_sha256": supported["libg_sha256"], "fingerprint_sha256": fingerprint,
                   "timing_schema": "cr-live-timing.v1"})
            print(json.dumps(ready), flush=True)
            try:
                stopped.wait(args.seconds)
            except KeyboardInterrupt:
                pass
            try:
                script.unload()
            except frida.InvalidOperationError:
                pass
            write({"event": "stopped", "detach_reason": detach_reason})
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except frida.InvalidOperationError:
            pass
    print(json.dumps({"output": str(args.output), "events": counts}), flush=True)


if __name__ == "__main__":
    main()
