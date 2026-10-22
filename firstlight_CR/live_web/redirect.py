"""Redirect the retired standalone port without starting any task workers."""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from render_web.config import Config


def create_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/{path:path}")
    def redirect(path: str):
        return RedirectResponse(f"http://127.0.0.1:{Config.load().port}/live/", status_code=308)

    return app
