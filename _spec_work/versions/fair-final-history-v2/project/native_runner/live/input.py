"""Persistent Android InputManager transport, owned by one policy process."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import threading
import time
import uuid

from .snapshot import LiveStateError

ROOT = Path(__file__).resolve().parents[2]


def build_input_helper() -> Path:
    source = Path(__file__).with_name("CRLiveInput.java")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    folder = ROOT / "artifacts/live-input" / digest
    jar = folder / "input.jar"
    if jar.is_file():
        return jar
    sdk = Path.home() / "Library/Android/sdk"
    java = Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin")
    platforms = sorted((sdk / "platforms").glob("android-*/android.jar"))
    compilers = sorted((sdk / "build-tools").glob("*/lib/d8.jar"))
    if not platforms or not compilers or not (java / "javac").is_file():
        raise LiveStateError("persistent input requires the local Android SDK and Android Studio JDK")
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(java / "javac"), "-source", "8", "-target", "8", "-bootclasspath",
                    str(platforms[-1]), "-d", str(folder), str(source)], check=True, capture_output=True, timeout=45)
    subprocess.run([str(java / "java"), "-cp", str(compilers[-1]), "com.android.tools.r8.D8",
                    "--lib", str(platforms[-1]), "--min-api", "26", "--output", str(jar),
                    str(folder / "CRLiveInput.class")], check=True, capture_output=True, timeout=45)
    return jar


class PersistentInput:
    def __init__(self, adb, serial, *, log_dir: Path):
        jar = build_input_helper()
        self.identity = uuid.uuid4().hex
        self.sequence = 0
        self.buffer = b""
        self.lock = threading.Lock()
        self.last_timing = None
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        remote = "/data/local/tmp/cr-live-input-" + digest[:20] + ".jar"
        command = [str(adb), "-s", serial]
        subprocess.run([*command, "push", str(jar), remote], check=True, capture_output=True, timeout=20)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log = (log_dir / ("input-" + self.identity + ".log")).open("wb")
        self.process = subprocess.Popen(
            [*command, "shell", "-T", "CLASSPATH=" + shlex.quote(remote),
             "app_process", "/", "CRLiveInput", self.identity],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, bufsize=0,
            start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            if self._line(10) != "READY " + self.identity:
                raise LiveStateError("input helper identity mismatch")
            # Bracket one Android monotonic sample by host send/receive. Keep
            # the shortest of five round trips; never equate the two clocks.
            samples = [self._clock_sample() for _ in range(5)]
            self.clock_sync = min(samples, key=lambda sample: sample["round_trip_ns"])
        except BaseException:
            self.close()
            raise

    def _clock_sample(self):
        self.sequence += 1
        sent = time.monotonic_ns()
        self.process.stdin.write(f"SYNC {self.sequence}\n".encode("ascii"))
        answer = self._line(5).split()
        received = time.monotonic_ns()
        if len(answer) != 3 or answer[:2] != ["CLOCK", str(self.sequence)] or not answer[2].isdigit():
            raise LiveStateError("input helper clock receipt mismatch")
        device = int(answer[2])
        return dict(host_sent_ns=sent, host_received_ns=received, device_monotonic_ns=device,
                    host_minus_device_min_ns=sent-device, host_minus_device_max_ns=received-device,
                    round_trip_ns=received-sent)

    def _line(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise LiveStateError("input helper acknowledgement timed out")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise LiveStateError("input helper disconnected")
            self.buffer += chunk
            if len(self.buffer) > 4096:
                raise LiveStateError("invalid input helper response")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode("ascii").strip()

    def tap(self, x, y):
        if type(x) is not int or type(y) is not int or not (0 <= x < 1080 and 0 <= y < 1920):
            raise LiveStateError("touch outside calibrated viewport")
        with self.lock:
            self.sequence += 1
            sent = time.monotonic_ns()
            self.process.stdin.write(f"{self.sequence} {x} {y}\n".encode("ascii"))
            answer = self._line(5).split()
            received = time.monotonic_ns()
            if (len(answer) != 7 or answer[:2] != ["OK", str(self.sequence)]
                    or not all(value.isdigit() for value in answer[2:])):
                raise LiveStateError("input helper touch receipt mismatch")
            duration, down_before, down_after, up_before, up_after = map(int, answer[2:])
            if not 0 < down_before <= down_after <= up_before <= up_after:
                raise LiveStateError("input helper touch timestamp order mismatch")
            self.last_timing = dict(schema="cr-live-touch-timing.v1", input_id=self.identity,
                request_sequence=self.sequence, host_sent_ns=sent, host_acknowledged_ns=received,
                device_down_before_ns=down_before, device_down_after_ns=down_after,
                device_up_before_ns=up_before, device_up_after_ns=up_after,
                clock_sync=dict(self.clock_sync))
            return int(answer[2])

    def close(self):
        # EOF is the remote helper's shutdown protocol; no global process kill.
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=2)
        self.selector.close()
        self.process.stdout.close()
        self.log.close()
