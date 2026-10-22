"""Interactive local human-vs-AI sessions for the CR workbench."""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

import torch

from native_runner.battle_env import BattleEnvV1
from native_runner.contracts import ActionKind, ActionV1
from native_runner.cr_native_env import (
    COMMAND_CONSUMPTION_STEPS,
    LIVE_COMMAND_AGE_TICKS,
    NativeClashEnv,
    RunnerError,
)
from native_runner.match_factory import MatchConfig, tournament_level_kwargs
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
from native_runner.training.event_abilities import ability_action_from_plan, ability_plan
from native_runner.training.event_action_space import event_cell_to_native, legal_event_actions
from native_runner.training.event_cache import canonicalize_event_observation
from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.v4.expert import POLICY_DECISION_TICKS
from native_runner.training.v4.factory import build_episode_tensorizer_v4, production_semantic_bundle

from .config import DECK_CARDS, Config, write_json


class HumanSessionError(RuntimeError):
    pass


@dataclass
class HumanSession:
    identity: str
    checkpoint: dict
    seed: int
    root: Path
    state: str = "queued"
    phase: str = "排队中"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    tick: int = 0
    winner: int | None = None
    error: str | None = None
    worker: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    action_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_human: dict | None = None
    last_state: dict = field(default_factory=dict)
    native: NativeClashEnv | None = None
    emulator: subprocess.Popen | None = None
    emulator_owner: dict | None = None
    engine_lock: object | None = None

    def update(self, **values) -> None:
        for key, value in values.items():
            setattr(self, key, value)
        self.updated = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        write_json(self.root / "session.json", self.public())

    def public(self) -> dict:
        return {
            "id": self.identity,
            "state": self.state,
            "phase": self.phase,
            "created": self.created,
            "updated": self.updated,
            "tick": self.tick,
            "winner": self.winner,
            "error": self.error,
            "checkpoint": {
                key: value
                for key, value in self.checkpoint.items()
                if key not in {"path", "contract", "source"}
            },
            "seed": self.seed,
            "owner_top": "AI",
            "owner_bottom": "人类",
            "state_view": self.last_state,
            "screen_url": f"/api/human/sessions/{self.identity}/screen",
            "can_play": self.state in {"ready", "running"},
        }


def _birth(pid: int) -> str:
    return subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()


def _alive(owner: dict | None) -> bool:
    return bool(owner and owner.get("pid") and _birth(int(owner["pid"])) == owner.get("birth"))


def _world_to_grid(x: int, y: int) -> tuple[int, int]:
    if not (0 <= x < 18_000 and 0 <= y < 32_000):
        raise ValueError("落点超出棋盘范围")
    return min(17, max(0, int(x) // 1000)), min(31, max(0, int(y) // 1000))


def _arena_click_to_world(source_x: float, source_y: float) -> tuple[int, int]:
    """Convert a browser click in the stock renderer coordinate space."""

    from native_runner.native_overlay import arena_source_to_world

    return arena_source_to_world(float(source_x), float(source_y))


def _rendered_action(action: ActionV1) -> ActionV1:
    """Apply the same native command age used by the offline live renderer."""

    if action.kind != ActionKind.PLAY_CARD:
        return action
    offset = action.execute_offset_ticks + COMMAND_CONSUMPTION_STEPS
    return ActionV1.from_mapping(
        {
            **action.to_dict(),
            "execute_offset_ticks": offset,
            "metadata": {
                **dict(action.metadata),
                "execute_offset_ticks": offset,
                "native_command_age_ticks": LIVE_COMMAND_AGE_TICKS,
            },
        }
    )


def _player_state(raw: dict, owner: int) -> dict:
    player = next((item for item in raw.get("players", []) if int(item.get("owner", -1)) == owner), None)
    if not player:
        return {"owner": owner, "hand": [], "elixir": 0.0}
    return {
        "owner": owner,
        "elixir": round(float(player.get("elixir", player.get("elixirExact", 0.0))), 2),
        "hand": [
            {
                "hand_index": int(card.get("handIndex", index)),
                "card_id": int(card.get("cardId", 0)),
                "cost": int(card.get("cost", 0)),
                "deck_slot": int(card.get("deckSlot", index)),
            }
            for index, card in enumerate(player.get("hand", []))
        ],
    }


def _state_view(raw: dict, fallback_tick: int) -> dict:
    """Build the small state envelope consumed by the human-play page."""

    return {
        "tick": int(raw.get("tick", fallback_tick)),
        "ended": bool(raw.get("ended", False) or raw.get("finalized", False)),
        "winner": raw.get("winner"),
        "crowns": raw.get("crowns"),
        "top": _player_state(raw, 0),
        "bottom": _player_state(raw, 1),
    }


def _publish_observation(session: HumanSession, raw: dict, fallback_tick: int) -> dict:
    view = _state_view(raw, fallback_tick)
    session.last_state = view
    session.tick = int(view["tick"])
    return view


def _recoverable_observe_error(error: RunnerError) -> bool:
    """Only transient read/transport failures may retain the previous frame."""

    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "native runner transport failed",
            "persistent native control session ended",
            "invalid native runner response",
        )
    )


