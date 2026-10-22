"""Owned local AVD replay, exact receipt verification, and MP4 export."""

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import socket
import subprocess
import time

from .config import write_json
from .guest_storage import cleanup_guest_recordings


class Cancelled(Exception):
    pass


class GuestStorageError(RuntimeError):
    pass


def birth(pid):
    return subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()


def live(owner):
    return bool(owner and owner.get("birth") and birth(owner["pid"]) == owner["birth"])


def validate_trace(root, request):
    stats = json.loads((root / "stats.json").read_text())
    actions = json.loads((root / "native-actions.json").read_text())
    if stats.get("action_constraints") != request.get("action_constraints"):
        raise ValueError("轨迹动作约束身份不一致")
    if (
        stats["owner_checkpoint_sha256"]
        != [request[x]["sha256"] for x in ("top", "bottom")]
        or stats["seed"] != request["seed"]
    ):
        raise ValueError("轨迹权重或种子身份不一致")
    if stats["decks"] != [request["deck_top"], request["deck_bottom"]]:
        raise ValueError("轨迹卡组身份不一致")
    if "deck_cards" in request:
        from .generate_match import resolve_decks

        decks = resolve_decks(request)
        if stats.get("deck_cards") != request["deck_cards"]:
            raise ValueError("轨迹完整牌表不一致")
        battle = json.loads((root / "render-match.json").read_text())["battle"]
        initial = json.loads((root / "initial.json").read_text())
        players = {p["owner"]: p for p in initial["players"]}
        for owner, cards in decks.items():
            if [c["d"] for c in battle[f"deck{owner}"]["sp"]] != list(cards):
                raise ValueError("生成引擎牌表与任务不一致")
            dealt = [c["cardId"] for key in ("hand", "cycle") for c in players[owner][key]]
            if sorted(dealt) != sorted(cards):
                raise ValueError("开场发牌与任务牌表不一致")
        if any(a["receipt"]["cardId"] not in decks.get(a["owner"], ()) for a in actions):
            raise ValueError("原生回执包含牌表之外的卡牌")
    counters = stats["counters"]
    if (
        not stats["terminal"]["ended"]
        or stats["terminal"]["reason"] != "native_final_result"
    ):
        raise ValueError("对局未到真实终局")
    if counters["scheduler_version"] != "event-scheduling.v2" or any(
        counters[k] for k in ("not_ready", "cancelled_window", "act_violation")
    ):
        raise ValueError("轨迹未通过新调度校验")
    if counters["executed"] != len(actions):
        raise ValueError("原生出牌数不一致")
    for a in actions:
        receipt = a["receipt"]
        if not receipt["ok"] or not receipt["queued"] or receipt["owner"] != a["owner"]:
            raise ValueError("原生出牌回执无效")
        if not 0 < receipt["executeTick"] <= stats["terminal"]["terminal_tick"]:
            raise ValueError("原生出牌执行时间无效")
        if (
            a["owner"] not in (0, 1)
            or a["world_x"] % 1000 != 500
            or a["world_y"] % 1000 != 500
        ):
            raise ValueError("原生坐标或玩家编号无效")
    return stats, actions


def validate_replay(root, request):
    stats, actions = validate_trace(root, request)
    render = json.loads((root / "render-stats.json").read_text())
    schedules = render["schedules"]
    if len(actions) != len(schedules):
        raise ValueError("回放出牌总数不一致")
    for a, s in zip(actions, schedules):
        if (
            a["owner"],
            a["receipt"]["cardId"],
            a["receipt"]["executeTick"],
            "succeeded",
        ) != (s["owner"], s["cardId"], s["executeTick"], s["state"]):
            raise ValueError("回放动作身份不一致")
    for p in render["per_owner"].values():
        if p["scheduled"] != p["succeeded"] or p["failed"] or p["pending"]:
            raise ValueError("有出牌未成功执行")
    expected = json.loads((root / "initial.json").read_text())
    actual = json.loads((root / "render-initial.json").read_text())
    by_owner = {p["owner"]: p for p in actual["players"]}
    for p in expected["players"]:
        for key in ("hand", "cycle"):
            if [x["cardId"] for x in p[key]] != [
                x["cardId"] for x in by_owner[p["owner"]][key]
            ]:
                raise ValueError("开场发牌不一致")
    final = render["final_observation"]
    if not final["ended"] or int(final["winner"]) != int(stats["terminal"]["winner"]):
        raise ValueError("终局或胜方不一致")
    if not render["final_status"]["ended"]:
        raise ValueError("渲染状态未确认终局")
    return stats, render


