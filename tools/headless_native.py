"""Isolated engineering runner for a user-supplied ARM64 native runtime."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import gzip
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import sys
import time


LIBG_SHA256 = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + str(time.time_ns()) + ".new")
    with temporary.open("x") as output:
        output.write(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()
                if key not in ("generation", "stateEpoch")}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def object_sha(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def semantic_capture(capture, sequence_bases):
    result = canonical(capture)
    rich = result.get("rich", {})
    for name in ("combatEvents", "remainingRuntime", "phaseRuntime"):
        stream = rich.get(name)
        if not stream:
            continue
        base = sequence_bases.setdefault(name, stream["epochFirstSequence"])
        for key in ("epochFirstSequence", "oldestRetainedSequence", "nextSequence", "transmitFromSequence"):
            if key in stream:
                stream[key] -= base - 1
        for event in stream.get("events", []):
            for key in ("sequence", "causeSequence"):
                if event.get(key) is not None:
                    event[key] -= base - 1
    deployment_ids = []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "deploymentSequence" and isinstance(item, int) and item > 0:
                    deployment_ids.append(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(rich)
    if deployment_ids:
        deployment_base = sequence_bases.setdefault("deploymentSequence", min(deployment_ids))

        def rebase(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "deploymentSequence" and isinstance(item, int) and item > 0:
                        value[key] -= deployment_base - 1
                    else:
                        rebase(item)
            elif isinstance(value, list):
                for item in value:
                    rebase(item)

        rebase(rich)
    return result


def require_sha(path, expected):
    if len(expected) != 64 or digest(path) != expected:
        raise ValueError("identity mismatch: " + Path(path).name)


def verified_runtime(runtime, resident, pins):
    runtime, resident = Path(runtime).resolve(), Path(resident).resolve()
    paths = {"libg": runtime / "app-libs/libg.so",
             "probe": resident / "libresident.so",
             "bootstrap": resident / "libbootstrap.so",
             "dex": resident / "build/classes.dex",
             "verifier": runtime / "verify_sources.py"}
    for name, path in paths.items():
        require_sha(path, pins[name])
    spec = importlib.util.spec_from_file_location("headless_source_verifier", paths["verifier"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify(runtime)
    return {name: digest(path) for name, path in paths.items()}


def listener_owned(pid, port):
    try:
        inodes = set()
        for path in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(path)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
        return any(row.split()[3] == "0A" and row.split()[9] in inodes
                   and row.split()[1] == f"0100007F:{port:04X}" for row in rows)
    except (OSError, IndexError):
        return False


class NativeProcess:
    def __init__(self, runtime, resident, output, timeout=140):
        self.runtime, self.resident = Path(runtime).resolve(), Path(resident).resolve()
        self.output, self.timeout = Path(output).resolve(), timeout
        if any(self.output == source or source in self.output.parents
               for source in (self.runtime, self.resident)):
            raise ValueError("output must be outside frozen inputs")
        self.process = self.connection = self.reader = None
        self.handles = []
        self.startup_seconds = None

    def argv(self, port):
        runtime, resident = self.runtime, self.resident
        sysroot = runtime / "sysroot"
        manifest = json.loads((runtime / "runtime-manifest.json").read_text())
        java = json.loads((runtime / "java-manifest.json").read_text())
        state = self.output / "state"
        state.mkdir()
        for name in ("save", "cache", "java-data"):
            (state / name).mkdir()
        (state / "update").symlink_to(runtime / "resources/runtime-update", target_is_directory=True)
        environment = {
            "LD_LIBRARY_PATH": ":".join([str(resident), str(runtime / "app-libs")] +
                                        [str(sysroot / path.lstrip("/"))
                                         for path in manifest["library_paths"]]),
            "LD_PRELOAD": str(runtime / "libpropertypath.so"),
            "CR_PROPERTY_ROOT": str(sysroot / "dev/__properties__"),
            "CR_ENGINE_ROOT": str(state), "CR_RESIDENT_STATE": str(state),
            "CR_RESIDENT_BOOTSTRAP": str(resident / "libbootstrap.so"),
            "ANDROID_ROOT": str(sysroot / "system"), "ANDROID_DATA": str(state / "java-data"),
            "ANDROID_RUNTIME_ROOT": str(sysroot / "apex/com.android.runtime"),
            "ANDROID_ART_ROOT": str(sysroot / "apex/com.android.art"),
            "ANDROID_I18N_ROOT": str(sysroot / "apex/com.android.i18n"),
            "ANDROID_TZDATA_ROOT": str(sysroot / "apex/com.android.tzdata"),
        }
        command = [str(runtime / "qemu/usr/bin/qemu-aarch64-static"), "-L", str(sysroot)]
        for key, value in environment.items():
            command += ["-E", key + "=" + value]
        command += [str(sysroot / "apex/com.android.art/bin/dalvikvm64"),
                    "-Xbootclasspath:" + ":".join(str(sysroot / path.lstrip("/"))
                                                   for path in java["bootclasspath"]),
                    "-Xnorelocate", "-Ximage:/nonexistent/headless-boot.art", "-Xusejit:false",
                    "-Xnoimage-dex2oat", "-Xint", "-Xms16m", "-Xmx256m", "-cp",
                    str(resident / "build/classes.dex") + ":" + str(runtime / "original-classes.jar"),
                    "AssetProbe", str(runtime), str(runtime / "original.apk"), "reader",
                    str(resident / "libresident.so"), str(port)]
        return command

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=False)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        started = time.perf_counter()
        try:
            command = self.argv(self.port)
            self.handles = [(self.output / name).open("wb") for name in ("stdout.log", "stderr.log")]
            self.process = subprocess.Popen(command, cwd=self.output, stdout=self.handles[0],
                                            stderr=self.handles[1], start_new_session=True)
            (self.output / "process.json").write_text(json.dumps({"pid": self.process.pid,
                "port": self.port, "argv": command, "start_stat": self.stat()}, indent=2))
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("isolated engine exited: " + str(self.process.returncode))
                if listener_owned(self.process.pid, self.port):
                    status = self.request("status")
                    if status.get("coldReady"):
                        self.startup_seconds = time.perf_counter() - started
                        return self
                time.sleep(0.05)
            raise TimeoutError("isolated engine startup timeout")
        except BaseException:
            self.close()
            raise

    def request(self, command):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("owned engine is not running")
        if self.connection is None:
            if not listener_owned(self.process.pid, self.port):
                raise RuntimeError("port is not owned by isolated engine")
            self.connection = socket.create_connection(("127.0.0.1", self.port), timeout=30)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.reader = self.connection.makefile("rb")
            self.connection.sendall(b"session-v1\n")
            handshake = json.loads(self.reader.readline(4097))
            if handshake.get("ok") is not True or handshake.get("session") != "control-session.v1":
                raise ValueError("persistent native handshake rejected")
        self.connection.sendall((command + "\n").encode())
        raw = self.reader.readline(16 * 1024 * 1024 + 1)
        if not raw.endswith(b"\n") or len(raw) > 16 * 1024 * 1024:
            raise ValueError("invalid native response envelope")
        result = json.loads(raw)
        if result.get("ok") is not True:
            raise ValueError("native command rejected: " + command.split()[0])
        return result

    def stat(self):
        path = Path(f"/proc/{self.process.pid}")
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        status = dict(line.split(":", 1) for line in (path / "status").read_text().splitlines() if ":" in line)
        return {"start_ticks": fields[19],
                "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                "peak_rss_kib": int(status["VmHWM"].split()[0])}

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.process is not None:
            atomic_json(self.output / "cleanup.json", {"pid": self.process.pid,
                "returncode": self.process.poll(), "reaped": self.process.poll() is not None})
        for handle in self.handles:
            handle.close()

    def __exit__(self, *exception):
        self.close()


def validate_fixture(fixture):
    if fixture.get("schema") != "headless-native-fixture.v1":
        raise ValueError("unsupported engineering fixture")
    if fixture["match"].get("cmd") or fixture["match"].get("evt"):
        raise ValueError("match must start without recorded commands")
    previous = -1
    for action in fixture["actions"]:
        tick = action["tick"]
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < previous or tick < 90:
            raise ValueError("invalid action tick")
        previous = tick
        if action["command"].get("ct") != 86:
            raise ValueError("unsupported native command")


def play(engine, fixture, chunk=20, progress=False, observation_ticks=20, capture_mode="rich"):
    validate_fixture(fixture)
    if chunk < 1 or observation_ticks < 1:
        raise ValueError("positive step and observation intervals required")
    reset_started = time.perf_counter()
    engine.request("configure " + json.dumps(fixture["match"], separators=(",", ":")))
    reset_seconds = time.perf_counter() - reset_started
    actions = {}
    for action in fixture["actions"]:
        actions.setdefault(action["tick"], []).append(action)
    boundaries = sorted(set(range(0, 10001, observation_ticks)) | set(actions) | {10000})
    rows, receipts, sent = [], {}, []
    sequence_bases = {}
    audit_path = engine.output / ("trace-" + str(time.time_ns()) + ".jsonl.gz")
    tick, limit = 0, 10000
    started = time.perf_counter()
    cpu_start = engine.stat()["cpu_seconds"]
    timings = {"capture": 0.0, "step": 0.0, "inject": 0.0}
    while tick < limit:
        phase_start = time.perf_counter()
        if capture_mode == "rich":
            capture = engine.request("observe-atomic")
        elif capture_mode == "ordinary":
            ordinary = engine.request("observe")
            capture = {"tick": ordinary["tick"], "ordinary": ordinary,
                       "rich": {"combatEvents": {"events": []}}}
            if ordinary.get("ended") or (tick > 0 and tick % 1000 == 0):
                capture = engine.request("observe-atomic")
        else:
            raise ValueError("unsupported capture mode")
        timings["capture"] += time.perf_counter() - phase_start
        ordinary = capture["ordinary"]
        if ordinary["tick"] != tick or capture["tick"] != tick:
            raise ValueError("atomic tick mismatch")
        combat = capture["rich"]["combatEvents"]
        if combat.get("droppedCount", 0):
            raise ValueError("native action evidence overflow")
        for event in combat.get("events", []):
            if event.get("kind") == "card_play":
                receipts[event["sequence"]] = event
        semantic = semantic_capture(capture, sequence_bases) if capture_mode == "rich" else ordinary
        rows.append({"tick": tick, "state_sha256": object_sha(semantic)})
        if capture_mode == "rich":
            with gzip.open(audit_path, "at", compresslevel=1) as audit:
                audit.write(json.dumps(capture, separators=(",", ":")) + "\n")
        if ordinary.get("ended"):
            terminal = ordinary
            break
        for action in actions.get(tick, []):
            command = action["command"]["c"]
            owner = next((player for player in ordinary["players"]
                          if player["accountIdHigh"] == command["idHi"]
                          and player["accountIdLow"] == command["idLo"]), None)
            if owner is None or not any(card["commandCardId"] == command["sel"]["os"]
                                        and card["cardParameter"] == command["sel"]["pd"]
                                        and card["cost"] * 10000 <= owner["elixirRaw"]
                                        for card in owner["hand"]):
                raise ValueError("fixture action is not in affordable current hand")
            phase_start = time.perf_counter()
            engine.request("inject " + json.dumps(action["command"], separators=(",", ":")))
            timings["inject"] += time.perf_counter() - phase_start
            sent.append((owner["owner"], command["sel"]["os"], command["t2"], command["sel"]["pd"]))
        target = next(boundary for boundary in boundaries if boundary > tick)
        while tick < target:
            requested = min(chunk, target - tick)
            phase_start = time.perf_counter()
            step = engine.request("step " + str(requested))
            timings["step"] += time.perf_counter() - phase_start
            if not tick < step["tick"] <= tick + requested:
                raise ValueError("native progression stalled or overshot")
            tick = step["tick"]
            if step.get("ended"):
                break
        if progress and tick % 1000 == 0:
            print(json.dumps({"progress_tick": tick, "seconds": time.perf_counter() - started}), flush=True)
    else:
        raise ValueError("native match truncated")
    if not terminal.get("finalized") or terminal.get("queuedCommands") or len(sent) != len(fixture["actions"]):
        raise ValueError("incomplete native terminal")
    executed = sorted((event["deploymentContext"]["owner"],
                       event["deploymentContext"]["playedCardGlobalId"], event["tick"],
                       event["deploymentContext"]["cardParameter"])
                      for event in receipts.values())
    if sorted(sent) != executed:
        (engine.output / "receipt-failure.json").write_text(json.dumps(
            {"submitted": sorted(sent), "executed": executed, "terminal_tick": tick}, indent=2))
        raise ValueError("native execution receipts differ from submitted commands")
    elapsed = time.perf_counter() - started
    stats = engine.stat()
    return {"seconds": elapsed, "reset_seconds": reset_seconds,
            "ticks": tick, "winner": terminal["winner"], "actions": len(sent),
            "trace_sha256": object_sha(rows), "terminal_sha256": object_sha(terminal),
            "private_trace_file": audit_path.name if capture_mode == "rich" else None,
            "boundary_count": len(rows), "cpu_seconds": stats["cpu_seconds"] - cpu_start,
            "peak_rss_kib": stats["peak_rss_kib"], "timings_seconds": timings,
            "capture_mode": capture_mode,
            "complete": True, "rejected": 0}


def snapshot_check(engine, fixture):
    engine.request("configure " + json.dumps(fixture["match"], separators=(",", ":")))
    engine.request("step 90")
    before = engine.request("state-digest")
    snapshot = engine.request("snapshot-create")
    handle = snapshot["handle"]
    try:
        engine.request("step 40")
        engine.request("restore " + str(handle))
        after = engine.request("state-digest")
        if before != after:
            raise ValueError("snapshot roundtrip altered state digest")
    finally:
        engine.request("release-snapshot " + str(handle))
    if engine.request("status").get("snapshotHandles") != 0:
        raise ValueError("snapshot handle leak")
    return {"roundtrip": True, "handles_remaining": 0}


def protocol_check(engine, fixture):
    configured = "configure " + json.dumps(fixture["match"], separators=(",", ":"))
    engine.request(configured)
    before = engine.request("state-digest")
    try:
        engine.request("step 0")
    except ValueError:
        pass
    else:
        raise ValueError("invalid zero tick advance accepted")
    if engine.request("state-digest") != before:
        raise ValueError("rejected command changed state")
    snapshot = engine.request("snapshot-create")
    engine.request(configured)
    try:
        engine.request("restore " + str(snapshot["handle"]))
    except ValueError:
        pass
    else:
        raise ValueError("stale snapshot survived reset")
    return {"invalid_step_rejected": True, "rejection_preserved_state": True,
            "reset_invalidated_snapshot": True}


def aggregate(rounds, concurrency, baseline=None):
    elapsed = [round_["seconds"] for round_ in rounds]
    matches = [match for round_ in rounds for match in round_["matches"]]
    latencies = sorted(match["seconds"] + match["reset_seconds"] for match in matches)
    throughput = len(matches) / sum(elapsed)
    total_ticks = sum(match["ticks"] for match in matches)
    return {"concurrency": concurrency, "repetitions": len(rounds), "completed_matches": len(matches),
            "matches_per_second": throughput, "seconds_per_match": sum(elapsed) / len(matches),
            "ticks_per_second": total_ticks / sum(elapsed),
            "game_seconds_per_second": total_ticks / 20 / sum(elapsed),
            "mean_match_latency_seconds": statistics.mean(latencies),
            "p95_match_latency_seconds": latencies[max(0, math.ceil(len(latencies) * .95) - 1)],
            "round_seconds_stdev": statistics.stdev(elapsed) if len(elapsed) > 1 else 0,
            "round_throughput_median": statistics.median(concurrency / seconds for seconds in elapsed),
            "cpu_core_equivalents": sum(match["cpu_seconds"] for match in matches) / sum(elapsed),
            "max_process_peak_rss_kib": max(match["peak_rss_kib"] for match in matches),
            "scaling_efficiency": throughput / (baseline * concurrency) if baseline else 1,
            "complete_fraction": 1, "rejected_fraction": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "resident", "output", "fixture"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("probe", "bootstrap", "dex", "verifier"):
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--concurrency", default="1,2,4,8")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--observation-ticks", type=int, default=20)
    parser.add_argument("--capture-mode", choices=("rich", "ordinary"), default="rich")
    parser.add_argument("--step-chunk", type=int)
    parser.add_argument("--protocol-check", action="store_true")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("real native runtime requires a Linux host")
    levels = [int(value) for value in args.concurrency.split(",")]
    if levels != sorted(set(levels)) or not levels or levels[0] != 1 or levels[-1] > 8 or args.repeats < 2 or args.observation_ticks < 1:
        parser.error("use increasing concurrency 1..8 and at least two repetitions")
    if args.step_chunk is not None and args.step_chunk < 1:
        parser.error("step chunk must be positive")
    if shutil.disk_usage(args.output.parent).free < 5 * 1024 ** 3:
        raise RuntimeError("diagnostic disk headroom below 5 GiB")
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if int(meminfo["MemAvailable"].split()[0]) < 32 * 1024 ** 2:
        raise RuntimeError("memory headroom below 32 GiB")
    sys.dont_write_bytecode = True
    pins = {"libg": LIBG_SHA256, **{name: getattr(args, name + "_sha256")
                                    for name in ("probe", "bootstrap", "dex", "verifier")}}
    identities = verified_runtime(args.runtime, args.resident, pins)
    identities["qemu"] = digest(args.runtime / "qemu/usr/bin/qemu-aarch64-static")
    identities["property_shim"] = digest(args.runtime / "libpropertypath.so")
    fixture = json.loads(args.fixture.read_text())
    validate_fixture(fixture)
    args.output.mkdir(parents=False, exist_ok=False)
    report = {"schema": "headless-native-benchmark.v1", "identity": identities,
              "runner_sha256": digest(__file__), "fixture_sha256": digest(args.fixture),
              "training_admissible": False, "android_emulator_started": False,
              "observation_ticks": args.observation_ticks, "levels": []}
    report["capture_mode"] = args.capture_mode
    report["step_chunk"] = args.step_chunk or args.observation_ticks
    report["host"] = {"architecture": os.uname().machine, "logical_cpus": os.cpu_count(),
                      "cpu_model": next((line.split(":", 1)[1].strip()
                                         for line in Path("/proc/cpuinfo").read_text().splitlines()
                                         if line.startswith("model name")), "unknown")}
    baseline = None
    expected_trace = None
    for concurrency in levels:
        engines = []
        try:
            for index in range(concurrency):
                engine = NativeProcess(args.runtime, args.resident, args.output / f"c{concurrency}-p{index}")
                engines.append(engine)
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(lambda engine: engine.__enter__(), engines))
            attestations = [engine.request("attest") for engine in engines]
            if concurrency == 1:
                report["snapshot_check"] = snapshot_check(engines[0], fixture)
                if args.protocol_check:
                    report["protocol_check"] = protocol_check(engines[0], fixture)
            rounds = []
            for repeat in range(args.repeats):
                started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    matches = list(pool.map(lambda engine: play(engine, fixture, chunk=report["step_chunk"],
                                                               observation_ticks=args.observation_ticks, progress=True,
                                                               capture_mode=args.capture_mode), engines))
                rounds.append({"seconds": time.perf_counter() - started, "matches": matches})
                atomic_json(args.output / f"round-c{concurrency}-r{repeat}.json", rounds[-1])
                for match in matches:
                    trace = (match["trace_sha256"], match["terminal_sha256"])
                    expected_trace = trace if expected_trace is None else expected_trace
                    if trace != expected_trace:
                        raise ValueError("repeated or concurrent native trajectory mismatch")
                print(json.dumps({"concurrency": concurrency, "repeat": repeat,
                                  "ticks": [match["ticks"] for match in matches]}), flush=True)
            summary = aggregate(rounds, concurrency, baseline)
            if baseline is None:
                baseline = summary["matches_per_second"]
            report["levels"].append({"summary": summary, "rounds": rounds,
                                     "startup_seconds": [engine.startup_seconds for engine in engines],
                                     "attestation_sha256": [object_sha(value) for value in attestations]})
            atomic_json(args.output / "results.json", report)
        finally:
            for engine in engines:
                engine.close()
    report["status"] = "NATIVE_HEADLESS_FEASIBLE"
    atomic_json(args.output / "results.json", report)
    print(json.dumps({"status": report["status"], "levels": [level["summary"] for level in report["levels"]]}))


if __name__ == "__main__":
    main()