def _observe_with_retry(native: NativeClashEnv) -> tuple[dict | None, RunnerError | None]:
    """Read one frame, retrying one known transient transport failure."""

    try:
        return native.observe(), None
    except RunnerError as error:
        if not _recoverable_observe_error(error):
            raise
        time.sleep(0.05)
        try:
            return native.observe(), None
        except RunnerError as retry_error:
            if not _recoverable_observe_error(retry_error):
                raise
            return None, retry_error


def _rebind_ai_card_action(environment, action: ActionV1) -> ActionV1:
    """Bind a decoded AI card to its current native hand slot.

    The native renderer owns hand rotation. Card identity is stable across a
    rotation while the visible slot is not, so a slot-only action can become
    invalid between decoding and submission. Synchronized rendering normally
    makes that window empty; this check keeps the contract explicit if an
    external renderer update still races the decision loop.
    """

    if action.kind != ActionKind.PLAY_CARD or action.card_id is None:
        return action
    raw = environment.raw_observation
    player = next(
        (item for item in raw.get("players", ()) if int(item.get("owner", -1)) == int(action.owner)),
        None,
    )
    if player is None:
        raise RunnerError(f"AI owner {action.owner} is missing from the native observation")
    selected = next(
        (item for item in player.get("hand", ()) if int(item.get("cardId", -1)) == int(action.card_id)),
        None,
    )
    if selected is None:
        raise RunnerError(f"AI card {action.card_id} is no longer in the native hand")
    current_slot = int(selected.get("handIndex", -1))
    if not 0 <= current_slot < 4:
        raise RunnerError(f"AI card {action.card_id} has an invalid native hand slot")
    if current_slot == action.hand_slot:
        return action
    return ActionV1.from_mapping(
        {
            **action.to_dict(),
            "hand_slot": current_slot,
            "metadata": {
                **dict(action.metadata),
                "hand_slot_rebound": True,
                "decoded_hand_slot": action.hand_slot,
                "bound_hand_slot": current_slot,
            },
        }
    )


