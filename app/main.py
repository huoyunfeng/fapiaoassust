"""应用入口：初始化基础设施、装配路由。"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles  # noqa: F401 (预留)

from app.config import settings
from app.infra.logger import setup_logging
from app.repository.db import init_schema
from app.web.api import api
from app.web.auth import auth
from app.web.middleware import AuthMiddleware
from app.web.pages import pages


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    init_schema()
    # 应用上次迁移后的附件目录（必须在任何文件读写前完成）
    from app.service.storage_service import apply_saved_upload_dir
    try:
        apply_saved_upload_dir()
    except Exception as e:  # 配置异常不阻塞启动
        from app.infra.logger import get_logger
        get_logger(__name__).warning("应用附件目录失败: %s", e)
    # 旧版本生成的「自动-…」单号一次性迁移为 BX-日期-全局序号（幂等）
    from app.service.reimburse_service import (normalize_bx_padding,
                                               renumber_legacy_auto_nos)
    try:
        renumber_legacy_auto_nos()
        normalize_bx_padding()   # 序号统一 4 位（BX-YYYYMMDD-0001）
    except Exception as e:  # 迁移失败不阻塞启动
        from app.infra.logger import get_logger
        get_logger(__name__).warning("旧单号迁移失败: %s", e)
    # 回收站自动清理：启动时先扫一次（超期数据不等待），再启动每日后台检查
    from app.service.recycle_service import auto_cleanup, start_background_cleaner
    try:
        auto_cleanup()
        start_background_cleaner()
    except Exception as e:  # 清理失败不阻塞启动
        from app.infra.logger import get_logger
        get_logger(__name__).warning("回收站自动清理启动失败: %s", e)
    # 静默备份：启动时后台线程检查一次，若距上次 > 24h 则打包 DB+config（不含附件）
    # 采用 LRU 只保留最新 3 个包，避免磁盘膨胀；失败不影响主流程。
    import threading as _th
    def _silent():
        try:
            from app.service.backup_service import run_silent_backup
            run_silent_backup()
        except Exception as e:
            from app.infra.logger import get_logger
            get_logger(__name__).warning("静默备份失败: %s", e)
    _th.Thread(target=_silent, name="silent-backup", daemon=True).start()
    # 授权自动联网校验：每 30 分钟检查一次，距上次成功同步超过 6 小时才真正请求云端。
    # 目的：避免付费用户从不点「同步授权状态」而被 15 天离线宽限误降级。
    def _license_sync():
        import time as _time
        from app.service import license_service
        while True:
            try:
                license_service.auto_sync()
            except Exception:
                pass
            _time.sleep(1800)
    _th.Thread(target=_license_sync, name="license-sync", daemon=True).start()
    yield
    # 正常关停（uvicorn should_exit / 进程退出前的优雅停止）：
    # 强制释放残留数据库连接，避免 WAL 未落盘或文件被占用导致下次启动异常
    from app.repository import db
    try:
        db.dispose()
    except Exception as e:
        from app.infra.logger import get_logger
        get_logger(__name__).warning("退出释放数据库连接失败: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(title="发票报销核对助手", version="1.0.0", lifespan=lifespan)

    @app.get("/health", include_in_schema=False)
    def health():
        """存活探针：供中间件放行的匿名检查、部署脚本与打包安装后的启动确认使用。"""
        return {"ok": True, "status": "healthy"}

    @app.post("/api/_shutdown", include_in_schema=False)
    def shutdown(x_shutdown_token: str = Header(default="")):
        """本机关停接口：仅桌面启动器持有令牌，用于关闭窗口前释放数据库连接。

        未设置令牌（源码 `python run.py` 调试模式）时一律拒绝，避免被本机其他
        进程误调用；令牌每次启动随机生成，仅通过环境变量传给本进程。
        """
        expected = os.getenv("FAPIAO_SHUTDOWN_TOKEN", "")
        if not expected or x_shutdown_token != expected:
            raise HTTPException(status_code=403, detail="forbidden")
        from app.repository import db
        db.dispose()
        return {"ok": True, "status": "shutting_down"}

    app.include_router(pages)
    app.include_router(auth)
    app.include_router(api)
    app.add_middleware(AuthMiddleware)
    return app


app = create_app()