class Renderer:
    def __init__(self, config, root, request, progress, cancelled):
        self.c = config
        self.root = Path(root)
        self.request = request
        self.progress = progress
        self.cancelled = cancelled
        self.emulator = None
        self.owner = None

    def check(self):
        if self.cancelled():
            raise Cancelled("任务已取消")

    def adb(self, *args, timeout=30):
        return subprocess.run(
            [self.c.adb, "-s", self.c.serial, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def ctl(self, command, timeout=20):
        with socket.create_connection(
            ("127.0.0.1", self.c.control_port), timeout=timeout
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall((command + "\n").encode())
            data = b""
            while b"\n" not in data:
                part = sock.recv(65536)
                if not part:
                    break
                data += part
        result = json.loads(data)
        if result.get("ok") is False:
            raise RuntimeError(str(result)[:600])
        return result

    def boot(self):
        for attempt in (1, 2):
            try:
                self.boot_once(attempt)
                return
            except GuestStorageError:
                self.boot_diagnostics(attempt)
                self.close()
                raise
            except TimeoutError:
                self.boot_diagnostics(attempt)
                self.close()
                if live(self.owner):
                    raise RuntimeError("启动超时的模拟器仍在退出，已停止重试")
                self.owner = None
                self.emulator = None
                if attempt == 2:
                    raise TimeoutError("本机模拟器启动超时（已重试，详情见 boot-diagnostics-2.json）")
                self.check()
                self.progress("booting_local", message="本机启动超时，正在重试一次")

    def boot_diagnostics(self, attempt):
        commands = {
            "devices": [self.c.adb, "devices", "-l"],
            "state": [self.c.adb, "-s", self.c.serial, "get-state"],
            "properties": [self.c.adb, "-s", self.c.serial, "shell", "getprop"],
            "logcat": [self.c.adb, "-s", self.c.serial, "logcat", "-d", "-t", "200"],
            "disk": [self.c.adb, "-s", self.c.serial, "shell", "df", "-k", "/data"],
        }
        diagnostics = dict(attempt=attempt, time=time.time(), owner=self.owner, probes={})
        for name, args in commands.items():
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                diagnostics["probes"][name] = dict(
                    returncode=result.returncode,
                    stdout=result.stdout[-40000:], stderr=result.stderr[-4000:],
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                diagnostics["probes"][name] = dict(error=str(exc))
        write_json(self.root / f"boot-diagnostics-{attempt}.json", diagnostics)

    def check_guest_space(self, minimum, *, required=True):
        try:
            result = self.adb("shell", "stat", "-f", "-c", "%a:%S", "/data", timeout=5)
            available, block_size = map(int, result.stdout.strip().split(":"))
            if result.returncode or available < 0 or block_size <= 0:
                raise ValueError("invalid filesystem statistics")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if required:
                raise RuntimeError("模拟器内部空间检查失败，详情见启动诊断")
            return
        free = available * block_size
        write_json(self.root / "guest-disk.json", dict(free_bytes=free, required_bytes=minimum, time=time.time()))
        if free < minimum:
            raise GuestStorageError(
                f"模拟器内部 /data 空间不足：剩余 {free / 2**20:.0f} MiB，"
                f"需要至少 {minimum / 2**20:.0f} MiB（不是 Mac 磁盘）"
            )

    def cleanup_guest_storage(self, phase):
        if not live(self.owner) or self.owner.get("serial") != self.c.serial:
            return
        return cleanup_guest_recordings(self.c, self.root, self.adb, phase=phase)

    def check_crash_dialog(self):
        result = self.adb("shell", "dumpsys", "window", timeout=10)
        if result.returncode:
            raise RuntimeError("录制窗口状态检查失败")
        focus = [line.strip() for line in result.stdout.splitlines()
                 if "mCurrentFocus=" in line]
        if not focus:
            raise RuntimeError("录制窗口焦点状态缺失")
        if any("Application Error:" in line or "Application Not Responding:" in line
               for line in focus):
            write_json(self.root / "visual-review.json", dict(
                passed=False, reason="Android crash dialog blocks game", focus=focus,
            ))
            raise RuntimeError("系统崩溃弹窗遮挡游戏画面，已停止发布录像")

    def boot_once(self, attempt):
        while True:
            self.check()
            devices = subprocess.run(
                [self.c.adb, "devices"], capture_output=True, text=True, check=True
            ).stdout
            processes = subprocess.run(
                ["ps", "-axo", "command="], capture_output=True, text=True, check=True
            ).stdout
            busy = any(
                x.split() and x.split()[0] == self.c.serial
                for x in devices.splitlines()[1:]
            ) or any(
                "qemu-system" in x and "-avd " + self.c.avd in x
                for x in processes.splitlines()
            )
            if not busy:
                try:
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", self.c.control_port))
                    break
                except OSError:
                    busy = True
            self.progress("waiting_local", message="等待本机模拟器空闲")
            time.sleep(2)
        self.progress("booting_local", message="启动本机渲染环境")
        args = [
            self.c.emulator,
            "-avd",
            self.c.avd,
            "-port",
            self.c.serial.split("-")[-1],
            "-cores",
            "6",
            "-gpu",
            "host",
            "-no-window",
            "-no-snapshot-load",
            "-no-snapshot-save",
            "-no-boot-anim",
        ]
        with (self.root / "emulator.log").open("ab") as log:
            log.write(f"\nBoot attempt {attempt} at {time.time()}\n".encode())
            log.flush()
            self.emulator = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        self.owner = dict(
            pid=self.emulator.pid, birth=birth(self.emulator.pid), serial=self.c.serial
        )
        write_json(self.root / "emulator-owner.json", self.owner)
        deadline = time.monotonic() + 180
        next_disk_check = 0
        while time.monotonic() < deadline:
            self.check()
            if self.emulator.poll() is not None:
                raise RuntimeError("本机模拟器启动失败，详情见任务日志")
            try:
                result = self.adb("shell", "getprop", "sys.boot_completed", timeout=5)
                sample = dict(returncode=result.returncode, stdout=result.stdout.strip(),
                              stderr=result.stderr.strip())
            except subprocess.TimeoutExpired:
                sample = dict(error="adb boot probe timed out")
            with (self.root / "boot-status.jsonl").open("a") as stream:
                stream.write(json.dumps(dict(attempt=attempt, time=time.time(), **sample)) + "\n")
            if time.monotonic() >= next_disk_check:
                try:
                    self.check_guest_space(256 * 2**20, required=False)
                except GuestStorageError:
                    self.cleanup_guest_storage("boot-low-space")
                    self.check_guest_space(256 * 2**20, required=False)
                next_disk_check = time.monotonic() + 10
            if sample.get("returncode") == 0 and sample.get("stdout") == "1":
                break
            time.sleep(1)
        else:
            raise TimeoutError("本机模拟器启动超时")
        self.adb("root")
        self.adb("wait-for-device")
        self.cleanup_guest_storage("startup")
        self.check_guest_space(600 * 2**20)
        self.adb("shell", "svc", "power", "stayon", "true")

    def configure(self, payload):
        self.check()
        if self.adb("shell", "pidof", "screenrecord").stdout.strip():
            raise RuntimeError("模拟器中已有录制任务")
        if self.adb("shell", "su", "0", "ip", "link", "set", "eth0", "down").returncode:
            raise RuntimeError("客户机离线配置失败")
        package = "nullsroyale.rel.free"
        self.adb("shell", "am", "force-stop", package)
        time.sleep(1)
        self.adb(
            "shell",
            "am",
            "start",
            "--user",
            "0",
            "-f",
            "0x18000000",
            "-n",
            package + "/com.supercell.clashroyale.Engine0App",
        )
        self.adb("forward", "tcp:" + str(self.c.control_port), "tcp:26789")
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            self.check()
            try:
                if self.ctl("attest", 1).get("attestation", {}).get("production_ready"):
                    configured = self.ctl("configure-native " + json.dumps(payload), 60)
                    break
            except (OSError, RuntimeError, ValueError):
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError("本机引擎未就绪")
        self.ctl("pause")
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            self.check()
            status = self.ctl("status")
            if (
                status.get("nativeRenderReady")
                and int(status.get("nativeRenderLoaded", 0)) >= configured["sequence"]
            ):
                self.ctl("pause")
                return self.ctl("observe")
            time.sleep(0.25)
        raise TimeoutError("画面未就绪")

    def render(self):
        stats, actions = validate_trace(self.root, self.request)
        expected = json.loads((self.root / "initial.json").read_text())
        payload = json.loads((self.root / "render-match.json").read_text())
        try:
            self.boot()
            self.progress("aligning", message="核对双方手牌和抽牌顺序")
            actual = self.configure(payload)
            alignment = []
            by_owner = {p["owner"]: p for p in actual["players"]}
            for want in expected["players"]:
                owner = want["owner"]
                have = by_owner[owner]
                entries = [None] * 8
                cards = {c["d"]: c for c in payload["battle"][f"deck{owner}"]["sp"]}
                for dst, src in zip(
                    have["hand"] + have["cycle"],
                    want["hand"] + want["cycle"],
                    strict=True,
                ):
                    entries[dst["deckSlot"]] = dict(cards[src["cardId"]])
                if not all(entries) or sorted(c["d"] for c in entries) != sorted(cards):
                    raise ValueError("发牌对齐失败")
                payload["battle"][f"deck{owner}"]["sp"] = entries
                side = ("top", "bottom")[owner]
                iteration = self.request[side]["iteration"]
                payload["battle"][f"avatar{owner}"]["name"] = f"RL{iteration} - {side}"
                alignment.append(dict(owner=owner, deck=entries))
            write_json(self.root / "alignment.json", alignment)
            write_json(self.root / "render-match-aligned.json", payload)
            actual = self.configure(payload)
            write_json(self.root / "render-initial.json", actual)
            actual_by = {p["owner"]: p for p in actual["players"]}
            for want in expected["players"]:
                for key in ("hand", "cycle"):
                    if [c["cardId"] for c in want[key]] != [
                        c["cardId"] for c in actual_by[want["owner"]][key]
                    ]:
                        raise ValueError("发牌对齐复核失败")
            sequences = []
            for a in actions:
                self.check()
                q = a["receipt"]
                r = self.ctl(
                    f"replay-schedule-card {a['owner']} {q['cardId']} {a['world_x']} {a['world_y']} {q['executeTick']}"
                )
                sequences.append((a["owner"], r["sequence"]))
            write_json(self.root / "registered-schedules.json", sequences)
            speed = 2 if stats["terminal"]["terminal_tick"] <= 6400 else 4
            limit = min(
                180, math.ceil(stats["terminal"]["terminal_tick"] / (20 * speed)) + 20
            )
            self.ctl("render on")
            self.ctl(f"speed {speed}")
            record = "/sdcard/render-web-" + self.root.name + ".mp4"
            self.check_guest_space(600 * 2**20)
            self.check_crash_dialog()
            with (self.root / "screenrecord.log").open("wb") as log:
                recorder = subprocess.Popen(
                    [
                        self.c.adb,
                        "-s",
                        self.c.serial,
                        "shell",
                        "screenrecord",
                        "--verbose",
                        "--time-limit",
                        str(limit),
                        record,
                    ],
                    stdout=log,
                    stderr=log,
                )
                write_json(
                    self.root / "recording.json",
                    dict(
                        pid=recorder.pid,
                        birth=birth(recorder.pid),
                        limit=limit,
                        start_time=time.time(),
                        record=record,
                        speed=speed,
                    ),
                )
                try:
                    time.sleep(1)
                    if recorder.poll() is not None:
                        raise RuntimeError("录屏启动失败")
                    start = time.monotonic()
                    self.ctl("resume")
                    last = -1
                    last_progress = start
                    next_window_check = start
                    with (self.root / "render-status.jsonl").open("w") as stream:
                        while time.monotonic() - start < limit - 5:
                            self.check()
                            s = self.ctl("status")
                            now = time.monotonic()
                            if now >= next_window_check:
                                self.check_crash_dialog()
                                next_window_check = time.monotonic() + 3
                            tick = int(s.get("tick", -1))
                            stream.write(
                                json.dumps(dict(wall_s=now - start, status=s)) + "\n"
                            )
                            stream.flush()
                            self.progress(
                                "recording",
                                tick=tick,
                                terminal_tick=stats["terminal"]["terminal_tick"],
                                message="录制真实游戏画面",
                            )
                            if tick != last:
                                last = tick
                                last_progress = now
                            if s.get("ended") or s.get("finalized"):
                                ending = now - start
                                break
                            if now - last_progress > 12:
                                raise RuntimeError("回放时钟超过 12 秒未推进")
                            time.sleep(0.5)
                        else:
                            raise TimeoutError("录屏时间内未到终局")
                    final = self.ctl("observe")
                    write_json(self.root / "render-final.json", final)
                    per = {
                        str(o): dict(scheduled=0, succeeded=0, failed=0, pending=0)
                        for o in (0, 1)
                    }
                    receipts = []
                    for owner, sequence in sequences:
                        q = self.ctl(f"replay-schedule-status {sequence}")
                        receipts.append(q)
                        per[str(owner)]["scheduled"] += 1
                        key = q.get("state", "unknown")
                        per[str(owner)][key] = per[str(owner)].get(key, 0) + 1
                    write_json(
                        self.root / "render-stats.json",
                        dict(
                            final_status=s,
                            final_observation={
                                k: final.get(k)
                                for k in ("tick", "winner", "ended", "finalized")
                            },
                            per_owner=per,
                            schedules=receipts,
                            ending_wall_s=ending,
                            record=record,
                            record_limit=limit,
                            speed=speed,
                        ),
                    )
                finally:
                    self.progress(
                        "finishing_recording",
                        message="正在结束录制，等待视频文件完整写入",
                    )
                    recorder.wait(timeout=limit + 30)
                    write_json(
                        self.root / "recording-exit.json",
                        dict(returncode=recorder.returncode, time=time.time(),
                             record=record, limit=limit),
                    )
                    result = self.adb(
                        "pull", record, str(self.root / "native-full.mp4"), timeout=120
                    )
                    if result.returncode:
                        raise RuntimeError("录屏文件拉取失败")
                    # close() verifies the host copy before reclaiming the guest
                    # file, including when replay validation or cancellation fails.
            self.check()
            validate_replay(self.root, self.request)
        finally:
            self.close()
        # ADB's exit status alone does not establish whether the MP4 is usable.
        validate_recording(self.c, self.root, self.request, self.cancelled)

    def close(self):
        if live(self.owner):
            try:
                self.cleanup_guest_storage("shutdown")
            except Exception:
                logging.exception("Guest recording cleanup deferred until next boot")
            try:
                self.adb("emu", "kill", timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if self.emulator:
                try:
                    self.emulator.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    if live(self.owner):
                        self.emulator.terminate()
                        self.emulator.wait(timeout=10)
        write_json(
            self.root / "local-shutdown.json",
            dict(stopped=not live(self.owner), time=time.time()),
        )


def recover_local(config, root):
    """Only reclaim resources bearing the interrupted job's PID birth identity."""
    exporter = root / "export-owner.json"
    if exporter.exists():
        owned = json.loads(exporter.read_text())
        if live(owned):
            os.kill(owned["pid"], 15)
            deadline = time.monotonic() + 15
            while live(owned) and time.monotonic() < deadline:
                time.sleep(0.5)
            if live(owned):
                os.kill(owned["pid"], 9)
    p = root / "emulator-owner.json"
    if not p.exists():
        return
    owner = json.loads(p.read_text())
    if not live(owner):
        return
    recording = root / "recording.json"
    if recording.exists():
        r = json.loads(recording.read_text())
        deadline = r["start_time"] + r["limit"] + 30
        # The host adb client can disappear before the guest recorder finishes.
        # Preserve the guest's original natural deadline even in that case.
        while time.time() < deadline and (
            live(r) or time.time() < r["start_time"] + r["limit"]
        ):
            time.sleep(1)
    if live(owner):
        renderer = Renderer(config, root, {}, lambda *a, **kw: None, lambda: False)
        renderer.owner = owner
        try:
            renderer.cleanup_guest_storage("recovery")
        except Exception:
            logging.exception("Interrupted recording cleanup deferred until next boot")
        subprocess.run(
            [config.adb, "-s", owner["serial"], "emu", "kill"],
            capture_output=True,
            timeout=30,
        )
        deadline = time.monotonic() + 30
        while live(owner) and time.monotonic() < deadline:
            time.sleep(0.5)
        if live(owner):
            raise RuntimeError("遗留模拟器仍在退出，请稍后重试")


def run_export_process(root, args, cancelled, log_name, timeout):
    """Track cancellable transcoding/verification children independently of recording."""
    with (root / log_name).open("wb") as log:
        p = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
        )
        write_json(root / "export-owner.json", dict(pid=p.pid, birth=birth(p.pid)))
        deadline = time.monotonic() + timeout
        try:
            while p.poll() is None:
                if cancelled():
                    raise Cancelled("任务已取消")
                if time.monotonic() > deadline:
                    raise TimeoutError("视频处理超时：" + log_name)
                time.sleep(0.5)
            if p.returncode:
                raise RuntimeError("视频处理失败，详情见 " + log_name)
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


def validate_recording(config, root, request, cancelled):
    _, render = validate_replay(root, request)
    if cancelled():
        raise Cancelled("任务已取消")
    raw = json.loads(subprocess.check_output(
        [config.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration,size:stream=codec_name,width,height",
         "-of", "json", str(root / "native-full.mp4")], timeout=30,
    ))
    streams = raw.get("streams", [])
    duration = float(raw["format"]["duration"])
    ending = 1 + float(render["ending_wall_s"])
    if (not streams or streams[0].get("width", 0) <= 0
            or streams[0].get("height", 0) <= 0
            or not math.isfinite(duration) or not math.isfinite(ending)
            or ending <= 0 or duration <= ending + 2):
        raise ValueError("录像未覆盖完整终局或视频流无效")
    # Normalize VFR timestamps for the null muxer while decoding every input frame.
    run_export_process(
        root,
        [config.ffmpeg, "-v", "error", "-xerror", "-err_detect", "explode",
         "-i", str(root / "native-full.mp4"), "-map", "0:v:0",
         "-vf", "fps=30", "-f", "null", "-"],
        cancelled, "raw-decode.log", 180,
    )
    exit_path = root / "recording-exit.json"
    exit_record = json.loads(exit_path.read_text()) if exit_path.exists() else None
    result = dict(duration=duration, ending_wall_s=render["ending_wall_s"],
                  full_frame_decode="passed", replay_verified=True,
                  recorder_exit=exit_record)
    write_json(root / "recording-validation.json", result)
    return result


def export(config, root, request, progress, cancelled):
    stats, render = validate_replay(root, request)

    def probe(path):
        return json.loads(
            subprocess.check_output(
                [
                    config.ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,size:stream=codec_name,width,height,avg_frame_rate",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )

    raw = probe(root / "native-full.mp4")
    duration = float(raw["format"]["duration"])
    ending = 1 + render["ending_wall_s"]
    speed = render["speed"]
    trim = min(duration, ending + 4)
    if ending + 2 >= duration:
        raise ValueError("录像未覆盖完整终局")
    filters = f"trim=duration={trim:.3f},setpts={speed}*PTS,fps=30"
    partial = root / "video.partial.mp4"
    progress("exporting", message="导出正常游戏速度的视频")
    run_export_process(
        root,
        [
            config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(root / "native-full.mp4"),
            "-vf",
            filters,
            "-map",
            "0:v:0",
            "-an",
            "-t",
            str(trim * speed),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(partial),
        ],
        cancelled,
        "export.log",
        600,
    )
    progress("validating", message="核验全帧解码和完整终局")
    info = probe(partial)
    if float(info["format"]["duration"]) < ending * speed + 2:
        raise ValueError("导出视频缺少终局")
    run_export_process(
        root,
        [
            config.ffmpeg,
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(partial),
            "-f",
            "null",
            "-",
        ],
        cancelled,
        "decode.log",
        180,
    )
    for name, second in [
        ("preview-start.jpg", min(35, ending * speed / 4)),
        ("preview-mid.jpg", ending * speed / 2),
        ("preview-end.jpg", ending * speed + 2),
    ]:
        run_export_process(
            root,
            [
                config.ffmpeg,
                "-v",
                "error",
                "-y",
                "-ss",
                str(second),
                "-i",
                str(partial),
                "-frames:v",
                "1",
                "-vf",
                "scale=540:-1",
                str(root / name),
            ],
            cancelled,
            name + ".log",
            30,
        )
    if cancelled():
        raise Cancelled("任务已取消")
    partial.replace(root / "video.mp4")
    metadata = dict(
        seed=request["seed"],
        checkpoints=[request[x]["sha256"] for x in ("top", "bottom")],
        iterations=stats["iterations"],
        seed_selection="fixed before generation; no result-based selection",
        decks=[request["deck_top"], request["deck_bottom"]],
        deck_cards=stats.get("deck_cards"),
        scheduler_version="event-scheduling.v2",
        action_constraints=stats.get("action_constraints"),
        runtime_fingerprint=stats.get("runtime_fingerprint"),
        winner=stats["terminal"]["winner"],
        generation_terminal=stats["terminal"],
        render_terminal=render["final_observation"],
        render_status_tick=render["final_status"]["tick"],
        terminal_tick_difference=render["final_observation"]["tick"]
        - stats["terminal"]["terminal_tick"],
        per_owner=render["per_owner"],
        actions=stats["counters"]["executed"],
        opening_deal_verified=True,
        native_receipts_verified=True,
        full_frame_decode="passed",
        visual_inspection="not performed automatically",
        duration=float(info["format"]["duration"]),
        bytes=int(info["format"]["size"]),
        sha256=hashlib.sha256((root / "video.mp4").read_bytes()).hexdigest(),
        probe=info,
    )
    recording_validation = root / "recording-validation.json"
    if recording_validation.exists():
        metadata["recording_validation"] = json.loads(recording_validation.read_text())
    write_json(root / "metadata.json", metadata)
    return metadata
