"""附件存储目录迁移：原生选择框 + 强制断连 + 物理移动 + 失败回滚。

迁移顺序（设计要求，防止 Windows 文件占用导致半迁移）：
1. 校验新目录（绝对路径、非旧目录子/父目录、为空目录）；
2. 强制关闭全部数据库连接；
3. 逐项物理移动 uploads 内容；
4. 任一步失败 -> 全部移回原路径并恢复配置；
5. 成功后把新路径写入 kv，重启后自动生效。
"""
import shutil
from pathlib import Path

from app.config import settings
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import kv_repo

logger = get_logger(__name__)
KV_KEY = "upload_dir"


class StorageError(Exception):
    """目录不可用 / 迁移失败（message 可直接展示）。"""


def apply_saved_upload_dir() -> None:
    """启动时应用上次迁移后的目录（幂等）。"""
    try:
        with db.read() as conn:
            saved = kv_repo.get(conn, KV_KEY)
        if saved and Path(saved).is_dir():
            settings.upload_dir = Path(saved)
    except Exception as e:
        logger.warning("读取附件目录配置失败: %s", e)
    # FileStore.root 是动态读取 settings.upload_dir 的只读属性，无需另行赋值


def current_upload_dir() -> str:
    return str(settings.upload_dir)


def pick_folder() -> str:
    """调用操作系统原生文件夹选择框（桌面版可用；无 GUI 环境给出提示）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="选择附件存储目录")
        root.destroy()
        return folder or ""
    except Exception as e:
        logger.warning("打开目录选择框失败: %s", e)
        raise StorageError("无法打开系统目录选择框，请在输入框中手动填写绝对路径")


def _ensure_empty_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise StorageError("目标路径已存在且不是文件夹")
        if any(path.iterdir()):
            raise StorageError("目标文件夹非空，请选择空文件夹避免覆盖已有附件")
    else:
        path.mkdir(parents=True, exist_ok=True)


def migrate(new_path: str) -> dict:
    raw = (new_path or "").strip().strip('"')
    if not raw:
        raise StorageError("请填写新的附件存储目录")
    new_dir = Path(raw).expanduser()
    if not new_dir.is_absolute():
        raise StorageError("请填写绝对路径")
    new_dir = new_dir.resolve()
    old_dir = Path(settings.upload_dir).resolve()
    if new_dir == old_dir:
        raise StorageError("新目录与当前目录相同")
    if old_dir in new_dir.parents or new_dir in old_dir.parents:
        raise StorageError("新目录不能是当前目录的子目录或父目录")
    _ensure_empty_dir(new_dir)

    db.dispose()   # 关键：先强制断连，Windows 才不会因文件被占用导致移动失败
    moved: list[tuple[Path, Path]] = []
    try:
        if old_dir.exists():
            for item in old_dir.iterdir():
                target = new_dir / item.name
                if target.exists():
                    raise StorageError(f"目标目录已存在同名项：{item.name}")
                shutil.move(str(item), str(target))
                moved.append((item, target))
        with db.tx() as conn:
            kv_repo.set(conn, KV_KEY, str(new_dir))
    except Exception as e:
        # 回滚：把已移动的内容原样搬回
        for src, target in reversed(moved):
            try:
                shutil.move(str(target), str(src))
            except Exception as rollback_err:
                logger.error("回滚失败 %s -> %s: %s", target, src, rollback_err)
        if isinstance(e, StorageError):
            raise
        raise StorageError(f"迁移失败，已回滚原路径：{e}")

    settings.upload_dir = new_dir
    logger.info("附件目录已迁移: %s -> %s（%d 项）", old_dir, new_dir, len(moved))
    return {"old": str(old_dir), "new": str(new_dir), "moved": len(moved),
            "message": "附件目录迁移完成，数据库连接已重新建立"}