def _record_ai_action(session: HumanSession, action: ActionV1, *, outcome: str,
                      raw: dict, error: str | None = None) -> None:
    """Persist compact AI action diagnostics without changing the policy input."""

    row = {
        "time": time.time(),
        "tick": int(raw.get("tick", -1)),
        "outcome": outcome,
        "action": action.to_dict(),
        "hand": _player_state(raw, action.owner).get("hand", []),
    }
    if error:
        row["error"] = error
    with (session.root / "ai-actions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


class HumanSessionManager:
    def __init__(self, config: Config, catalog, *, data: Path | None = None):
        self.config = config
        self.catalog = catalog
        self.data = Path(data or config.data / "human")
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions: dict[str, HumanSession] = {}

    def create(self, checkpoint: dict, seed: int | None = None) -> HumanSession:
        identity = uuid.uuid4().hex
        session = HumanSession(
            identity=identity,
            checkpoint=checkpoint,
            seed=int(seed if seed is not None else int.from_bytes(os.urandom(4), "big")),
            root=self.data / identity,
        )
        with self.lock:
            self.sessions[identity] = session
        session.update()
        session.worker = threading.Thread(target=self._run, args=(session,), daemon=True,
                                           name=f"human-match-{identity[:8]}")
        session.worker.start()
        return session

    def get(self, identity: str) -> HumanSession:
        with self.lock:
            session = self.sessions.get(identity)
        if session is None:
            raise KeyError(identity)
        return session

    def stop_session(self, identity: str) -> HumanSession:
        session = self.get(identity)
        if session.state in {"ended", "failed", "cancelled"}:
            return session
        session.stop.set()
        session.update(state="stopping", phase="正在结束对局")
        return session

    def queue_play(self, identity: str, payload: dict) -> HumanSession:
        session = self.get(identity)
        if session.state not in {"ready", "running"}:
            raise HumanSessionError("当前对局还没有进入可操作状态")
        try:
            hand_index = int(payload["hand_index"])
            source_x = float(payload["source_x"])
            source_y = float(payload["source_y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HumanSessionError("动作参数无效") from exc
        if not 0 <= hand_index < 4:
            raise HumanSessionError("只能选择下方四个手牌槽")
        try:
            world_x, world_y = _arena_click_to_world(source_x, source_y)
        except ValueError as exc:
            raise HumanSessionError(str(exc)) from exc
        with session.action_lock:
            if session.pending_human is not None:
                raise HumanSessionError("上一手牌仍在处理，请稍候")
            session.pending_human = {"hand_index": hand_index, "world_x": world_x, "world_y": world_y}
        return session

    def screen(self, identity: str) -> bytes:
        session = self.get(identity)
        if not _alive(session.emulator_owner):
            raise HumanSessionError("交互模拟器尚未就绪")
        result = subprocess.run(
            [self.config.adb, "-s", self.config.human_serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=8,
        )
        if result.returncode or not result.stdout.startswith(b"\x89PNG"):
            raise HumanSessionError("暂时无法读取游戏画面")
        return result.stdout

    def close(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop.set()
        for session in sessions:
            if session.worker:
                session.worker.join(timeout=30)

    def _run(self, session: HumanSession) -> None:
        lock_path = self.config.data / "worker.lock"
        try:
            with lock_path.open("a") as engine_lock:
                session.engine_lock = engine_lock
                fcntl.flock(engine_lock, fcntl.LOCK_EX)
                if session.stop.is_set():
                    return
                self._run_owned(session)
        except BaseException as exc:  # session failures are reported in the UI
            session.update(state="failed", phase="失败", error=str(exc))
        finally:
            self._cleanup(session)

    def _run_owned(self, session: HumanSession) -> None:
        session.update(state="preparing", phase="准备 AI 权重")
        stop = session.stop

        def progress(done, total):
            if total:
                session.update(phase=f"下载 AI 权重 {done / total:.0%}")

        row = self.catalog.prepare(session.checkpoint, stop=stop, progress=progress)
        checkpoint_path = Path(row.get("path") or "")
        if not checkpoint_path.is_file() or hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() != row["sha256"]:
            raise HumanSessionError("AI 权重本地缓存校验失败")
        write_json(session.root / "identity.json", {
            "checkpoint_sha256": row["sha256"], "seed": session.seed,
            "top_owner": 0, "bottom_owner": 1,
        })

        session.update(phase="启动交互模拟器")
        self._boot(session)
        native = NativeClashEnv("127.0.0.1", self.config.human_control_port, timeout=20.0)
        session.native = native
        bundle = production_semantic_bundle()
        deck = tuple(DECK_CARDS["golem-beatdown"])
        match = MatchConfig(
            deck0=deck, deck1=deck, seed=session.seed,
            owner0_name=f"AI RL{row.get('iteration', '?')}", owner1_name="Human",
            **tournament_level_kwargs(),
        )
        environment = BattleEnvV1(
            native=native,
            card_catalog=bundle.native_card_catalog,
            semantic_subset_contract=bundle.semantic_subset_contract,
            effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
            ruleset_id=hashlib.sha256(b"human-web.v1").hexdigest(),
            terminal_margin_beta=0.0,
        )
        observations, _ = environment.reset(
            match_config=match,
            options={
                "render_mode": "native-render",
                "pause_native_render": True,
                "synchronize_native_render_steps": True,
                "allow_initial_remaining_runtime_rejections": True,
                "decision_ticks": POLICY_DECISION_TICKS,
                "event_driven_decisions": False,
            },
        )
        loaded = load_event_checkpoint(checkpoint_path, device="cpu")
        model = loaded.model.eval()
        event_config = model.event_config
        tensorizer = build_episode_tensorizer_v4(environment.episode_config, actor_owner=0)
        initial_elixir = {owner: next(p.elixir_exact for p in observations[owner].players if p.owner == owner)
                          for owner in (0, 1)}
        tensorizer.start_episode(observations[0], initial_elixir=initial_elixir)
        native.set_speed(1.0)
        raw, observe_error = _observe_with_retry(native)
        if raw is None:
            raise observe_error or RunnerError("首次原生观测失败")
        initial_view = _publish_observation(session, raw, int(observations[0].tick))
        if initial_view["ended"]:
            session.update(
                state="ended", phase="对局结束", tick=session.tick,
                winner=initial_view.get("winner"), error=None,
            )
            return
        session.update(state="ready", phase="对局进行中", tick=session.tick, error=None)

        while not stop.is_set():
            tick = int(observations[0].tick)
            raw, observe_error = _observe_with_retry(native)
            if raw is None:
                # Keep the last complete hand/elixir frame visible while the
                # read-only native channel reconnects. No action is submitted
                # against an observation whose native state was not captured.
                session.update(
                    state="running", phase="等待原生观测恢复",
                    tick=session.tick, error=str(observe_error),
                )
                time.sleep(0.05)
                continue
            view = _publish_observation(session, raw, tick)
            session.update(state="running", phase="对局进行中", tick=session.tick, error=None)
            if view["ended"]:
                session.update(
                    state="ended", phase="对局结束", tick=session.tick,
                    winner=view.get("winner"), error=None,
                )
                break

            human = None
            with session.action_lock:
                if session.pending_human is not None:
                    human = session.pending_human
                    session.pending_human = None

            if human is not None:
                grid = _world_to_grid(human["world_x"], human["world_y"])
                human_action = ActionV1.play(
                    owner=1, hand_slot=human["hand_index"], grid=grid,
                    execute_offset_ticks=1, next_decision_ticks=POLICY_DECISION_TICKS,
                    action_id=f"{session.identity}:human:{tick}",
                )
                try:
                    observations, *_ = environment.step(
                        {
                            0: ActionV1.wait(0, ticks=POLICY_DECISION_TICKS),
                            1: _rendered_action(human_action),
                        }, record_trace=False
                    )
                except Exception as exc:
                    session.update(phase="人类动作被拒绝", error=str(exc))
                    observations, *_ = environment.step(
                        {
                            0: ActionV1.wait(0, ticks=POLICY_DECISION_TICKS),
                            1: ActionV1.wait(1, ticks=POLICY_DECISION_TICKS),
                        }, record_trace=False
                    )
                continue

            # Run the selected event model only for owner 0. Owner 1 receives
            # WAIT until the browser queues a concrete bottom-side action.
            observation = observations[0]
            ai_action = self._event_ai_action(
                session, environment, observation, model, event_config, tensorizer, bundle
            )
            try:
                ai_action = _rebind_ai_card_action(environment, ai_action)
                _record_ai_action(session, ai_action, outcome="submitted", raw=environment.raw_observation)
                observations, *_ = environment.step(
                    {
                        0: ai_action,
                        1: ActionV1.wait(1, ticks=POLICY_DECISION_TICKS),
                    }, record_trace=False
                )
            except Exception as exc:
                _record_ai_action(
                    session,
                    ai_action,
                    outcome="rejected",
                    raw=environment.raw_observation,
                    error=str(exc),
                )
                # A stale AI plan is discarded and the next observation gets a
                # fresh legality mask. It is never exposed as a human error.
                session.update(phase="AI 动作被拒绝，重新决策", error=str(exc))
                observations, *_ = environment.step(
                    {
                        0: ActionV1.wait(0, ticks=POLICY_DECISION_TICKS),
                        1: ActionV1.wait(1, ticks=POLICY_DECISION_TICKS),
                    }, record_trace=False
                )
        if session.state not in {"ended", "failed"}:
            session.update(state="cancelled", phase="已停止")

    def _event_ai_action(self, session, environment, observation, model, event_config, tensorizer, bundle):
        """Decode one legal event action for the top-side AI."""

        if int(observation.tick) < 90:
            return ActionV1.wait(0, ticks=POLICY_DECISION_TICKS)
        frame = canonicalize_event_observation(tensorizer.tensorize(observation, validate=False))
        legal = legal_event_actions(
            environment,
            observation,
            frame,
            0,
            catalog=bundle.card_catalog,
            card_count=model.card_count,
            cell_count=model.cell_count,
            board_width=event_config.board_width,
            board_height=event_config.board_height,
            delay_offset_ms=event_config.delay_offset_ms,
            device=torch.device("cpu"),
        )
        batch = frame.to_model_input(torch.device("cpu"), float_dtype=torch.float32)
        with torch.inference_mode():
            context = model.encode(
                batch,
                model.initial_state(batch.batch_size, device=torch.device("cpu")),
                episode_start=torch.ones(batch.batch_size, dtype=torch.bool),
            )
            step = model.select_action(
                context,
                gate_mask=torch.tensor([[True, legal.act_allowed]], dtype=torch.bool),
                card_mask=legal.card_mask.unsqueeze(0),
                cell_masks=legal.cell_masks.unsqueeze(0),
                delay_mask=legal.delay_masks.unsqueeze(0),
                temperatures=(1.0, 1.0, 1.0, 1.0),
                sample=False,
            )
        if not bool(step.action.act[0]):
            return ActionV1.wait(0, ticks=POLICY_DECISION_TICKS)
        if getattr(model.config, "ability_actions", False) and int(step.action.ability_candidate[0]) >= 0:
            plan = ability_plan(
                frame,
                int(step.action.ability_candidate[0]),
                owner=0,
                tick=int(observation.tick),
                ability_catalog=bundle.ability_catalog,
                delay_ms=(event_config.bin_to_delay_ms(int(step.action.delay_bin[0]))
                          if model.config.ability_delay else 0),
            )
            return _rendered_action(ability_action_from_plan(
                environment,
                observation,
                0,
                plan,
                action_id=f"{session.identity}:ai:{int(observation.tick)}",
            ))
        row = int(step.action.card_row[0])
        cell_index = int(step.action.cell[0])
        delay_ms = int(event_config.bin_to_delay_ms(int(step.action.delay_bin[0])))
        action = ActionV1.play(
            owner=0,
            hand_slot=int(legal.hand_index[row]),
            card_id=int(legal.row_card_id[row]),
            grid=event_cell_to_native(cell_index, owner=0, board_width=event_config.board_width),
            execute_offset_ticks=max(1, math.ceil(delay_ms / 50)),
            next_decision_ticks=POLICY_DECISION_TICKS,
            action_id=f"{session.identity}:ai:{int(observation.tick)}",
        )
        return _rendered_action(action)

    def _boot(self, session: HumanSession) -> None:
        c = self.config
        serial_num = c.human_serial.split("-")[-1]
        devices_probe = subprocess.run(
            [c.adb, "devices", "-l"], capture_output=True, text=True, timeout=15
        )
        devices = devices_probe.stdout
        if c.human_serial in devices:
            raise HumanSessionError(f"交互模拟器 {c.human_serial} 已被占用")
        if devices_probe.returncode:
            raise HumanSessionError(
                "ADB 不可用：" + (devices_probe.stderr.strip() or "adb devices 失败")
            )
        processes = subprocess.run(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=15, check=True
        ).stdout
        if any("qemu-system" in line and "-avd " + c.human_avd in line
               for line in processes.splitlines()):
            raise HumanSessionError(f"模拟器 {c.human_avd} 已被占用")
        args = [
            c.emulator, "-avd", c.human_avd, "-port", serial_num,
            "-cores", "6", "-gpu", "host", "-no-snapshot-load",
            "-no-snapshot-save", "-no-boot-anim",
        ]
        log = (session.root / "emulator.log").open("ab")
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        session.emulator = process
        session.emulator_owner = {"pid": process.pid, "birth": _birth(process.pid), "serial": c.human_serial}
        write_json(session.root / "emulator-owner.json", session.emulator_owner)
        # AVD boot and ADB registration are separate events.  In particular,
        # the first adb shell can race the emulator transport coming online.
        session.update(phase="等待模拟器连接 ADB")
        deadline = time.monotonic() + 180
        last_devices = ""
        last_adb_error = ""
        while time.monotonic() < deadline:
            if session.stop.is_set():
                raise HumanSessionError("对局已停止")
            if process.poll() is not None:
                raise HumanSessionError("交互模拟器启动失败，请查看 emulator.log")
            try:
                listed = subprocess.run(
                    [c.adb, "devices"], capture_output=True, text=True, timeout=5
                )
                last_devices = listed.stdout.strip()
                last_adb_error = listed.stderr.strip()
                target = next(
                    (line.split() for line in listed.stdout.splitlines()
                     if line.startswith(c.human_serial)),
                    (),
                )
                if len(target) >= 2 and target[1] == "device":
                    break
            except (OSError, subprocess.SubprocessError) as exc:
                last_adb_error = str(exc)
            time.sleep(.5)
        else:
            write_json(session.root / "boot-diagnostics.json", {
                "stage": "adb-registration", "devices": last_devices,
                "stderr": last_adb_error,
            })
            raise HumanSessionError(
                f"交互模拟器未连接 ADB（{c.human_serial}；{last_adb_error or last_devices or '无设备'}）"
            )

        session.update(phase="等待 Android 启动")
        boot_deadline = time.monotonic() + 180
        last_boot = ""
        while time.monotonic() < boot_deadline:
            if session.stop.is_set():
                raise HumanSessionError("对局已停止")
            try:
                boot = subprocess.run(
                    [c.adb, "-s", c.human_serial, "shell", "getprop", "sys.boot_completed"],
                    capture_output=True, text=True, timeout=5,
                )
                last_boot = boot.stderr.strip() or boot.stdout.strip()
                if boot.returncode == 0 and boot.stdout.strip() == "1":
                    break
            except (OSError, subprocess.SubprocessError) as exc:
                last_boot = str(exc)
            time.sleep(1)
        else:
            write_json(session.root / "boot-diagnostics.json", {
                "stage": "android-boot", "devices": last_devices,
                "stderr": last_adb_error, "boot": last_boot,
            })
            raise HumanSessionError(f"交互模拟器 Android 启动超时（{last_boot or '无状态'}）")

        def adb(*args, timeout=15):
            return subprocess.run(
                [c.adb, "-s", c.human_serial, *args],
                capture_output=True, text=True, timeout=timeout,
            )

        adb("root", timeout=10)
        adb("wait-for-device", timeout=30)
        # A visible emulator may still be showing the Android keyguard after
        # boot.  Wake and unlock it before starting the native game process.
        adb("shell", "svc", "power", "stayon", "true")
        adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        adb("shell", "input", "keyevent", "82")
        adb("shell", "input", "swipe", "540", "2200", "540", "500", "300")
        adb("shell", "su", "0", "ip", "link", "set", "eth0", "down", timeout=10)
        package = "nullsroyale.rel.free"
        adb("shell", "am", "force-stop", package, timeout=10)
        launch = adb(
            "shell", "am", "start", "--user", "0", "-f", "0x18000000",
            "-n", package + "/com.supercell.clashroyale.Engine0App", timeout=20,
        )
        if launch.returncode:
            raise HumanSessionError(
                "CR 启动失败：" + (launch.stderr.strip() or launch.stdout.strip() or "am start 失败")
            )
        forward = adb("forward", "tcp:" + str(c.human_control_port), "tcp:26789", timeout=10)
        if forward.returncode:
            raise HumanSessionError(
                "原生控制端口转发失败：" + (forward.stderr.strip() or "adb forward 失败")
            )
        native = NativeClashEnv("127.0.0.1", c.human_control_port, timeout=10)
        deadline = time.monotonic() + 150
        session.update(phase="等待原生引擎就绪")
        last_attest_error = ""
        while time.monotonic() < deadline:
            if session.stop.is_set():
                native.close_transport()
                raise HumanSessionError("对局已停止")
            try:
                if native.attest().production_ready:
                    session.native = native
                    return
            except Exception as exc:
                last_attest_error = str(exc)
            time.sleep(.25)
        try:
            logcat = adb("logcat", "-d", "-t", "120", timeout=10)
            log_tail = logcat.stdout[-6000:]
        except Exception as exc:
            log_tail = str(exc)
        write_json(session.root / "boot-diagnostics.json", {
            "stage": "native-attestation", "devices": last_devices,
            "boot": last_boot, "attest": last_attest_error, "logcat": log_tail,
        })
        native.close_transport()
        detail = last_attest_error or "无响应"
        raise HumanSessionError(f"本机原生引擎未就绪（{detail}；详见 boot-diagnostics.json）")

    def _cleanup(self, session: HumanSession) -> None:
        if session.native:
            try:
                session.native.pause()
            except Exception:
                pass
            session.native.close_transport()
        if _alive(session.emulator_owner):
            subprocess.run([self.config.adb, "-s", self.config.human_serial, "emu", "kill"],
                           capture_output=True, timeout=20)
            deadline = time.monotonic() + 30
            while _alive(session.emulator_owner) and time.monotonic() < deadline:
                time.sleep(.5)
        session.emulator = None
        session.native = None
