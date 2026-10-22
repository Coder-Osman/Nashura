import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .config import REPO
from .local_render import live, birth, recover_local
from .remote import Remote
from .store import Store, TERMINAL


class Service:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.data)
        self.remote = Remote(config)
        self.stop = threading.Event()
        self.refresh_lock = threading.Lock()
        self.thread = None

    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            return
        self.store.set_setting("catalog_status", dict(refreshing=True, error=None))
        try:
            entries = self.remote.call("catalog")
            self.store.replace_checkpoints(entries)
            self.store.set_setting(
                "catalog_status",
                dict(
                    refreshing=False,
                    error=None,
                    updated=time.time(),
                    count=len(entries),
                ),
            )
        except Exception as exc:
            logging.exception("Checkpoint refresh failed")
            previous = self.store.setting("catalog_status", {})
            self.store.set_setting(
                "catalog_status",
                dict(
                    **{
                        k: v
                        for k, v in previous.items()
                        if k not in ("refreshing", "error")
                    },
                    refreshing=False,
                    error=str(exc)
                ),
            )
        finally:
            self.refresh_lock.release()

    def refresh_async(self):
        threading.Thread(
            target=self.refresh, daemon=True, name="checkpoint-index"
        ).start()

    def start(self):
        self.thread = threading.Thread(
            target=self.loop, daemon=True, name="video-queue"
        )
        self.thread.start()
        self.refresh_async()

    def loop(self):
        children = {}
        last_catalog_attempt = time.monotonic()
        while not self.stop.is_set():
            try:
                if time.monotonic() - last_catalog_attempt >= 300:
                    self.refresh_async()
                    last_catalog_attempt = time.monotonic()
                for identity, p in list(children.items()):
                    if p.poll() is not None:
                        p.wait()
                        del children[identity]
                active = [
                    j
                    for j in self.store.jobs()
                    if j["state"] not in TERMINAL | {"queued"}
                    or j["progress"].get("cleanup_pending")
                ]
                for job in active:
                    if live(job["worker"]):
                        continue
                    if not job["worker"] and time.time() - job["updated"] < 5:
                        continue
                    self.store.update(
                        job["id"],
                        progress=dict(
                            cleanup_pending=True, message="回收中断任务的专用资源"
                        ),
                    )
                    if self.remote.call("recover", job["id"]).get("pending"):
                        self.store.update(
                            job["id"],
                            progress=dict(
                                cleanup_pending=True, message="等待远端任务收尾"
                            ),
                        )
                        continue
                    recover_local(self.config, self.config.data / "jobs" / job["id"])
                    if job["state"] in TERMINAL:
                        self.store.update(
                            job["id"],
                            progress=dict(
                                cleanup_pending=False, message="任务资源已清理"
                            ),
                        )
                    else:
                        self.store.update(
                            job["id"],
                            state="interrupted",
                            error="任务进程已中断，可保留参数重试",
                            progress=dict(
                                cleanup_pending=False, message="任务资源已清理"
                            ),
                        )
                # A terminal result can be persisted just before the worker's finally block.
                # Wait for process exit as well, including workers inherited after restart.
                if (
                    active
                    or children
                    or any(live(j["worker"]) for j in self.store.jobs())
                ):
                    self.stop.wait(1)
                    continue
                job = self.store.claim()
                if job:
                    root = self.config.data / "jobs" / job["id"]
                    root.mkdir(parents=True, exist_ok=True)
                    env = {**os.environ, "CR_RENDER_DATA": str(self.config.data)}
                    with (root / "worker.log").open("ab") as log:
                        p = subprocess.Popen(
                            [
                                sys.executable,
                                "-u",
                                "-m",
                                "render_web.runner",
                                job["id"],
                            ],
                            cwd=REPO,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                            env=env,
                        )
                    self.store.update(
                        job["id"], worker=dict(pid=p.pid, birth=birth(p.pid))
                    )
                    children[job["id"]] = p
                self.stop.wait(1)
            except Exception:
                logging.exception("Queue recovery deferred")
                self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        # Detached job processes own their cleanup; they observe this persisted cancellation.
        active = [
            j for j in self.store.jobs() if j["state"] not in TERMINAL | {"queued"}
        ]
        for job in active:
            self.store.cancel(job["id"])
        deadline = time.monotonic() + 240
        while any(live(j["worker"]) for j in active) and time.monotonic() < deadline:
            time.sleep(1)
