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
    # 旧版本生成的「自动-…」单号一次性迁移为 BX-日期-全局序号（幂等）
    from app.service.reimburse_service import (normalize_bx_padding,
                                               renumber_legacy_auto_nos)
    try:
        renumber_legacy_auto_nos()
        normalize_bx_padding()   # 序号统一 4 位（BX-YYYYMMDD-0001）
    except Exception as e:  # 迁移失败不阻塞启动
        from app.infra.logger import get_logger
        get_logger(__name__).warning("旧单号迁移失败: %s", e)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="发票报销核对助手", version="1.0.0", lifespan=lifespan)
    app.include_router(pages)
    app.include_router(api)
    return app


app = create_app()
