"""Local, detached HTTP service with an OS process lock and owned shutdown."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from .config import Config, REPO, write_json
from .local_render import birth, live


def healthy(config):
    try:
        with urlopen(
            f"http://127.0.0.1:{config.port}/api/health", timeout=2
        ) as response:
            return json.load(response).get("app") == "cr-render-web"
    except (OSError, ValueError, URLError):
        return False


def serve(config):
    import uvicorn
    from .app import create_app

    with (config.data / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("视频工作台已在运行")
        write_json(
            config.data / "server.json", dict(pid=os.getpid(), birth=birth(os.getpid()))
        )
        uvicorn.run(
            create_app(config),
            host=config.host,
            port=config.port,
            log_level="info",
            access_log=False,
        )


def main():
    parser = argparse.ArgumentParser(description="本机 CR 视频工作台")
    parser.add_argument(
        "command",
        choices=("start", "stop", "serve", "bootstrap"),
        nargs="?",
        default="start",
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    config = Config.load()
    config.data.mkdir(parents=True, exist_ok=True)
    if args.command == "bootstrap":
        from .remote import Remote

        result = Remote(config).call("bootstrap", timeout=900)
        print("远端工作台 AVD 已就绪：" + result["avd"])
        return
    if args.command == "serve":
        serve(config)
        return
    if args.command == "stop":
        owner_file = config.data / "server.json"
        owner = json.loads(owner_file.read_text()) if owner_file.exists() else None
        if not live(owner):
            print("视频工作台已停止")
            return
        os.kill(owner["pid"], signal.SIGTERM)
        print("正在停止服务；录制中的任务会先完成文件收尾。", flush=True)
        deadline = time.monotonic() + 270
        while live(owner) and time.monotonic() < deadline:
            time.sleep(1)
        if live(owner):
            raise SystemExit("任务仍在收尾，请查看 artifacts/render-web/server.log")
        print("视频工作台已停止，排队任务和视频历史已保留")
        return
    if not healthy(config):
        with (config.data / "server.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "render_web", "serve"],
                cwd=REPO,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 25
        while not healthy(config):
            if process.poll() is not None or time.monotonic() > deadline:
                raise SystemExit(
                    "服务启动未成功，请查看 artifacts/render-web/server.log（端口可能被占用）"
                )
            time.sleep(0.3)
    url = f"http://127.0.0.1:{config.port}"
    print("视频工作台：" + url)
    if not args.no_browser:
        subprocess.run(["open", url], check=True)


if __name__ == "__main__":
    main()
