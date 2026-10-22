import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import time


PROBE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
LIBG_SHA = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
SYMBOLS = dict(
    manager="_ZN12_GLOBAL__N_120g_controlled_managerE",
    generation="_ZN12_GLOBAL__N_120g_control_generationE",
    epoch="_ZN12_GLOBAL__N_113g_state_epochE",
    tick="_ZN12_GLOBAL__N_122g_last_controlled_tickE",
    worker="_ZN12_GLOBAL__N_123g_native_worker_runningE",
    libg="_ZN12_GLOBAL__N_111g_libg_baseE")


def next_native_state(state):
    if type(state) is not int or not 0 <= state <= 0xffffffff:
        raise ValueError("PRNG state must be an unsigned 32-bit integer")
    value = state or 0xffffffff
    value = (value ^ (value << 13)) & 0xffffffff
    signed = value if value < 0x80000000 else value - 0x100000000
    value = (value ^ (signed >> 17)) & 0xffffffff
    return (value ^ (value << 5)) & 0xffffffff


def parse_symbols(output):
    resolved = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 8 and fields[-1] in SYMBOLS.values():
            key = next(key for key, name in SYMBOLS.items() if name == fields[-1])
            if key in resolved or fields[3] != "OBJECT" or fields[6] == "UND":
                raise ValueError("ambiguous native audit symbol")
            resolved[key] = int(fields[1], 16)
    if set(resolved) != set(SYMBOLS):
        raise ValueError("native audit symbols missing")
    return resolved


def mapped_base(maps, path):
    matches = []
    for line in maps.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5] == str(path) and int(fields[2], 16) == 0:
            matches.append(int(fields[0].split("-")[0], 16))
    if len(matches) != 1:
        raise ValueError("native audit requires one exact mapped library")
    return matches[0]


def process_start_ticks(stat):
    return int(stat.rsplit(")", 1)[1].split()[19])


def mapped_guest_pointer(pointer, bias, maps, size):
    candidates = {pointer + bias, (pointer & 0x00ffffffffffffff) + bias}
    matches = set()
    for line in maps.splitlines():
        fields = line.split()
        start, end = (int(value, 16) for value in fields[0].split("-"))
        if fields[1].startswith("rw"):
            matches.update(address for address in candidates if start <= address and address + size <= end)
    if len(matches) != 1:
        raise ValueError(f"native pointer has no unique writable mapping: {pointer:#x}, bias={bias:#x}")
    return matches.pop()


class NativePrngAudit:
    def __init__(self, instance, *, probe, libg, port):
        self.instance = Path(instance).resolve()
        self.probe, self.libg = Path(probe).resolve(), Path(libg).resolve()
        self.record = json.loads((self.instance / "process.json").read_text())
        self.pid = int(self.record["child_pid"])
        self.port = port
        for path, expected in ((self.probe, PROBE_SHA), (self.libg, LIBG_SHA)):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("native PRNG audit binary identity differs")
        symbols = subprocess.run(["readelf", "-sW", str(self.probe)], check=True,
            capture_output=True, text=True, timeout=10)
        self.symbols = parse_symbols(symbols.stdout)
        self._verify_process()

    def _verify_process(self):
        process = Path("/proc") / str(self.pid)
        current = json.loads((self.instance / "process.json").read_text())
        if (current != self.record or current["phase"] != "ready" or current["port"] != self.port
                or process_start_ticks((process / "stat").read_text()) != int(current["child_start_ticks"])
                or (process / "cwd").resolve() != self.instance
                or (process / "cmdline").read_bytes().split(b"\0")[0].split(b"/")[-1] != b"qemu-aarch64-static"):
            raise ValueError("native audit process ownership changed")
        return process

    @contextlib.contextmanager
    def _paused(self):
        process = self._verify_process()
        os.kill(self.pid, signal.SIGSTOP)
        try:
            deadline = time.monotonic() + 5
            while True:
                statuses = [path.read_text().rsplit(")", 1)[1].split()[0]
                    for path in (process / "task").glob("*/stat")]
                if statuses and all(status in ("T", "t") for status in statuses):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("native audit process did not stop")
                time.sleep(.01)
            self._verify_process()
            yield process
        finally:
            if process.exists() and process_start_ticks((process / "stat").read_text()) == int(self.record["child_start_ticks"]):
                os.kill(self.pid, signal.SIGCONT)

    def inspect(self, identity, *, advance=False, expected=None):
        with self._paused() as process:
            maps = (process / "maps").read_text()
            probe_base = mapped_base(maps, self.probe)
            libg_base = mapped_base(maps, self.libg)
            descriptor = os.open(process / "mem", os.O_RDWR if advance else os.O_RDONLY)
            try:
                def read(address, format):
                    size = struct.calcsize(format)
                    content = os.pread(descriptor, size, address)
                    if len(content) != size:
                        raise ValueError("short native memory read")
                    return struct.unpack(format, content)[0]

                def symbol(key, format):
                    return read(probe_base + self.symbols[key], format)

                actual = dict(generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), tick=symbol("tick", "<i"))
                if actual != {key: identity[key] for key in actual} or symbol("worker", "<B"):
                    raise ValueError("stale or active native execution lane")
                guest_bias = libg_base - symbol("libg", "<Q")
                manager = symbol("manager", "<Q")
                manager_address = mapped_guest_pointer(manager, guest_bias, maps, 0x220)
                if not manager or read(manager_address + 0x60, "<i") != actual["tick"]:
                    raise ValueError("native manager tick differs")
                address = manager_address + 0xa0
                before = read(address, "<I")
                if expected is not None and before != expected:
                    raise ValueError("native PRNG compare-and-swap mismatch")
                after = next_native_state(before) if advance else before
                if advance:
                    if after == before:
                        raise ValueError("native PRNG perturbation did not change state")
                    if os.pwrite(descriptor, struct.pack("<I", after), address) != 4:
                        raise ValueError("short native PRNG write")
                    if read(address, "<I") != after:
                        raise ValueError("native PRNG mutation readback differs")
                return dict(**actual, before=before, after=after, changed=before != after,
                    manager_offset="0xa0", algorithm="native-xorshift32-arithmetic-shift",
                    manager_pointer=hex(manager), manager_host_address=hex(manager_address), guest_bias=hex(guest_bias),
                    probe_sha256=PROBE_SHA, libg_sha256=LIBG_SHA,
                    child_pid=self.pid, child_start_ticks=self.record["child_start_ticks"])
            finally:
                os.close(descriptor)
