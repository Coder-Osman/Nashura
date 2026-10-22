"""One durable job process; independent of browser and HTTP connections."""

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
import paramiko

from .config import Config, write_json
from .local_render import (
    Renderer,
    Cancelled,
    validate_trace,
    validate_replay,
    validate_recording,
    export,
    recover_local,
    birth,
)
from .remote import Remote, ARTIFACTS
from .store import Store


def reuse_recording(config, previous, root, request, cancelled):
    try:
        review = previous / "visual-review.json"
        if review.exists() and not json.loads(review.read_text()).get("passed"):
            raise ValueError("已有录像未通过画面检查，需要重新渲染")
        validate_replay(previous, request)
        for name in (
            "native-full.mp4", "render-initial.json", "render-stats.json",
            "render-final.json", "render-match-aligned.json", "alignment.json",
            "registered-schedules.json", "recording.json", "recording-exit.json",
            "screenrecord.log", "render-status.jsonl",
        ):
            if (previous / name).exists():
                shutil.copyfile(previous / name, root / name)
        validate_recording(config, root, request, cancelled)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        write_json(root / "recording-reuse.json", dict(reused=False, reason=str(exc)))
        return False
    write_json(root / "recording-reuse.json", dict(reused=True, source_job=previous.name))
    return True


def run(config, identity):
    store = Store(config.data)
    # The child also persists ownership if the HTTP parent exits just after Popen.
    store.update(identity, worker=dict(pid=os.getpid(), birth=birth(os.getpid())))
    job = store.get(identity)
    request = job["request"]
    root = config.data / "jobs" / identity
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "request.json", request)
    remote = Remote(config)
    remote_started = False

    def cancelled():
        return store.get(identity)["cancel"]

    def check():
        if cancelled():
            raise Cancelled("任务已取消")

    def progress(stage, **values):
        store.update(identity, state=stage, progress=dict(stage=stage, **values))

    try:
        with (config.data / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            check()
            if shutil.disk_usage(config.data).free < config.min_free_bytes:
                raise RuntimeError("本机磁盘不足，请先在视频历史中删除不需要的任务")
            # Reuse only a complete trace whose receipts and identities pass again.
            reused = False
            if job["retry_of"]:
                previous = config.data / "jobs" / job["retry_of"]
                try:
                    validate_trace(previous, request)
                    for name in ARTIFACTS:
                        if (previous / name).exists():
                            shutil.copyfile(previous / name, root / name)
                    reused = True
                except (OSError, ValueError, KeyError):
                    pass
            if not reused:
                progress("preparing", message="固定双方权重，准备独立对局引擎")
                offline_since = None
                started = False
                cancel_sent = False
                deadline = time.monotonic() + 3000
                while time.monotonic() < deadline:
                    try:
                        if not started:
                            check()
                            # start is idempotent: loss of its reply never creates a second match.
                            remote_started = True
                            status = remote.submit(job)
                            started = True
                        else:
                            status = remote.call("status", identity)
                        offline_since = None
                        if cancelled() and not cancel_sent:
                            remote.call("cancel", identity)
                            cancel_sent = True
                        if status["state"] == "completed":
                            check()
                            remote.fetch(identity, root)
                            validate_trace(root, request)
                            break
                        if status["state"] == "cancelled":
                            raise Cancelled("任务已取消")
                        if status["state"] in ("failed", "interrupted"):
                            raise ValueError(status.get("error", "生成进程中断"))
                        stages = {
                            "starting": "preparing",
                            "preparing": "preparing",
                            "waiting": "waiting_remote",
                            "booting": "booting_remote",
                            "generating": "generating",
                        }
                        progress(
                            stages.get(status["state"], "generating"),
                            **status.get("progress", {}),
                            message=(
                                "正在取消远端对局"
                                if cancel_sent
                                else status.get("message", "生成真实对局")
                            )
                        )
                    except (OSError, EOFError, paramiko.SSHException) as exc:
                        offline_since = offline_since or time.monotonic()
                        if time.monotonic() - offline_since > 180:
                            raise RuntimeError(
                                "远端连接中断，请恢复网络后重试"
                            ) from exc
                        progress(
                            "reconnecting",
                            message="连接中断，正在查询原任务；不会重复生成",
                        )
                    time.sleep(2)
                else:
                    raise TimeoutError("生成任务超过等待期限")
            check()
            recording_reused = False
            if reused:
                progress("validating", message="核验已有录像")
                recording_reused = reuse_recording(config, previous, root, request, cancelled)
            if not recording_reused:
                renderer = Renderer(config, root, request, progress, cancelled)
                renderer.render()
            check()
            result = export(config, root, request, progress, cancelled)
            check()
            outcome = dict(state="completed", result=result, error=None)
    except Cancelled as exc:
        outcome = dict(state="cancelled", error=str(exc))
    except BaseException as exc:
        logging.exception("Job %s failed", identity)
        outcome = dict(state="failed", error=str(exc))
    finally:
        progress("cleaning", message="关闭任务所属模拟器，保存核验资料")
        cleanup_pending = False
        # Cleanup commands verify remote PID birth identity and never touch training guests.
        if remote_started and outcome["state"] != "completed":
            try:
                cleanup_pending = remote.call("recover", identity).get("pending", False)
            except Exception:
                cleanup_pending = True
                logging.exception("Remote cleanup pending for %s", identity)
        try:
            recover_local(config, root)
        except Exception:
            cleanup_pending = True
            logging.exception("Local cleanup pending for %s", identity)
        store.update(
            identity,
            **outcome,
            progress=dict(
                cleanup_pending=cleanup_pending,
                message=(
                    "专用资源清理等待重连"
                    if cleanup_pending
                    else (
                        "视频已就绪"
                        if outcome["state"] == "completed"
                        else "任务已结束"
                    )
                ),
            )
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("identity")
    args = parser.parse_args()
    run(Config.load(), args.identity)


if __name__ == "__main__":
    main()
