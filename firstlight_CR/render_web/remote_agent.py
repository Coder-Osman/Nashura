"""Standalone remote helper. Commands and paths come from the local service only."""

import argparse
import concurrent.futures
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import traceback

if __package__:
    from .runtime_constraints import runtime_overlay, delay_constraints_contract
    from .checkpoint_sources import (
        FORMAL1553_SHA, SETTLEMENT_RUN, SETTLEMENT_ROOT, SETTLEMENT_CATEGORY,
        SETTLEMENT_LABEL, CONTINUATION_RUN, CONTINUATION_ROOT,
        CONTINUATION_CATEGORY, CONTINUATION_LABEL, CONTINUATION_START_SHA,
        checkpoint_location,
    )
else:
    from runtime_constraints import runtime_overlay, delay_constraints_contract
    from checkpoint_sources import (
        FORMAL1553_SHA, SETTLEMENT_RUN, SETTLEMENT_ROOT, SETTLEMENT_CATEGORY,
        SETTLEMENT_LABEL, CONTINUATION_RUN, CONTINUATION_ROOT,
        CONTINUATION_CATEGORY, CONTINUATION_LABEL, CONTINUATION_START_SHA,
        checkpoint_location,
    )


def write(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.replace(path)


def birth(pid):
    return subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()


def live(owner):
    return bool(owner and owner.get("birth") and birth(owner["pid"]) == owner["birth"])


def runtime(cfg, action_constraints=None):
    source = Path(cfg["archive_root"]) / cfg["source_run"]
    manifest = json.loads((source / "collector-scheduler-manifest.json").read_text())
    frozen = (source / "collector-scheduler-code").resolve()
    overlay = {}
    if action_constraints is not None:
        if action_constraints != delay_constraints_contract():
            raise ValueError("unsupported runtime action constraints")
        for name, digest in manifest.items():
            if hashlib.sha256((frozen / name).read_bytes()).hexdigest() != digest:
                raise ValueError("source runtime manifest mismatch: " + name)
        overlay = runtime_overlay(frozen)
    identity = dict(manifest=manifest, freeze_format=2)
    if overlay:
        identity.update(freeze_format=3, action_constraints=action_constraints,
                        overlay={name: hashlib.sha256(data).hexdigest() for name, data in overlay.items()})
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    manifest = {**manifest, **identity.get("overlay", {})}
    root = Path(cfg["remote_root"]) / "runtimes" / fingerprint
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / "prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not root.exists():
            stage = root.with_name(root.name + ".staging")
            if stage.exists():
                shutil.rmtree(stage)
            shutil.copytree(
                frozen,
                stage,
                symlinks=True,
                copy_function=shutil.copy2,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
            )
            for name, data in overlay.items():
                target = stage / name
                if any(parent.is_symlink() for parent in target.parents if parent.is_relative_to(stage)):
                    raise ValueError("runtime overlay requires frozen parent directories")
                temporary = target.with_suffix(target.suffix + ".overlay")
                temporary.write_bytes(data)
                temporary.replace(target)
            if overlay:
                write(stage / "runtime-identity.json", identity)
            stage.rename(root)
        for name, digest in manifest.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise ValueError("runtime manifest mismatch: " + name)
        helper = root / "native_runner/training/event_scheduling.py"
        if "event-scheduling.v2" not in helper.read_text():
            raise ValueError("scheduler v2 required")
    return root, manifest


SCAN = r"""
from pathlib import Path
import json,sys
base=Path(sys.argv[1]);entries=[]
roots=sorted(set(list(base.glob('event-*'))+list(base.glob('event-*/*'))))
for r in roots:
 if not (r/'fleet.json').is_file():continue
 try:cfg=json.loads((r/'fleet.json').read_text())
 except (ValueError,OSError):continue
 initial=cfg.get('initial_checkpoint_sha256')
 if initial:
  entries.append(dict(run=str(r.relative_to(base)),sha256=initial,iteration=cfg.get('start_iteration',0),initial=True,source=cfg.get('source_checkpoint','')))
 for p in sorted(r.glob('iteration-*/success.json')):
  try:x=json.loads(p.read_text());sha=Path(x['checkpoint']).stem
  except (ValueError,KeyError,OSError):continue
  entries.append(dict(run=str(r.relative_to(base)),sha256=sha,iteration=x.get('iteration',int(p.parent.name.split('-')[-1])),initial=False,source=x.get('archive_checkpoint','')))
print(json.dumps(entries))
"""


def experiment_entries(archive):
    """Expose only completed, verified updates from the selected trial."""
    run = "event-mixed-deck-100r-20260920/tower-beta1-formal1553-200r-20260922"
    root = Path(archive) / run
    if not (root / "protocol.json").is_file():
        return []
    protocol = json.loads((root / "protocol.json").read_text())
    if (protocol.get("run_id") != root.name or protocol.get("start_iteration") != 1553
            or protocol.get("reward_config", {}).get("version") != "elixir-overflow-tower.v4"):
        raise ValueError("塔血实验协议与目录不匹配")
    return completed_trial_entries(root, protocol, run=run,
        category="tower-beta1-formal1553", category_label="塔血实验 β=1 · 1553 起点",
        label_prefix="塔血 β=1")


def completed_trial_entries(root, protocol, *, run, category, category_label, label_prefix,
                            require_optimizer_audit=False):
    entries = []
    parent = protocol["start_sha256"]
    updates = protocol.get("updates")
    number = 1
    while updates is None or number <= updates:
        directory = root / "tower" / f"round-{number:03d}"
        if not (directory / "success.json").is_file():
            break
        success = json.loads((directory / "success.json").read_text())
        verified = json.loads((directory / "update-verified.json").read_text())
        sha = success["checkpoint_sha256"]
        iteration = protocol["start_iteration"] + number
        if (success.get("run_id") != protocol["run_id"] or success.get("round") != number
                or success.get("iteration") != iteration
                or success.get("parent_checkpoint_sha256") != parent
                or success.get("reward_config") != protocol["reward_config"]
                or verified.get("checkpoint_sha256") != sha
                or not verified.get("preflight", {}).get("passed")
                or verified.get("verifier", {}).get("iterations") != iteration):
            raise ValueError("塔血实验完成记录未通过身份校验")
        if require_optimizer_audit and (
                verified.get("verifier", {}).get("checkpoint_sha256") != sha
                or verified.get("verifier", {}).get("optimizer_steps") != iteration
                or not verified.get("optimizer_resumed", {}).get("resumed")
                or verified.get("optimizer_resumed", {}).get("iterations") != iteration - 1
                or verified.get("advanced_parameters", 0) <= 0
                or verified.get("preflight", {}).get("reward_config") != protocol["reward_config"]):
            raise ValueError("续训优化器或奖励身份校验失败")
        entries.append(dict(run=run, sha256=sha, iteration=iteration, initial=False,
                            category=category, category_label=category_label,
                            label_prefix=label_prefix,
                            trial_round=number, trial_updates=updates))
        parent = sha
        number += 1
    return entries


def settlement_entries(root=None):
    """Read only the accepted training branch; benchmark weights stay separate."""
    root = Path(SETTLEMENT_ROOT if root is None else root)
    if not (root / "protocol.json").is_file():
        return []
    protocol = json.loads((root / "protocol.json").read_text())
    reward = protocol.get("reward_config", {})
    settlement = reward.get("settlement", {})
    config = json.loads((root / "experiment-config.json").read_text())
    if (protocol.get("run_id") != SETTLEMENT_RUN or protocol.get("start_iteration") != 1553
            or protocol.get("start_sha256") != FORMAL1553_SHA or protocol.get("updates") != 200
            or protocol.get("gamma") != .999 or protocol.get("gae_lambda") != .95
            or reward.get("version") != "elixir-settlement-nearquiet.v1"
            or reward.get("penalty_per_elixir") != .005 or reward.get("penalty_cap") is not None
            or settlement.get("version") != "settlement-nearquiet.v1"
            or settlement.get("beta") != 1 / 3
            or config.get("mode") != "training" or config.get("run_id") != SETTLEMENT_RUN):
        raise ValueError("近静场实验协议与训练分支不匹配")
    acceptance_path = root / "first-round-acceptance.json"
    if not acceptance_path.is_file():
        return []
    acceptance = json.loads(acceptance_path.read_text())
    if (not acceptance.get("passed") or acceptance.get("start_iteration") != 1553
            or acceptance.get("parent_checkpoint_sha256") != FORMAL1553_SHA
            or not acceptance.get("optimizer_resumed") or not acceptance.get("settlement_same_trace")):
        raise ValueError("近静场实验首轮验收未通过身份校验")
    entries = completed_trial_entries(root, protocol, run=SETTLEMENT_RUN,
        category=SETTLEMENT_CATEGORY, category_label=SETTLEMENT_LABEL,
        label_prefix="近静场结算 β=1/3", require_optimizer_audit=True)
    if entries and acceptance.get("checkpoint_sha256") != entries[0]["sha256"]:
        raise ValueError("近静场实验首轮权重未通过身份校验")
    return entries


def continuation_entries(root=None):
    """Index the verified v6 continuation of the 1753 tower branch."""
    root = Path(CONTINUATION_ROOT if root is None else root)
    if not (root / "protocol.json").is_file():
        return []
    protocol = json.loads((root / "protocol.json").read_text())
    reward = protocol.get("reward_config", {})
    tower = reward.get("tower_potential", {})
    reward_switch = protocol.get("reward_switch", {})
    previous_reward = reward_switch.get("from_config", {})
    ppo = protocol.get("ppo", {})
    if (protocol.get("run_id") != CONTINUATION_RUN
            or protocol.get("start_iteration") != 1753
            or protocol.get("start_sha256") != CONTINUATION_START_SHA
            or protocol.get("source_run") != "tower-beta1-formal1553-200r-20260922"
            or protocol.get("checkpoint") != str(root / "weights" / (CONTINUATION_START_SHA + ".pt"))
            or protocol.get("resume_optimizer") is not True
            or protocol.get("updates") is not None
            or reward_switch.get("iteration") != 1754
            or reward_switch.get("to_config") != reward
            or previous_reward.get("version") != "elixir-overflow-tower.v4"
            or previous_reward.get("tower_potential", {}).get("coefficient") != 1.0
            or ppo.get("gamma") != .999 or ppo.get("gae_lambda") != .95
            or reward.get("version") != "elixir-overflow-tower.v6"
            or reward.get("penalty_per_elixir") != .003
            or reward.get("penalty_cap") is not None
            or tower.get("version") != "visible-tower-hp.v1"
            or tower.get("coefficient") != .15
            or tower.get("gamma") != .999
            or tower.get("terminal") != "zero"):
        raise ValueError("1753 续训协议与分支不匹配")
    acceptance_path = root / "first-round-acceptance.json"
    if not acceptance_path.is_file():
        return []
    acceptance = json.loads(acceptance_path.read_text())
    if (acceptance.get("passed") is not True or acceptance.get("games") != 108
            or acceptance.get("start_iteration") != 1753
            or acceptance.get("iteration") != 1754
            or acceptance.get("parent_checkpoint_sha256") != CONTINUATION_START_SHA
            or acceptance.get("reward_config") != reward
            or acceptance.get("optimizer_resumed") is not True
            or acceptance.get("advanced_parameters", 0) <= 0):
        raise ValueError("1753 续训首轮验收未通过身份校验")
    entries = completed_trial_entries(root, protocol, run=CONTINUATION_RUN,
        category=CONTINUATION_CATEGORY, category_label=CONTINUATION_LABEL,
        label_prefix="塔血 β=0.15", require_optimizer_audit=True)
    if entries and acceptance.get("checkpoint_sha256") != entries[0]["sha256"]:
        raise ValueError("1753 续训首轮权重未通过身份校验")
    return entries


def catalog(cfg):
    code, _ = runtime(cfg)
    command = shlex.join(["python3", "-c", SCAN, cfg["metadata_root"]])
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            cfg["metadata_host"],
            command,
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    entries = (json.loads(result.stdout) + experiment_entries(cfg["archive_root"])
               + settlement_entries() + continuation_entries())
    root = Path(cfg["remote_root"])
    request = root / "catalog-input.json"
    write(request, entries)
    # Torch runs only on the remote host, with mmap and cached metadata per immutable SHA.
    output = subprocess.run(
        [
            str(code / ".venv/bin/python"),
            str(Path(__file__).with_name("catalog_metadata.py")),
            str(code),
            str(request),
            cfg["archive_root"],
            str(root / "metadata-cache.json"),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=True,
        env={
            **os.environ,
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "CUDA_VISIBLE_DEVICES": "",
        },
    )
    return json.loads(output.stdout)


def control(port, command, timeout=10, cancelled=lambda: None):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(1)
        sock.sendall((command + "\n").encode())
        data = b""
        deadline = time.monotonic() + timeout
        while not data.endswith(b"\n"):
            cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError("引擎命令超时：" + command)
            try:
                part = sock.recv(65536)
            except socket.timeout:
                continue
            if not part:
                break
            data += part
    return json.loads(data)


def job_root(cfg, identity):
    if not re.fullmatch("[a-f0-9]{32}", identity):
        raise ValueError("invalid job id")
    return Path(cfg["remote_root"]) / "jobs" / identity


def avd_process_running(processes, avd):
    for line in processes.splitlines():
        try:
            args = shlex.split(line)
        except ValueError:
            continue
        if not args or not (
            Path(args[0]).name.startswith("qemu-system")
            or Path(args[0]).name == "emulator"
        ):
            continue
        if (
            "-avd" in args
            and args.index("-avd") + 1 < len(args)
            and args[args.index("-avd") + 1] == avd
        ):
            return True
    return False


def start(cfg, identity):
    root = job_root(cfg, identity)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = (
            json.loads((root / "status.json").read_text())
            if (root / "status.json").exists()
            else {}
        )
        if status.get("state") in ("completed", "failed", "cancelled") or live(
            status.get("owner")
        ):
            return status
        if status:
            return dict(state="interrupted", error="远端生成进程已中断；请重试任务")
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--config",
            json.dumps(cfg),
            "--id",
            identity,
        ]
        with (root / "supervisor.log").open("ab") as log:
            p = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        state = dict(state="starting", owner=dict(pid=p.pid, birth=birth(p.pid)))
        write(root / "status.json", state)
        return state


def stage_checkpoint(cfg, entry, destination):
    expected, allowed_root = checkpoint_location(cfg["archive_root"], entry)
    if entry.get("path") != str(expected):
        raise ValueError("checkpoint path does not match catalog identity")
    path = Path(expected).resolve()
    if not path.is_relative_to(Path(allowed_root).resolve()):
        raise ValueError("checkpoint outside archive")
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("checkpoint SHA mismatch")
    destination = Path(destination)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("staged checkpoint SHA mismatch")
        return
    try:
        os.link(path, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # The settlement branch is on the data disk, separate from video jobs.
        temporary = destination.with_suffix(".copying")
        shutil.copyfile(path, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("copied checkpoint SHA mismatch")
        temporary.replace(destination)


def run(cfg, identity):
    root = job_root(cfg, identity)
    # Synchronize with the parent's initial status write.
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
    owner = dict(pid=os.getpid(), birth=birth(os.getpid()))
    emulator = None
    generator = None
    emu_owner = None
    final = None

    def state(stage, **kwargs):
        write(
            root / "status.json",
            dict(state=stage, time=time.time(), owner=owner, **kwargs),
        )

    def cancelled():
        if (root / "CANCEL").exists():
            raise InterruptedError("任务已取消")

    resource = (Path(cfg["remote_root"]) / "engine.lock").open("a")
    adb = str(Path(cfg["remote_sdk"]) / "platform-tools/adb")
    serial = cfg["remote_serial"]

    def adbc(*args):
        return subprocess.run(
            [adb, "-s", serial, *args], capture_output=True, text=True, timeout=30
        )

    try:
        deadline = time.monotonic() + 900
        while True:
            cancelled()
            try:
                fcntl.flock(resource, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                state("waiting", message="等待独立对局引擎")
                if time.monotonic() > deadline:
                    raise TimeoutError("等待远端引擎超时")
                time.sleep(2)
        request = json.loads((root / "request.json").read_text())
        code, manifest = runtime(cfg, request.get("action_constraints"))
        write(root / "runtime-manifest.json", manifest)
        write(root / "runtime-identity.json", dict(
            fingerprint=code.name, action_constraints=request.get("action_constraints"),
            source_run=cfg["source_run"], manifest=manifest))
        state("preparing")
        if shutil.disk_usage(root).free < 2_000_000_000:
            raise RuntimeError("远端可用磁盘不足 2 GB")
        for side in ("top", "bottom"):
            stage_checkpoint(cfg, request[side], root / (side + ".pt"))
        port = int(cfg["remote_port"])
        console = int(serial.split("-")[-1])
        deadline = time.monotonic() + 900
        while True:
            cancelled()
            devices = subprocess.run(
                [adb, "devices"], capture_output=True, text=True, timeout=15
            ).stdout
            processes = subprocess.check_output(["ps", "-eo", "args="], text=True)
            busy = avd_process_running(processes, cfg["remote_avd"]) or any(
                line.split() and line.split()[0] == serial
                for line in devices.splitlines()[1:]
            )
            for p in (port, console, console + 1):
                try:
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", p))
                except OSError:
                    busy = True
            if not busy:
                break
            state("waiting", message="视频模拟器已被其他会话占用，等待释放")
            if time.monotonic() > deadline:
                raise TimeoutError("等待视频模拟器释放超时，可保留参数重试")
            time.sleep(2)
        command = [
            str(Path(cfg["remote_sdk"]) / "emulator/emulator"),
            "-avd",
            cfg["remote_avd"],
            "-port",
            str(console),
            "-cores",
            "6",
            "-memory",
            "4096",
            "-no-window",
            "-no-audio",
            "-no-boot-anim",
            "-no-snapshot",
            "-gpu",
            "swiftshader_indirect",
            "-no-metrics",
        ]
        with (root / "emulator.log").open("ab") as log:
            emulator = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        emu_owner = dict(pid=emulator.pid, birth=birth(emulator.pid), serial=serial)
        write(root / "emulator-owner.json", emu_owner)
        state("booting")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            cancelled()
            if emulator.poll() is not None:
                raise RuntimeError("独立模拟器启动失败，详情见 emulator.log")
            if adbc("shell", "getprop", "sys.boot_completed").stdout.strip() == "1":
                break
            time.sleep(1)
        else:
            raise TimeoutError("独立模拟器启动超时")
        sys.path.insert(0, str(code))
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "guest_config",
            code / "native_runner/training/v4/configure_emulator_engine_guests.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ADB = Path(adb)
        module.wait_root(serial)
        module.configure_firewall(serial)
        if (
            not adbc("shell", "pm", "path", module.PACKAGE)
            .stdout.strip()
            .startswith("package:")
        ):
            raise RuntimeError("专用模拟器缺少已验证游戏安装，请重新准备 AVD")
        adbc("shell", "svc", "power", "stayon", "true")
        adbc("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        # Cold boot occasionally leaves the app alive but its native endpoint silent.
        # Retry only bootstrap, before any match configuration or policy sampling.
        endpoint = None
        for attempt in range(1, 4):
            cancelled()
            state("booting", message=f"初始化独立对局引擎（{attempt}/3）")
            adbc("shell", "am", "force-stop", module.PACKAGE)
            launch = adbc(
                "shell",
                "am",
                "start",
                "--user",
                "0",
                "-f",
                "0x18000000",
                "-n",
                module.PACKAGE + "/com.supercell.clashroyale.Engine0App",
            )
            adbc("forward", "tcp:" + str(port), "tcp:26789")
            deadline = time.monotonic() + 50
            last_error = launch.stderr
            while time.monotonic() < deadline:
                cancelled()
                try:
                    status = module.control(port, "engine-status", timeout=2)
                    # Attestation hashes the full native library and assets. Keep
                    # one request open under load; short retries backlog the serial
                    # control server with repeated expensive hashing requests.
                    attestation = control(
                        port, "attest", timeout=90, cancelled=cancelled
                    ).get("attestation", {})
                    if not (
                        status.get("ok") is True
                        and status.get("engineId") == 0
                        and status.get("processName") == module.PACKAGE + ":engine0"
                        and status.get("port") == 26789
                        and status.get("assignedCpu") == 0
                        and int(status.get("onlineCpus", 0)) >= 6
                        and attestation.get("production_ready") is True
                    ):
                        raise RuntimeError("引擎尚未通过就绪核验")
                    module.control(port, "render off", timeout=3)
                    endpoint = dict(
                        engine_id=0,
                        host_port=port,
                        status=status,
                        attestation=attestation,
                    )
                    break
                except Exception as exc:
                    last_error = str(exc)
                time.sleep(0.5)
            if endpoint:
                break
            write(root / f"bootstrap-attempt-{attempt}.json", dict(error=last_error))
            (root / f"bootstrap-logcat-{attempt}.txt").write_text(
                adbc("logcat", "-d", "-t", "1600").stdout
            )
            write(
                root / f"bootstrap-launch-{attempt}.json",
                dict(
                    returncode=launch.returncode,
                    stdout=launch.stdout,
                    stderr=launch.stderr,
                ),
            )
        if endpoint is None:
            raise RuntimeError("独立引擎多次初始化未就绪：" + last_error)
        write(root / "engine-attestation.json", endpoint)
        state("generating")
        with (root / "generation.log").open("ab") as log:
            generator = subprocess.Popen(
                [
                    str(code / ".venv/bin/python"),
                    "-u",
                    str(Path(__file__).with_name("generate_match.py")),
                    str(code),
                    str(root),
                    str(port),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "OMP_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "CUDA_VISIBLE_DEVICES": "",
                },
            )
        write(
            root / "generator-owner.json",
            dict(pid=generator.pid, birth=birth(generator.pid)),
        )
        deadline = time.monotonic() + 1600
        while generator.poll() is None:
            cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError("对局生成超时")
            time.sleep(1)
        if generator.returncode:
            raise RuntimeError(
                "对局生成失败：" + (root / "generation.log").read_text()[-1800:]
            )
        final = ("completed", {})
    except InterruptedError as exc:
        final = ("cancelled", dict(error=str(exc)))
    except BaseException as exc:
        traceback.print_exc()
        final = ("failed", dict(error=str(exc)))
    finally:
        if generator and generator.poll() is None:
            generator.terminate()
            try:
                generator.wait(timeout=15)
            except subprocess.TimeoutExpired:
                generator.kill()
                generator.wait()
        if emulator and live(emu_owner):
            adbc("emu", "kill")
            try:
                emulator.wait(timeout=20)
            except subprocess.TimeoutExpired:
                emulator.terminate()
                emulator.wait(timeout=10)
        write(
            root / "shutdown.json",
            dict(remote_emulator_stopped=not live(emu_owner), time=time.time()),
        )
        if final:
            state(final[0], **final[1])
        resource.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--config", required=True)
    parser.add_argument("--id")
    args = parser.parse_args()
    cfg = json.loads(args.config)
    if args.command == "bootstrap":
        from setup_remote_avd import setup

        result = setup(cfg)
    elif args.command == "catalog":
        result = catalog(cfg)
    elif args.command == "start":
        result = start(cfg, args.id)
    elif args.command == "run":
        run(cfg, args.id)
        return
    elif args.command == "status":
        root = job_root(cfg, args.id)
        p = root / "status.json"
        result = json.loads(p.read_text()) if p.exists() else {"state": "missing"}
        progress = root / "progress.json"
        if progress.exists():
            result["progress"] = json.loads(progress.read_text())
        if result.get("state") not in (
            "missing",
            "completed",
            "failed",
            "cancelled",
        ) and not live(result.get("owner")):
            result.update(state="interrupted", error="远端生成进程中断")
    elif args.command == "cancel":
        root = job_root(cfg, args.id)
        if root.exists():
            (root / "CANCEL").touch()
        result = {"ok": True}
    elif args.command == "recover":
        root = job_root(cfg, args.id)
        pending = False
        if root.exists():
            (root / "CANCEL").touch()
            p = root / "status.json"
            status = json.loads(p.read_text()) if p.exists() else {}
            deadline = time.monotonic() + 40
            while live(status.get("owner")) and time.monotonic() < deadline:
                time.sleep(0.5)
            pending = live(status.get("owner"))
            if not live(status.get("owner")):
                generator = root / "generator-owner.json"
                if generator.exists():
                    owned = json.loads(generator.read_text())
                    if live(owned):
                        os.kill(owned["pid"], 15)
                emulator = root / "emulator-owner.json"
                if emulator.exists():
                    owned = json.loads(emulator.read_text())
                    if live(owned):
                        subprocess.run(
                            [
                                str(Path(cfg["remote_sdk"]) / "platform-tools/adb"),
                                "-s",
                                owned["serial"],
                                "emu",
                                "kill",
                            ],
                            capture_output=True,
                            timeout=30,
                        )
        result = {"ok": True, "pending": pending}
    elif args.command == "delete":
        root = job_root(cfg, args.id)
        if root.exists():
            p = root / "status.json"
            state = json.loads(p.read_text()) if p.exists() else {}
            if live(state.get("owner")):
                raise RuntimeError("task still running")
            shutil.rmtree(root)
        result = {"ok": True}
    else:
        raise ValueError("invalid command")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
