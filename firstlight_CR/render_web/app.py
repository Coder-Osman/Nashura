from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import shutil
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import Config, DECKS, DECK_CARDS, deck_options
from .service import Service
from .store import TERMINAL
from .local_render import live
from .runtime_constraints import delay_constraints_contract


class NewJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str = "selfplay"
    top: str
    bottom: str | None = None
    deck_top: str = "golem-beatdown"
    deck_bottom: str = "golem-beatdown"
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)


class HumanSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    checkpoint: str
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)


class HumanPlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hand_index: int = Field(ge=0, le=3)
    source_x: float = Field(ge=0, le=1080)
    source_y: float = Field(ge=0, le=1920)


def public_checkpoint(c):
    return {k: v for k, v in c.items() if k not in ("path", "contract", "source")}


def public_job(job):
    result = {k: v for k, v in job.items() if k != "worker"}
    result["request"] = {
        **job["request"],
        **{side: public_checkpoint(job["request"][side]) for side in ("top", "bottom")},
    }
    return result


def create_app(config=None, *, start_worker=True):
    config = config or Config.load()
    service = Service(config)
    store = service.store
    from live_web.app import create_app as create_live_app
    from live_web.checkpoints import Checkpoints
    from live_web.remote_weights import RemoteWeights
    from live_web.service import Service as LiveService
    from .human import HumanSessionError, HumanSessionManager
    from .config import REPO

    live_data = REPO / "artifacts/live-web" if config.data == Config().data else config.data / "live"
    live_data.mkdir(parents=True, exist_ok=True)
    shared_weights = RemoteWeights(live_data, config=config, remote=service.remote,
                                   store=store, refresh=service.refresh_async)
    live_service = LiveService(data=live_data,
        catalog=Checkpoints(REPO, live_data, remote=shared_weights),
        match_mode="friendly")
    human_manager = HumanSessionManager(config, live_service.catalog)

    @asynccontextmanager
    async def lifespan(app):
        live_service.open()
        if start_worker:
            service.start()
        yield
        human_manager.close()
        live_service.close()
        if start_worker:
            service.close()

    app = FastAPI(
        title="CR 工作台", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.service = service
    app.state.live_service = live_service
    app.state.human_manager = human_manager

    @app.middleware("http")
    async def local_origin(request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "仅接受本机请求"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin not in (
                f"http://127.0.0.1:{config.port}",
                f"http://localhost:{config.port}",
                "http://testserver",
            ):
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
        return await call_next(request)

    def job(identity):
        try:
            return store.get(identity)
        except KeyError:
            raise HTTPException(404, "任务不存在")

    def disk_check():
        if shutil.disk_usage(config.data).free < config.min_free_bytes:
            raise HTTPException(507, "磁盘空间不足，请删除不需要的视频后重试")

    @app.get("/api/health")
    def health():
        return dict(app="cr-render-web", version=2, features=["video", "live", "shared-checkpoints"],
                    action_constraints=delay_constraints_contract())

    @app.get("/api/options")
    def options():
        return dict(
            decks=deck_options(),
            free_bytes=shutil.disk_usage(config.data).free,
        )

    @app.get("/api/checkpoints")
    def checkpoints():
        entries = store.checkpoints()
        eligible = [x for x in entries if x["available"] and x["supported"]]
        latest = (
            max(eligible, key=lambda x: (x["iteration"], x["run"]))["id"]
            if eligible
            else None
        )
        categories = []
        for category in dict.fromkeys(x.get("category", "formal") for x in entries):
            group = [x for x in eligible if x.get("category", "formal") == category]
            if group:
                newest = max(group, key=lambda x: (x["iteration"], x["run"]))
                categories.append(dict(id=category, label=newest.get("category_label", "正式训练"),
                                       latest=newest["id"], iteration=newest["iteration"]))
        return dict(
            items=[public_checkpoint(x) for x in entries],
            latest=latest,
            categories=categories,
            status=store.setting("catalog_status", {}),
        )

    @app.post("/api/checkpoints/refresh", status_code=202)
    def refresh():
        service.refresh_async()
        return dict(accepted=True)

    @app.get("/api/human/sessions/{identity}")
    def human_session(identity: str):
        try:
            return human_manager.get(identity).public()
        except KeyError:
            raise HTTPException(404, "人机对局不存在")

    @app.post("/api/human/sessions", status_code=202)
    def create_human_session(body: HumanSessionRequest):
        try:
            checkpoint = live_service.catalog.get(body.checkpoint)
            session = human_manager.create(checkpoint, seed=body.seed)
            return session.public()
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/human/sessions/{identity}/play")
    def human_play(identity: str, body: HumanPlayRequest):
        try:
            return human_manager.queue_play(identity, body.model_dump()).public()
        except KeyError:
            raise HTTPException(404, "人机对局不存在")
        except HumanSessionError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/human/sessions/{identity}/stop")
    def stop_human_session(identity: str):
        try:
            return human_manager.stop_session(identity).public()
        except KeyError:
            raise HTTPException(404, "人机对局不存在")

    @app.get("/api/human/sessions/{identity}/screen")
    def human_screen(identity: str):
        try:
            return Response(human_manager.screen(identity), media_type="image/png",
                            headers={"Cache-Control": "no-store"})
        except KeyError:
            raise HTTPException(404, "人机对局不存在")
        except HumanSessionError as exc:
            raise HTTPException(503, str(exc))

    @app.get("/api/jobs")
    def jobs():
        return dict(items=[public_job(x) for x in store.jobs()])

    @app.post("/api/jobs", status_code=202)
    def create(body: NewJob):
        disk_check()
        if (
            body.mode not in ("selfplay", "versus")
            or body.deck_top not in DECKS
            or body.deck_bottom not in DECKS
        ):
            raise HTTPException(422, "对战模式或卡组无效")
        try:
            top = store.checkpoint(body.top)
            bottom = top if body.mode == "selfplay" else store.checkpoint(body.bottom)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not all(x["available"] and x["supported"] for x in (top, bottom)):
            raise HTTPException(422, "权重未通过可用性或契约检查")
        if top["compatibility"] != bottom["compatibility"]:
            raise HTTPException(422, "双方输入或动作配置不兼容")
        request = dict(
            mode=body.mode,
            top=top,
            bottom=bottom,
            deck_top=body.deck_top,
            deck_bottom=body.deck_bottom,
            deck_cards=dict(top=list(DECK_CARDS[body.deck_top]),
                            bottom=list(DECK_CARDS[body.deck_bottom])),
            seed=body.seed if body.seed is not None else secrets.randbits(32),
            action_constraints=delay_constraints_contract(),
        )
        return public_job(store.create(request))

    @app.get("/api/jobs/{identity}")
    def get_job(identity: str):
        return public_job(job(identity))

    @app.post("/api/jobs/{identity}/cancel")
    def cancel(identity: str):
        job(identity)
        return public_job(store.cancel(identity))

    @app.post("/api/jobs/{identity}/retry", status_code=202)
    def retry(identity: str):
        disk_check()
        previous = job(identity)
        if previous["state"] not in TERMINAL:
            raise HTTPException(409, "当前任务尚未结束")
        return public_job(store.create(previous["request"], retry_of=identity))

    @app.delete("/api/jobs/{identity}")
    def delete(identity: str):
        previous = job(identity)
        if previous["state"] not in TERMINAL:
            raise HTTPException(409, "请先取消正在进行的任务")
        if live(previous["worker"]) or previous["progress"].get("cleanup_pending"):
            raise HTTPException(409, "任务正在清理所属资源，请稍后删除")
        if any(
            x["retry_of"] == identity and x["state"] not in TERMINAL
            for x in store.jobs()
        ):
            raise HTTPException(409, "重试任务仍在使用这份轨迹")
        # Delete the remote job first. A connection failure preserves the history and its retry data.
        if start_worker:
            try:
                service.remote.call("delete", identity)
            except Exception as exc:
                raise HTTPException(503, "远端清理暂未完成，请稍后重试：" + str(exc))
        root = config.data / "jobs" / identity
        if root.exists():
            shutil.rmtree(root)
        store.delete(identity)
        return dict(deleted=True)

    @app.get("/api/jobs/{identity}/files/{name}")
    def file(identity: str, name: str, download: bool = False):
        record = job(identity)
        allowed = {
            "video.mp4": "video/mp4",
            "preview-start.jpg": "image/jpeg",
            "preview-mid.jpg": "image/jpeg",
            "preview-end.jpg": "image/jpeg",
            "metadata.json": "application/json",
        }
        if name not in allowed or record["state"] != "completed":
            raise HTTPException(404, "产物尚未就绪")
        root = (config.data / "jobs" / identity).resolve()
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise HTTPException(404, "文件不存在")
        filename = (
            f"RL{record['request']['top']['iteration']}-vs-RL{record['request']['bottom']['iteration']}-seed{record['request']['seed']}.mp4"
            if name == "video.mp4"
            else name
        )
        return FileResponse(
            path,
            media_type=allowed[name],
            filename=filename if download else None,
            content_disposition_type="attachment" if download else "inline",
        )

    static = Path(__file__).parent / "static"

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/play")
    def play():
        return RedirectResponse("/live/?mode=friendly", status_code=307)

    app.mount("/static", StaticFiles(directory=static), name="static")
    app.mount("/live", create_live_app(live_service), name="live")
    return app
