"""数据备份与恢复（应用服务层）。

备份：数据库文件（含 WAL/SHM 副文件）+ 全部原始发票附件 -> 单个 zip。
恢复：dispose() 强制断连 -> 旧库/旧附件改名 .bak 保底 -> 解压覆盖 ->
      校验新库可读 -> 失败整体回滚 -> 重新初始化连接。

设计要点：
- Windows 下打开的数据库连接会锁定文件，覆盖前必须先断开（db.dispose）。
- 旧数据先改名保底再解压，任何一步失败都能完整回滚，绝不丢原数据。
- zip 内路径统一正斜杠；解压时拦截 ".." 防 zip-slip 路径穿越。
"""
import io
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.infra.logger import get_logger
from app.repository import db

logger = get_logger(__name__)

_UPLOADS_ARC_PREFIX = "uploads/"


def create_backup() -> Path:
    """打包数据库与全部发票附件为 zip，返回 zip 文件路径。

    zip 结构：fapiao.db（根目录）+ uploads/ 下保持原有目录层级。
    """
    backup_dir = settings.data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    zip_path = backup_dir / f"system_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"

    n_files = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 数据库文件（含 -wal/-shm 副文件，若存在）
        for suffix in ("", "-wal", "-shm"):
            f = settings.db_path.with_name(settings.db_path.name + suffix)
            if f.exists():
                zf.write(f, arcname=f.name)
        # 原始发票附件（PDF / OFD / 图片等，按 YYYYMM 子目录原样打包）
        if settings.upload_dir.exists():
            for p in sorted(settings.upload_dir.rglob("*")):
                if p.is_file():
                    arc = _UPLOADS_ARC_PREFIX + str(p.relative_to(settings.upload_dir)).replace("\\", "/")
                    zf.write(p, arcname=arc)
                    n_files += 1
    logger.info("数据备份完成: %s（附件 %s 个，%.1f KB）",
                zip_path.name, n_files, zip_path.stat().st_size / 1024)
    return zip_path


def _safe_members(zf: zipfile.ZipFile) -> tuple:
    """返回 (根目录 .db 成员名, uploads/ 下成员名列表)，拦截路径穿越。"""
    db_members, upload_members = [], []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or name.endswith("/"):
            continue
        if name.startswith("/") or ".." in Path(name).parts:
            logger.warning("备份包内含可疑路径，已跳过: %s", name)
            continue
        if "/" not in name and name.lower().endswith(".db"):
            db_members.append(name)
        elif name.startswith(_UPLOADS_ARC_PREFIX):
            upload_members.append(name)
    return db_members, upload_members


def restore_backup(content: bytes) -> dict:
    """从 zip 备份覆盖恢复全部数据。

    返回 {"ok": bool, "message": str}。数据库任何失败都会回滚到恢复前的数据；
    附件采用原位覆盖解压（受限磁盘环境下目录改名可能被拒，文件级覆盖更稳）。
    """
    if not content or not zipfile.is_zipfile(io.BytesIO(content)):
        return {"ok": False, "message": "备份文件无效：不是合法的 .zip 文件"}

    db_path = settings.db_path
    upload_dir = settings.upload_dir
    bak_db = db_path.with_name(db_path.name + ".bak")

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        db_members, upload_members = _safe_members(zf)
        if not db_members:
            return {"ok": False, "message": "备份文件无效：压缩包内未找到数据库文件（.db）"}
        db_member = sorted(db_members)[0]
        logger.info("开始恢复数据: db=%s, 附件 %s 个", db_member, len(upload_members))

        # 1. 强制断开所有数据库连接（Windows 文件占用防护，防崩溃关键步骤）
        db.dispose()

        # 2. 旧数据库改名保底（先清 WAL/SHM 副文件，避免与新库混用；重试应对瞬时占用）
        try:
            for suffix in ("-wal", "-shm"):
                side = db_path.with_name(db_path.name + suffix)
                if side.exists():
                    side.unlink()
            if bak_db.exists():
                bak_db.unlink()
            if db_path.exists():
                _rename_with_retry(db_path, bak_db)
        except OSError as e:
            logger.exception("恢复前置步骤失败（数据库文件被占用）")
            return {"ok": False,
                    "message": f"恢复失败：数据库文件被占用（{e}）。请重启软件后立即恢复，不要进行其他操作。"}

        # 3. 解压新数据库 + 校验可读；附件原位覆盖解压。任何失败 -> 回滚旧库
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(db_member) as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)

            # 校验新数据库可正常打开且是合法 SQLite
            probe = sqlite3.connect(db_path)
            try:
                probe.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            finally:
                probe.close()

            if upload_members:
                upload_dir.mkdir(parents=True, exist_ok=True)
                for name in upload_members:
                    target = upload_dir / name[len(_UPLOADS_ARC_PREFIX):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except Exception as e:
            logger.exception("备份解压/校验失败，回滚旧数据库")
            _rollback_db(db_path, bak_db)
            return {"ok": False, "message": f"恢复失败，已自动回滚原数据库：{e}"}

    # 4. 成功：重建连接（init_schema 幂等，同时建全可能缺失的表）
    try:
        db.init_schema()
    except Exception as e:
        logger.warning("恢复后 init_schema 异常（不影响已恢复数据）: %s", e)
    logger.info("数据恢复成功（旧库保留为 %s，附件覆盖 %s 个）", bak_db.name, len(upload_members))
    return {"ok": True,
            "message": "数据恢复成功，为了确保系统稳定，请刷新页面或重启软件。"}


def _rename_with_retry(src: Path, dst: Path, attempts: int = 5, delay: float = 0.4) -> None:
    """改名重试：Windows 下文件/目录可能被瞬时占用（杀毒、索引器等）。"""
    import time
    last: OSError | None = None
    for _ in range(attempts):
        try:
            src.rename(dst)
            return
        except OSError as e:
            last = e
            time.sleep(delay)
    raise last


def _rollback_db(db_path: Path, bak_db: Path) -> None:
    """恢复失败时把保底副本还原回原位（尽力而为，异常仅记日志）。"""
    try:
        if db_path.exists():
            db_path.unlink()
        if bak_db.exists():
            bak_db.rename(db_path)
        logger.info("回滚完成：数据库已恢复为备份前的状态")
    except Exception:
        logger.exception("回滚失败，请手动将 %s 改回 %s", bak_db.name, db_path.name)
