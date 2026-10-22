from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .service import Service


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    checkpoint_id: str
    match_mode: Literal["standard", "friendly"] | None = None


class AddCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str


def create_app(service=None):
    service = service or Service()

    @asynccontextmanager
    async def lifespan(app):
        service.open()
        yield
        service.close()

    app = FastAPI(title="Null's Royale 对战台", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if host.split(":")[0] not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse({"detail": "仅接受本机请求"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            if request.headers.get("origin") not in {None, f"http://{host}"}:
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
            if request.headers.get("x-live-control") != "1":
                return JSONResponse({"detail": "缺少本机控制标识"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return dict(app="cr-live-web", version=1)

    @app.get("/api/checkpoints")
    def checkpoints():
        return dict(items=[{k: v for k, v in row.items() if k not in {"contract", "compatibility"}}
                           for row in service.catalog.items()], status=service.catalog.status())

    @app.post("/api/checkpoints/refresh", status_code=202)
    def refresh():
        service.catalog.refresh_async()
        return dict(accepted=True)

    @app.post("/api/checkpoints")
    def add(body: AddCheckpoint):
        try:
            return service.catalog.add(body.path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/jobs")
    def history():
        return dict(items=service.history())

    @app.post("/api/start", status_code=202)
    def start(body: StartRequest):
        try:
            return service.start(body.checkpoint_id, match_mode=body.match_mode)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/stop")
    def stop():
        return service.stop()

    @app.get("/api/screen")
    def screen():
        try:
            return Response(service.device.screen(), media_type="image/jpeg")
        except Exception as exc:
            raise HTTPException(503, "游戏画面暂不可用：" + str(exc)[:240])

    @app.get("/api/jobs/{identity}/files/{name}")
    def file(identity: str, name: str):
        allowed = {"summary.json", "policy.jsonl", "policy.log", "capture.log", "result.png"}
        if name not in allowed or not any(r["id"] == identity for r in service.history()):
            raise HTTPException(404, "文件不存在")
        path = service.data / "jobs" / identity / name
        if not path.is_file():
            raise HTTPException(404, "文件尚未生成")
        return FileResponse(path, filename=name)

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
