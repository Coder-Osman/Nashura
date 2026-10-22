"""Device preview and navigation through visually verified controls."""

import io
from pathlib import Path
import subprocess
import sys
import threading
import time

from PIL import Image, ImageChops, ImageStat

from native_runner.live.capture import DEFAULT_ADB


class Device:
    serial = "emulator-5570"
    human_serial = "emulator-5572"
    human_avd = "firstlight-human"
    frida_server = "/data/local/tmp/cr-live-frida-server"
    frida_log = "/data/local/tmp/cr-live-frida-server.log"
    game_package = "nullsroyale.rel.free"
    game_component = "nullsroyale.rel.free/com.supercell.clashroyale.GameApp"
    game_stability_seconds = 5
    regions = {
        "lobby": ((385, 1430, 706, 1550), (540, 1489)),
        "result": ((410, 1700, 672, 1790), (540, 1745)),
        "battle_log": ((980, 192, 1042, 259), (1011, 225)),
    }

    def __init__(self):
        self.lock = threading.Lock()
        self.updated = 0
        self.png = None
        self.jpeg = None
        self.scene = "unknown"
        self.error = None
        self.stable_game_pid = None
        folder = Path(__file__).with_name("templates")
        self.templates = {name: Image.open(folder / (name + ".png")).convert("RGB")
                          for name in self.regions}

    def adb(self, *args: str, timeout: int = 12) -> str:
        return subprocess.check_output([str(DEFAULT_ADB), "-s", self.serial, *args],
                                       timeout=timeout, stderr=subprocess.STDOUT, text=True).strip()

    def adb_on(self, serial: str, *args: str, timeout: int = 12) -> str:
        return subprocess.check_output([str(DEFAULT_ADB), "-s", serial, *args],
                                       timeout=timeout, stderr=subprocess.STDOUT, text=True).strip()

    @staticmethod
    def _adb_devices() -> dict[str, str]:
        result = subprocess.run([str(DEFAULT_ADB), "devices"], capture_output=True,
                                text=True, timeout=10, check=True)
        devices = {}
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                devices[fields[0]] = fields[1]
        return devices

    def ensure_human_running(self, stop: threading.Event, *, timeout: float = 180):
        """Start or reuse only the dedicated HumanPilot emulator.

        The live AI emulator and borrowed render emulators have different AVD
        names and serials.  A missing game package is reported instead of
        installing an APK into the user's account snapshot.
        """
        if stop.is_set():
            raise InterruptedError("已停止准备人类模拟器")
        emulator = DEFAULT_ADB.parent.parent / "emulator/emulator"
        log_path = Path(__file__).resolve().parents[1] / "artifacts/live-web/human-emulator.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def connected():
            try:
                return self._adb_devices().get(self.human_serial) == "device"
            except (OSError, subprocess.SubprocessError):
                return False

        if not connected():
            processes = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                       text=True, timeout=10, check=True).stdout
            owns_avd = any(f"-avd {self.human_avd}" in line for line in processes.splitlines()
                           if "qemu-system" in line or "/emulator" in line)
            if not owns_avd:
                with log_path.open("ab") as log:
                    subprocess.Popen(
                        [str(emulator), "-avd", self.human_avd, "-port", "5572",
                         "-cores", "4", "-memory", "4096", "-gpu", "host",
                         "-no-snapshot-load", "-no-snapshot-save", "-no-boot-anim"],
                        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop.is_set():
                raise InterruptedError("已停止等待人类模拟器")
            if connected():
                break
            stop.wait(.5)
        else:
            raise TimeoutError(f"{self.human_avd} 未在 {timeout:g} 秒内连接 ADB")

        boot_deadline = time.monotonic() + timeout
        while time.monotonic() < boot_deadline:
            if stop.is_set():
                raise InterruptedError("已停止等待人类模拟器启动")
            try:
                if self.adb_on(self.human_serial, "shell", "getprop", "sys.boot_completed",
                               timeout=5) == "1":
                    break
            except subprocess.SubprocessError:
                pass
            stop.wait(1)
        else:
            raise TimeoutError(f"{self.human_avd} Android 启动超时")

        try:
            package_path = self.adb_on(self.human_serial, "shell", "pm", "path", self.game_package,
                                       timeout=8)
        except subprocess.CalledProcessError as exc:
            package_path = str(exc.output or "")
        if not package_path.startswith("package:"):
            raise RuntimeError(
                f"{self.human_avd} 未安装 {self.game_package}，请恢复已安装游戏的 AVD 快照；不会自动安装 APK"
            )
        self.adb_on(self.human_serial, "shell", "am", "start", "-n", self.game_component,
                    timeout=15)

    def classify(self, picture: Image.Image) -> str:
        if picture.size != (1080, 1920):
            raise ValueError("Live control requires the calibrated 1080 x 1920 viewport")
        for name, (box, _) in self.regions.items():
            crop = picture.crop(box).convert("RGB")
            difference = ImageStat.Stat(ImageChops.difference(crop, self.templates[name]))
            if sum(difference.mean) / 3 < 13:
                return name
        return "unknown"

    def screen(self, *, force=False) -> bytes:
        with self.lock:
            if not force and self.jpeg and time.monotonic() - self.updated < .8:
                return self.jpeg
            try:
                png = subprocess.check_output([str(DEFAULT_ADB), "-s", self.serial,
                                               "exec-out", "screencap", "-p"],
                                              timeout=8, stderr=subprocess.PIPE)
                picture = Image.open(io.BytesIO(png))
                self.scene = self.classify(picture)
                self.png = png
                picture.thumbnail((540, 960))
                encoded = io.BytesIO()
                picture.convert("RGB").save(encoded, "JPEG", quality=82)
                self.jpeg = encoded.getvalue()
                self.error = None
                self.updated = time.monotonic()
                return self.jpeg
            except Exception as exc:
                self.error = str(exc)[:240]
                raise

    def tap(self, x: int, y: int):
        self.adb("shell", "input", "tap", str(x), str(y))

    def ensure_adb_root(self, stop: threading.Event):
        """Prepare privileged reads before checking the game's native runtime."""
        if stop.is_set():
            raise InterruptedError("已停止准备 ADB 观测权限")
        if self.adb("shell", "id", "-u", timeout=4) == "0":
            return
        detail = self.adb("root")
        self.adb("wait-for-device", timeout=25)
        if stop.is_set():
            raise InterruptedError("已停止准备 ADB 观测权限")
        if self.adb("shell", "id", "-u", timeout=4) != "0":
            raise RuntimeError("ADB 尚未取得读取游戏进程所需的 root 权限：" + detail)

    def frida_ready(self, port: str) -> tuple[bool, str]:
        # The unified web server may use a Python without Frida installed.
        python = Path(__file__).resolve().parents[1] / ".venv-live/bin/python"
        try:
            result = subprocess.run(
                [str(python) if python.exists() else sys.executable, "-c",
                 "import frida,sys; frida.get_device_manager().add_remote_device("
                 "'127.0.0.1:'+sys.argv[1]).enumerate_processes()", port],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            return False, "连接检测超时"
        detail = result.stderr.strip().splitlines()
        return result.returncode == 0, detail[-1][-240:] if detail else "服务尚未响应"

    def ensure_frida(self, stop: threading.Event, *, timeout: float = 45):
        def check_stop():
            if stop.is_set():
                raise InterruptedError("已停止连接观测服务")

        def running():
            try:
                return bool(self.adb("shell", "pidof", "cr-live-frida-server", timeout=4))
            except subprocess.CalledProcessError:
                return False

        def diagnostics():
            try:
                return self.adb("shell", "tail", "-n", "8", self.frida_log, timeout=4)[-800:]
            except subprocess.SubprocessError:
                return ""

        check_stop()
        self.ensure_adb_root(stop)
        if not running():
            check_stop()
            # -D waits for daemon initialization, which can exceed ADB's timeout.
            # Detach the foreground server from ADB, then probe its actual API.
            self.adb("shell", f"nohup {self.frida_server} -l 127.0.0.1:27042 "
                     f"</dev/null >{self.frida_log} 2>&1 &", timeout=4)
        check_stop()
        port = self.adb("forward", "tcp:0", "tcp:27042", timeout=4)
        try:
            deadline = time.monotonic() + timeout
            detail = "服务尚未响应"
            while time.monotonic() < deadline:
                check_stop()
                ready, detail = self.frida_ready(port)
                check_stop()
                if ready:
                    return
                if not running():
                    raise RuntimeError("观测服务启动后退出：" + (diagnostics() or detail))
                stop.wait(.5)
            raise TimeoutError(f"观测服务在 {timeout:g} 秒内未就绪：" + (diagnostics() or detail))
        finally:
            # Only remove this probe's ephemeral forward; capture owns its own.
            try:
                self.adb("forward", "--remove", f"tcp:{port}", timeout=4)
            except subprocess.SubprocessError:
                pass

    def ensure_running(self, stop: threading.Event, *, frida=True):
        try:
            connected = self.adb("get-state") == "device"
        except subprocess.SubprocessError:
            connected = False
        if not connected:
            devices = self._adb_devices()
            if devices.get(self.serial) != "device":
                emulator = DEFAULT_ADB.parent.parent / "emulator/emulator"
                log_path = Path(__file__).resolve().parents[1] / "artifacts/live-web/emulator.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                processes = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                           text=True, timeout=10, check=True).stdout
                owns_avd = any(
                    "-avd firstlight-live" in line and "-port 5570" in line
                    for line in processes.splitlines()
                    if "qemu-system" in line or "/emulator" in line
                )
                if not owns_avd:
                    with log_path.open("ab") as log:
                        subprocess.Popen([str(emulator), "-avd", "firstlight-live", "-port", "5570",
                                          "-cores", "4", "-memory", "4096", "-gpu", "host",
                                          "-no-snapshot-load", "-no-snapshot-save", "-no-boot-anim"],
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # ADB may report `device` before Android has finished booting.  Keep
        # the same readiness gate for cold starts, offline recovery, and an
        # already-running emulator so native game startup cannot race boot.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if stop.wait(1):
                raise InterruptedError("Stopped while starting the emulator")
            try:
                if self.adb("get-state", timeout=4) == "device" and self.adb(
                        "shell", "getprop", "sys.boot_completed", timeout=4) == "1":
                    break
            except subprocess.SubprocessError:
                continue
        else:
            raise TimeoutError("The firstlight-live emulator did not finish starting")
        if stop.is_set():
            raise InterruptedError("Stopped before connecting to the game")
        if frida:
            self.ensure_frida(stop)

    def game_observation(self) -> int | None:
        """Return the game PID only while its foreground native runtime is ready."""
        try:
            pids = self.adb("shell", "pidof", self.game_package, timeout=4).split()
            if len(pids) != 1:
                return None
            pid = int(pids[0])
            activities = self.adb("shell", "dumpsys", "activity", "activities", timeout=4)
            if not any("mResumedActivity:" in line and self.game_component in line
                       for line in activities.splitlines()):
                return None
            self.adb("shell", "grep", "-Fq", "/libg.so", f"/proc/{pid}/maps", timeout=4)
            return pid
        except subprocess.CalledProcessError as exc:
            detail = str(exc.output or "").strip()
            if "Permission denied" in detail or "Operation not permitted" in detail:
                raise RuntimeError("游戏状态读取权限不足：" + detail[-400:]) from exc
            return None
        except (subprocess.SubprocessError, ValueError):
            return None

    def wait_for_game_stable(self, stop: threading.Event, *, settle_seconds=None, timeout=75) -> int:
        """Require a continuous healthy game process before a Frida attachment."""
        # /proc/<pid>/maps is privileged even when preview and pidof succeed.
        # Do this before the stability loop; Frida is deliberately started later.
        self.ensure_adb_root(stop)
        settle_seconds = self.game_stability_seconds if settle_seconds is None else settle_seconds
        deadline = time.monotonic() + timeout
        candidate = None
        candidate_since = None
        while time.monotonic() < deadline:
            if stop.is_set():
                raise InterruptedError("已停止等待游戏稳定")
            observed = self.game_observation()
            if observed is not None and observed == self.stable_game_pid:
                return observed
            if observed != candidate:
                candidate = observed
                candidate_since = time.monotonic() if observed is not None else None
            elif candidate_since is not None and time.monotonic() - candidate_since >= settle_seconds:
                self.stable_game_pid = observed
                return observed
            if stop.wait(.5):
                raise InterruptedError("已停止等待游戏稳定")
        raise TimeoutError(f"游戏进程在 {timeout:g} 秒内未稳定，请检查启动状态")

    def prepare_friendly(self, stop: threading.Event):
        """Attach to the AI client without dismissing its invitation screens."""
        self.ensure_human_running(stop)
        self.ensure_running(stop, frida=False)
        if stop.is_set():
            raise InterruptedError("已停止准备友谊战")
        self.adb("shell", "am", "start", "-n", self.game_component)
        self.wait_for_game_stable(stop)
        self.ensure_frida(stop)
        self.screen(force=True)

    def home(self, stop: threading.Event):
        self.ensure_running(stop, frida=False)
        self.adb("shell", "am", "start", "-n", self.game_component)
        self.wait_for_game_stable(stop)
        self.ensure_frida(stop)
        for _ in range(14):
            if stop.wait(.5):
                raise InterruptedError("Stopped before matchmaking")
            self.screen(force=True)
            scene = self.scene
            if scene == "lobby":
                return
            if scene in {"result", "battle_log"}:
                if stop.is_set():
                    raise InterruptedError("Stopped before navigation")
                self.tap(*self.regions[scene][1])
                stop.wait(1.5)
        raise ValueError("Please return the game to the Battle lobby, then start again")

    def match(self, stop: threading.Event):
        self.screen(force=True)
        if stop.is_set():
            raise InterruptedError("Stopped before matchmaking")
        if self.scene != "lobby":
            raise ValueError("The game left the Battle lobby; matching was not started")
        self.tap(*self.regions["lobby"][1])
