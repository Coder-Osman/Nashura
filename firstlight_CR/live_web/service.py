"""One owned real-time state reader and policy process pair per requested match."""

import copy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from native_runner.live.control import device_lease
from native_runner.live.snapshot import latest_snapshot, LiveStateError
from .checkpoints import Checkpoints
from .device import Device

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "artifacts/live-null-20260920"
TERMINAL = {"completed", "failed", "stopped", "interrupted"}
BATTLE_CAPTURE_SECONDS = 1800
BATTLE_POLICY_SECONDS = 1800
BATTLE_WAIT_SECONDS = 900


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


class JsonlTail:
    def __init__(self, path):
        self.path, self.offset = path, 0

    def read(self):
        if not self.path.exists():
            return []
        with self.path.open("rb") as source:
            source.seek(self.offset)
            data = source.read()
        end = data.rfind(b"\n") + 1
        self.offset += end
        return [json.loads(line) for line in data[:end].splitlines() if line]


class Service:
    def __init__(self, data=None, device=None, catalog=None, *, match_mode="standard"):
        self.data = data or ROOT / "artifacts/live-web"
        (self.data / "jobs").mkdir(parents=True, exist_ok=True)
        self.device = device or Device()
        self.catalog = catalog or Checkpoints(ROOT, self.data)
        if match_mode not in {"standard", "friendly"}:
            raise ValueError("未知对局模式")
        self.match_mode = match_mode
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.current = None
        self.instance_lock = None

    def open(self):
        self.instance_lock = (self.data / "service.lock").open("a+")
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.instance_lock.close()
            raise RuntimeError("A live dashboard is already running") from exc
        for path in (self.data / "jobs").glob("*/summary.json"):
            row = json.loads(path.read_text())
            if row["state"] not in TERMINAL:
                row.update(state="interrupted", message="服务曾中断，请检查游戏状态后重新开始", ended_at=time.time())
                save(path, row)
        self.current = next(iter(self.history()), None)
        self.catalog.start()

    def history(self):
        with self.lock:
            rows = [{**json.loads(p.read_text()), "has_result_image": (p.parent / "result.png").is_file()}
                    for p in (self.data / "jobs").glob("*/summary.json")]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:50]

    def status(self):
        with self.lock:
            job = copy.deepcopy(self.current)
            busy = bool(self.worker and self.worker.is_alive())
        return dict(job=job, busy=busy, match_mode=self.match_mode, device=dict(serial=self.device.serial,
                    online=self.device.error is None and self.device.updated > 0
                           and time.monotonic() - self.device.updated < 15,
                    scene=self.device.scene, error=self.device.error))

    def start(self, checkpoint_id, *, match_mode=None):
        mode = match_mode or self.match_mode
        if mode not in {"standard", "friendly"}:
            raise ValueError("未知对局模式")
        checkpoint = self.catalog.get(checkpoint_id)
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("当前对局仍在运行")
            # The runner holds the same lease for its complete lifetime.
            with device_lease(self.device.serial):
                pass
            self.stop_event.clear()
            identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
            directory = self.data / "jobs" / identity
            directory.mkdir()
            self.current = dict(id=identity, checkpoint=checkpoint, mode=mode,
                                state="preparing",
                                message="正在准备游戏", created_at=time.time(), events=[],
                                purpose="interactive_test" if mode == "friendly" else "live_match",
                                training_export=False,
                                counts=dict(decisions=0, submitted=0, confirmed=0, cancelled=0),
                                tick=0, telemetry=None, result=None, warning=None)
            self.persist()
            self.worker = threading.Thread(target=self.work, args=(directory, checkpoint), daemon=True)
            self.worker.start()
            return copy.deepcopy(self.current)

    def persist(self):
        save(self.data / "jobs" / self.current["id"] / "summary.json", self.current)

    def update(self, **values):
        with self.lock:
            self.current.update(values)
            self.persist()

    def stop(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                self.stop_event.set()
                self.update(state="stopping", message="正在停止 AI 接管，游戏对局保留")
        return self.status()

    def check_stop(self):
        if self.stop_event.is_set():
            raise InterruptedError("AI 接管已停止，游戏对局保留")

    def spawn(self, module, arguments, log):
        self.check_stop()
        with log.open("wb") as output:
            python = ROOT / ".venv-live/bin/python"
            return subprocess.Popen([str(python) if python.exists() else sys.executable, "-m", module, *map(str, arguments)],
                                    cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                    start_new_session=True)

    @staticmethod
    def shutdown(process):
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except ProcessLookupError:
                process.wait(timeout=5)

    def ready(self, process, path, timeout=60):
        deadline = time.monotonic() + timeout
        tail = JsonlTail(path)
        while time.monotonic() < deadline:
            self.check_stop()
            for row in tail.read():
                if row.get("event") == "ready":
                    return
            if process.poll() is not None:
                log = path.with_suffix(".log")
                detail = log.read_text(errors="replace")[-1400:] if log.exists() else ""
                raise ValueError("启动失败：" + detail)
            self.stop_event.wait(.15)
        raise TimeoutError("准备超时，请检查设备连接和权重")

    def policy_event(self, event):
        kind = event["event"]
        with self.lock:
            event = {**event, "at": time.time()}
            self.current["events"] = (self.current["events"] + [event])[-100:]
            if "tick" in event:
                self.current["tick"] = event["tick"]
            if kind == "bound":
                self.current.update(state="playing", message="AI 对战中", warning=None)
            elif kind == "decision":
                self.current["counts"]["decisions"] += 1
                self.current["warning"] = None
                self.current["inference_ms"] = event["inference_ms"]
            elif kind == "confirmed":
                self.current["counts"]["confirmed"] += 1
                self.current["counts"]["submitted"] += 1
            elif kind == "cancelled":
                self.current["counts"]["cancelled"] += 1
            elif kind == "blocked" and self.current["state"] == "playing":
                self.current["warning"] = event["reason"]
            if "counts" in event:
                self.current["counts"] = event["counts"]
            self.persist()

    @staticmethod
    def _telemetry(row, *, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        age = (now_ns - row["received_monotonic_ns"]) / 1e9
        return dict(tick=row["tick"], elixir=row["own"]["elixir_raw"] / 10000,
                    hand=[s["card"]["card_id"] if s["card"] else None
                          for s in row["own"]["hand"]], age_ms=round(age * 1000))

    def work(self, directory, checkpoint):
        state_reader = policy = None
        mode = self.current["mode"]
        final = dict(state="failed", message="任务意外结束")
        try:
            def progress(received, total):
                self.check_stop()
                self.update(state="downloading", message=f"正在下载 {checkpoint['label']} 权重",
                            download=dict(received=received, total=total))

            if checkpoint.get("source") == "remote":
                self.update(state="downloading", message="正在准备远端权重")
            checkpoint = self.catalog.prepare(checkpoint, stop=self.stop_event, progress=progress)
            self.check_stop()
            self.update(state="preparing", message="正在准备游戏", checkpoint=checkpoint, download=None)
            if mode == "friendly":
                self.device.prepare_friendly(self.stop_event)
            else:
                self.device.home(self.stop_event)
            self.check_stop()
            self.update(message="正在连接实时游戏状态")
            capture_path, policy_path = directory / "capture.jsonl", directory / "policy.jsonl"
            state_reader = self.spawn("native_runner.live.capture", [
                "--serial", self.device.serial, "--seconds", BATTLE_CAPTURE_SECONDS, "--libg", ASSETS / "libg.so",
                "--output", capture_path], directory / "capture.log")
            self.ready(state_reader, capture_path)
            self.update(message="正在加载所选权重")
            policy = self.spawn("native_runner.live.run", [
                "--capture", capture_path, "--output", policy_path,
                "--checkpoint", checkpoint["path"], "--serial", self.device.serial,
                "--calibration", ASSETS / "calibration-01.jsonl", ASSETS / "calibration-02.jsonl",
                "--seconds", BATTLE_POLICY_SECONDS, "--execute", "--allow-partial", "--public-tower-identities"],
                directory / "policy.log")
            self.ready(policy, policy_path)
            self.check_stop()
            if state_reader.poll() is not None or policy.poll() is not None:
                raise ValueError("实时状态或 AI 推理进程已退出，请重新开始")
            if mode == "standard":
                self.device.match(self.stop_event)
                waiting_message = "正在匹配对手"
            else:
                waiting_message = "AI 已就绪。请在 Nashura 的 Social → Friendly Battle → 1v1 发起挑战，在 HumanPilot 的同一部落聊天里接受，并直接在 HumanPilot 模拟器出牌。"
            self.update(state="waiting_battle", message=waiting_message)
            started = time.monotonic()
            blocked_since = None
            tail = JsonlTail(policy_path)
            last_snapshot = None
            ended = False
            while not ended:
                self.check_stop()
                for event in tail.read():
                    self.policy_event(event)
                    kind = event["event"]
                    if kind == "decision":
                        blocked_since = None
                    elif kind == "blocked" and self.current["state"] == "playing":
                        blocked_since = blocked_since or time.monotonic()
                    elif kind == "terminal":
                        if not event.get("same_battle"):
                            raise ValueError("游戏已切换到另一场对局")
                        winner = event.get("winner")
                        result = "draw" if winner == -1 else (
                            "win" if winner == event["own_side"] else "loss" if winner in (0, 1) else "unknown")
                        final = dict(state="completed", message="本局已结束", result=result)
                        ended = True
                    elif kind in {"failed", "timeout", "interrupted"}:
                        raise ValueError(event.get("reason", "推理已停止或超时"))
                if ended:
                    break
                if policy.poll() is not None:
                    # Drain once more after exit to include its final flushed event.
                    if tail.offset < policy_path.stat().st_size:
                        continue
                    raise ValueError("推理进程退出，详见运行日志")
                if state_reader.poll() is not None:
                    raise ValueError("实时游戏状态读取已中断")
                if self.current["state"] == "waiting_battle" and time.monotonic() - started > BATTLE_WAIT_SECONDS:
                    raise TimeoutError(f"等待对局超过 {BATTLE_WAIT_SECONDS} 秒，AI 接管已停止")
                if blocked_since and time.monotonic() - blocked_since > 15:
                    raise ValueError("推理持续受阻：" + str(self.current.get("warning")))
                try:
                    row = latest_snapshot(capture_path)
                    age = (time.monotonic_ns() - row["received_monotonic_ns"]) / 1e9
                    if age > 5 and self.current["state"] == "playing":
                        raise TimeoutError("观测超过 5 秒未更新")
                    last_snapshot = row
                    with self.lock:
                        self.current["telemetry"] = self._telemetry(row)
                except LiveStateError:
                    # Keep the last complete hand visible while the capture writer
                    # is flushing its next line or briefly reports a read error.
                    if last_snapshot is not None:
                        with self.lock:
                            self.current["telemetry"] = self._telemetry(last_snapshot)
                self.stop_event.wait(.2)
            if not self.stop_event.wait(3):
                try:
                    self.update(state="finishing", message="本局已结束，正在保存结算画面")
                    for _ in range(10):
                        self.device.screen(force=True)
                        if self.device.scene == "result":
                            (directory / "result.png").write_bytes(self.device.png)
                            break
                        if self.stop_event.wait(1):
                            break
                except Exception:
                    pass
        except InterruptedError as exc:
            final = dict(state="stopped", message=str(exc))
        except Exception as exc:
            final = dict(state="failed", message=str(exc)[-1800:])
        finally:
            try:
                self.shutdown(policy)
            finally:
                try:
                    self.shutdown(state_reader)
                finally:
                    self.update(**final, ended_at=time.time())

    def close(self):
        self.stop()
        self.catalog.close()
        if self.worker:
            self.worker.join(timeout=35)
        if self.instance_lock:
            self.instance_lock.close()
