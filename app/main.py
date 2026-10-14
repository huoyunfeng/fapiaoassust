"""应用入口：初始化基础设施、装配路由。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles  # noqa: F401 (预留)

from app.config import settings
from app.infra.logger import setup_logging
from app.repository.db import init_schema
from app.web.api import api
from app.web.pages import pages


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    init_schema()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="发票报销核对助手", version="1.0.0", lifespan=lifespan)
    app.include_router(pages)
    app.include_router(api)
    return app


app = create_app()
