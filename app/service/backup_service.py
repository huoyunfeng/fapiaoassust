"""数据备份与恢复（金融级防损机制）。

模块一·备份内容：数据库（含 WAL/SHM）+ 全部附件 + 配置 + 近期日志。
模块二·备份流程：全局锁 → WAL checkpoint → 副本打包（非直打）→ finally 清临时目录。
模块三·恢复流程：dispose 断连 → 旧库/旧附件带时间戳改名保底 → 沙盒解压校验 → 原子替换 → 失败回滚。
模块四·前端：API 返回统计 dict，供前端渲染成功/失败反馈。

设计要点：
- 全局锁（threading.Lock）+ 状态查询：备份期间写操作一律拒绝，防止"断档"。
- 数据库绝不直接打包：先 WAL checkpoint + 复制到 temp/，副本无写，打包稳定。
- 恢复路径穿越防护：zip-slip 拦截 + Windows 路径重试 + 完整回滚。
"""
import io
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.config import settings
from app.infra.logger import get_logger
from app.repository import db as db_repo

logger = get_logger(__name__)

_UPLOADS_ARC_PREFIX = "uploads/"
_BACKUP_LOCK = threading.Lock()
_backup_holder: Optional[str] = None       # 当前备份持有者标识（调试用）
_BACKUP_STARTED_AT: Optional[datetime] = None

# 附件容量预警阈值：超过 5GB 时提示用户备份并清理历史附件
STORAGE_LIMIT_BYTES = 5 * 1024 ** 3
STORAGE_WARN4_BYTES = 4 * 1024 ** 3   # 4.0 GB 黄色预警
STORAGE_WARN5_BYTES = 5 * 1024 ** 3   # 5.0 GB 红色严重

# 日志截断策略：近 LOG_MAX_DAYS 天 或 单文件 LOG_MAX_BYTES 字节以内
_LOG_MAX_DAYS = 30
_LOG_MAX_BYTES = 10 * 1024 * 1024

# 恢复重试
_RENAME_ATTEMPTS = 5
_RENAME_DELAY = 0.4


# ==============================================================
# 全局备份锁（模块二·1 并发防护）
# ==============================================================

def is_backing_up() -> bool:
    """当前是否正在生成备份 / 恢复数据。写路径统一调用此函数拦截。"""
    return _BACKUP_LOCK.locked()


def assert_not_backing_up() -> None:
    """写路径统一入口：备份期间抛出异常，由 api 层转 503 + 友好提示。"""
    if _BACKUP_LOCK.locked():
        raise BackingUpError("系统正在生成备份，请稍候再操作")


class BackingUpError(Exception):
    """备份中 / 恢复中抛给调用方的业务异常（api 层识别后返回 503）。"""


@contextmanager
def _backup_guard(holder: str):
    """备份/恢复统一加锁上下文，保证整个流程原子化不被并发打断。"""
    global _backup_holder, _BACKUP_STARTED_AT
    acquired = _BACKUP_LOCK.acquire(blocking=False)
    if not acquired:
        raise BackingUpError("已有备份/恢复任务在进行，请稍候再试")
    _backup_holder = holder
    _BACKUP_STARTED_AT = datetime.now()
    logger.info("备份锁已获取: holder=%s", holder)
    try:
        yield
    finally:
        _BACKUP_STARTED_AT = None
        _backup_holder = None
        _BACKUP_LOCK.release()
        logger.info("备份锁已释放")


# ==============================================================
# 公共工具
# ==============================================================

def _human_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def storage_usage() -> dict:
    """设置页容量监控：统计 uploads/ 目录体积、文件数 + 清理候选。"""
    total, n_files = 0, 0
    if settings.upload_dir.exists():
        for p in settings.upload_dir.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
                    n_files += 1
            except OSError:
                continue
    # 回收站附件（deleted_at IS NOT NULL 且 source_path 非空）
    recycle_n, recycle_bytes = _scan_recycle_attachments()
    # 孤儿文件（uploads/ 目录里但数据库无任何记录引用）
    orphan_n, orphan_bytes = _scan_orphan_files()
    limit = STORAGE_LIMIT_BYTES
    warn4 = total >= STORAGE_WARN4_BYTES
    warn5 = total >= STORAGE_WARN5_BYTES
    return {
        "bytes": total,
        "files": n_files,
        "limit_bytes": limit,
        "human": _human_size(total),
        "limit_human": _human_size(limit),
        "percent": round(total / limit * 100, 1) if limit else 0,
        "warn4": warn4,        # 4GB 黄色预警
        "warn5": warn5,        # 5GB 红色严重
        "recycle": {"files": recycle_n, "bytes": recycle_bytes, "human": _human_size(recycle_bytes)},
        "orphan": {"files": orphan_n, "bytes": orphan_bytes, "human": _human_size(orphan_bytes)},
        "backing_up": is_backing_up(),
    }


def _scan_recycle_attachments() -> tuple:
    """扫描回收站中的发票附件（deleted_at IS NOT NULL 且 source_path 非空且文件还存在）。"""
    n, total = 0, 0
    try:
        with db_repo.read() as conn:
            rows = conn.execute(
                "SELECT source_path FROM invoice WHERE deleted_at IS NOT NULL "
                "AND source_path IS NOT NULL AND source_path != ''"
            ).fetchall()
        for (sp,) in rows:
            fp = _resolve_source_path(sp)
            if fp and fp.exists() and fp.is_file():
                try:
                    total += fp.stat().st_size
                    n += 1
                except OSError:
                    continue
    except Exception as e:
        logger.warning("扫描回收站附件失败: %s", e)
    return n, total


def _scan_orphan_files() -> tuple:
    """扫描 uploads/ 目录中数据库无记录引用的孤儿文件。"""
    n, total = 0, 0
    try:
        # 数据库里所有被引用的附件绝对路径
        known = set()
        with db_repo.read() as conn:
            for (sp,) in conn.execute(
                "SELECT source_path FROM invoice WHERE source_path IS NOT NULL AND source_path != ''"
            ).fetchall():
                fp = _resolve_source_path(sp)
                if fp:
                    known.add(str(fp.resolve()))
            # 报销单无附件字段，reimburse.source_path 可能不存在；跳过
        # 遍历 uploads/ 目录所有文件，排除 known
        if settings.upload_dir.exists():
            for p in settings.upload_dir.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    if str(p.resolve()) not in known:
                        total += p.stat().st_size
                        n += 1
                except OSError:
                    continue
    except Exception as e:
        logger.warning("扫描孤儿文件失败: %s", e)
    return n, total


def _resolve_source_path(sp: str) -> Optional[Path]:
    """把 source_path 解析为真实 Path（兼容绝对/相对路径）。"""
    if not sp:
        return None
    try:
        p = Path(sp)
        if p.is_absolute():
            return p
        cand = settings.upload_dir / p
        if cand.exists():
            return cand
        # 再按文件名匹配一次（数据库里只存了文件名）
        for f in settings.upload_dir.rglob(p.name):
            return f
        return cand
    except OSError:
        return None


def cleanup_attachments(recycle: bool = False, orphan: bool = False,
                        detach_original: bool = False) -> dict:
    """清理附件（按用户勾选的策略执行）。

    返回 {"ok": bool, "files_deleted": int, "bytes_freed": int, "human_freed": str,
          "recycle_deleted": int, "orphan_deleted": int, "detached": int, "message": str}。
    所有操作写审计日志。
    """
    from app.service import audit_service
    files_deleted = 0
    bytes_freed = 0
    recycle_del = orphan_del = detached = 0

    try:
        # 1. 清理孤儿文件（uploads/ 目录里但数据库无任何引用）
        if orphan:
            _, _ = (None, None)  # 先做一次新鲜扫描
            n, b = _delete_orphans()
            orphan_del, bytes_freed = n, bytes_freed + b
            files_deleted += n

        # 2. 清理回收站附件（物理删除 + 审计）
        if recycle:
            n, b = _delete_recycle_attachments()
            recycle_del, bytes_freed = n, bytes_freed + b
            files_deleted += n

        # 3. 仅清理原件（保留台账，更新 is_attachment_deleted）
        if detach_original:
            n, b = _detach_invoice_originals()
            detached, bytes_freed = n, bytes_freed + b
            files_deleted += n

        # 审计日志
        try:
            with db_repo.tx() as conn:
                audit_service.log(
                    conn, "attachment_cleanup", "system", "",
                    f"recycle={recycle_del}, orphan={orphan_del}, detached={detached}, "
                    f"files={files_deleted}, bytes={bytes_freed}"
                )
        except Exception as e:
            logger.warning("审计日志写入失败（清理动作本身已完成）: %s", e)

        msg = (f"清理完成，删除 {files_deleted} 个文件，"
               f"释放空间 {_human_size(bytes_freed)}")
        if recycle or orphan or detach_original:
            msg += f"（回收站 {recycle_del} / 孤儿 {orphan_del} / 仅原件 {detached}）"
        return {"ok": True, "files_deleted": files_deleted, "bytes_freed": bytes_freed,
                "human_freed": _human_size(bytes_freed), "recycle_deleted": recycle_del,
                "orphan_deleted": orphan_del, "detached": detached, "message": msg}
    except Exception as e:
        logger.exception("清理附件失败")
        return {"ok": False, "files_deleted": files_deleted, "bytes_freed": bytes_freed,
                "human_freed": _human_size(bytes_freed), "message": f"清理失败：{e}"}


def _delete_orphans() -> tuple:
    """物理删除孤儿文件。返回 (删除文件数, 释放字节)。"""
    n, total = 0, 0
    try:
        with db_repo.read() as conn:
            known = set()
            for (sp,) in conn.execute(
                "SELECT source_path FROM invoice WHERE source_path IS NOT NULL AND source_path != ''"
            ).fetchall():
                fp = _resolve_source_path(sp)
                if fp:
                    known.add(str(fp.resolve()))
        if settings.upload_dir.exists():
            for p in list(settings.upload_dir.rglob("*")):
                if not p.is_file():
                    continue
                try:
                    if str(p.resolve()) not in known:
                        sz = p.stat().st_size
                        p.unlink()
                        total += sz
                        n += 1
                except OSError:
                    continue
    except Exception as e:
        logger.warning("删除孤儿文件异常: %s", e)
    return n, total


def _delete_recycle_attachments() -> tuple:
    """物理删除回收站中的发票附件（invoice.deleted_at IS NOT NULL 的 source_path）。"""
    n, total = 0, 0
    try:
        files_to_delete = []  # 先收集再删，避免并发问题
        with db_repo.read() as conn:
            rows = conn.execute(
                "SELECT id, source_path FROM invoice "
                "WHERE deleted_at IS NOT NULL AND source_path IS NOT NULL AND source_path != ''"
            ).fetchall()
        for inv_id, sp in rows:
            fp = _resolve_source_path(sp)
            if fp and fp.exists() and fp.is_file():
                files_to_delete.append((inv_id, fp))
        for inv_id, fp in files_to_delete:
            try:
                sz = fp.stat().st_size
                fp.unlink()
                total += sz
                n += 1
            except OSError:
                continue
    except Exception as e:
        logger.warning("删除回收站附件异常: %s", e)
    return n, total


def _detach_invoice_originals() -> tuple:
    """仅删除当前未软删除发票的原件，数据库标记 is_attachment_deleted = 1。"""
    n, total = 0, 0
    try:
        files_to_delete = []
        with db_repo.read() as conn:
            rows = conn.execute(
                "SELECT id, source_path FROM invoice "
                "WHERE deleted_at IS NULL AND source_path IS NOT NULL AND source_path != ''"
            ).fetchall()
        for inv_id, sp in rows:
            fp = _resolve_source_path(sp)
            if fp and fp.exists() and fp.is_file():
                files_to_delete.append((inv_id, fp))
        # 先删文件，再批量 UPDATE（事务中）
        with db_repo.tx() as conn:
            for inv_id, fp in files_to_delete:
                try:
                    sz = fp.stat().st_size
                    fp.unlink()
                    conn.execute(
                        "UPDATE invoice SET is_attachment_deleted = 1 WHERE id = ?",
                        (inv_id,)
                    )
                    total += sz
                    n += 1
                except OSError:
                    continue
    except Exception as e:
        logger.warning("清理原件异常: %s", e)
    return n, total


def _rename_with_retry(src: Path, dst: Path, attempts: int = _RENAME_ATTEMPTS,
                       delay: float = _RENAME_DELAY) -> None:
    """改名重试：Windows 下文件/目录可能被瞬时占用（杀毒、索引器等）。"""
    last: OSError | None = None
    for _ in range(attempts):
        try:
            src.rename(dst)
            return
        except OSError as e:
            last = e
            time.sleep(delay)
    raise last


def _safe_members(zf: zipfile.ZipFile) -> tuple:
    """返回 (根目录 .db 成员名, uploads/ 下成员名列表, 其他文件成员列表)，拦截路径穿越。"""
    db_members, upload_members, extra_members = [], [], []
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
        else:
            extra_members.append(name)
    return db_members, upload_members, extra_members


# ==============================================================
# 模块一·二：备份（全量打包）
# ==============================================================

def create_backup() -> dict:
    """打包全量数据并触发浏览器下载前的信息准备。

    模块一覆盖清单：数据库、WAL/SHM、uploads/、config.json/.env、
                   logs/app.log（近 30 天或 10MB 内）。
    返回 {"zip_path": Path, "invoices": int, "reimburses": int,
          "attachments": int, "size_bytes": int, "size_human": str}。
    """
    with _backup_guard("create_backup"):
        return _do_backup()


def _do_backup() -> dict:
    backup_dir = settings.data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = backup_dir / f"system_backup_{stamp}.zip"
    temp_dir = settings.data_dir / "temp_backup"

    # 统计源数据库中的记录数（给前端反馈）
    invoices_n = reimburses_n = 0
    with db_repo.read() as conn:
        invoices_n = conn.execute(
            "SELECT COUNT(*) FROM invoice WHERE deleted_at IS NULL"
        ).fetchone()[0]
        reimburses_n = conn.execute(
            "SELECT COUNT(*) FROM reimburse WHERE deleted_at IS NULL"
        ).fetchone()[0]

    try:
        # 1. WAL checkpoint（模块二·2：确保所有内存数据已落盘）
        _wal_checkpoint()

        # 2. 复制 db 到 temp/ 作为打包副本（模块二·3：绝不能直接打正在使用的文件）
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_db = temp_dir / settings.db_path.name
        shutil.copy2(settings.db_path, temp_db)
        for suffix in ("-wal", "-shm"):
            side = settings.db_path.with_name(settings.db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, temp_dir / side.name)

        # 3. 打包副本 + uploads + config + 日志
        n_att = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 3a. 数据库副本
            for f in sorted(temp_dir.iterdir()):
                if f.is_file() and f.name.startswith(settings.db_path.name):
                    zf.write(f, arcname=f.name)
            # 3b. 附件目录（原样保留 YYYYMM 子目录层级）
            if settings.upload_dir.exists():
                for p in sorted(settings.upload_dir.rglob("*")):
                    if p.is_file():
                        arc = (_UPLOADS_ARC_PREFIX
                               + str(p.relative_to(settings.upload_dir)).replace("\\", "/"))
                        zf.write(p, arcname=arc)
                        n_att += 1
            # 3c. 配置文件（若存在）
            for extra_name in ("config.json", ".env"):
                cand = settings.data_dir.parent / extra_name
                if cand.exists() and cand.is_file():
                    zf.write(cand, arcname=extra_name)
            # 3d. 运行日志：近 30 天 + 单文件 ≤ 10MB
            _add_recent_logs(zf)

        size_bytes = zip_path.stat().st_size
        logger.info("备份完成: %s (invoices=%d, reimburse=%d, attachments=%d, size=%.1f KB)",
                    zip_path.name, invoices_n, reimburses_n, n_att, size_bytes / 1024)

        return {
            "zip_path": zip_path,
            "invoices": invoices_n,
            "reimburses": reimburses_n,
            "attachments": n_att,
            "size_bytes": size_bytes,
            "size_human": _human_size(size_bytes),
        }
    finally:
        # 模块二·4：无论成功失败，finally 里强制清临时目录
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            logger.warning("清理备份临时目录失败（可忽略）: %s", temp_dir)


def _wal_checkpoint() -> None:
    """执行 PRAGMA wal_checkpoint(TRUNCATE)，确保所有 WAL 页已落主文件并截断。"""
    try:
        conn = sqlite3.connect(str(settings.db_path), timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        logger.info("WAL checkpoint 完成")
    except sqlite3.Error as e:
        # checkpoint 失败不致命（可能数据库是 journal_mode=DELETE 无 WAL），继续备份
        logger.warning("WAL checkpoint 失败，继续备份: %s", e)


def _add_recent_logs(zf: zipfile.ZipFile) -> None:
    """打包 logs/ 目录下近 30 天或单文件 ≤ 10MB 的 app.log / app.log.N。"""
    if not settings.log_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=_LOG_MAX_DAYS)
    for p in sorted(settings.log_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".log", ".bak") and "log" not in p.name.lower():
            continue
        try:
            st = p.stat()
            if st.st_size > _LOG_MAX_BYTES:
                continue
            mtime = datetime.fromtimestamp(st.st_mtime)
            if mtime < cutoff:
                continue
            zf.write(p, arcname=str(Path("logs") / p.name))
            logger.debug("备份日志: %s", p.name)
        except OSError:
            continue


# ==============================================================
# 模块三·四：恢复（沙盒校验 + 原子替换 + 失败回滚）
# ==============================================================

def restore_backup(content: bytes) -> dict:
    """从 zip 备份覆盖恢复全部数据。

    返回 {"ok": bool, "message": str, "invoices": int, "reimburses": int,
          "attachments": int}。
    - dispose() 强制断连 + 中止后台清理线程。
    - 旧库/旧附件带时间戳改名保底，失败自动回滚。
    - 任何损坏（zip-slip / db 打不开 / 移动失败）都只返回失败，不碰现有数据。
    """
    with _backup_guard("restore_backup"):
        return _do_restore(content)


def _do_restore(content: bytes) -> dict:
    if not content or not zipfile.is_zipfile(io.BytesIO(content)):
        return {"ok": False, "message": "备份文件无效：不是合法的 .zip 文件"}

    db_path = settings.db_path
    upload_dir = settings.upload_dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_db = db_path.with_name(f"fapiao_old_{stamp}.db")
    bak_uploads = upload_dir.with_name(f"uploads_old_{stamp}")
    restore_dir = settings.data_dir / "temp_restore"

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            db_members, upload_members, _ = _safe_members(zf)
            if not db_members:
                return {"ok": False,
                        "message": "备份文件无效：压缩包内未找到数据库文件（.db）"}
            db_member = sorted(db_members)[0]

            # ① 强制释放所有数据库连接（Windows 文件占用防护）
            db_repo.dispose()
            time.sleep(0.5)  # 让 WAL 收敛

            # ② 旧数据改名保底（先清 WAL/SHM 副文件；重试应对瞬时占用）
            try:
                for suffix in ("-wal", "-shm"):
                    side = db_path.with_name(db_path.name + suffix)
                    if side.exists():
                        side.unlink()
                if bak_db.exists():
                    bak_db.unlink()
                if db_path.exists():
                    _rename_with_retry(db_path, bak_db)
                if bak_uploads.exists():
                    shutil.rmtree(bak_uploads, ignore_errors=True)
                if upload_dir.exists():
                    _rename_with_retry(upload_dir, bak_uploads)
            except OSError as e:
                logger.exception("恢复前置改名失败（数据库/附件被占用）")
                return {"ok": False,
                        "message": f"恢复失败：旧数据无法改名保底（{e}）。"
                                   f"请关闭可能占用的 Excel/资源管理器后重试。"}

            # ③ 沙盒解压到 temp_restore/ + 校验（模块三·2：绝不直接覆盖原目录）
            try:
                if restore_dir.exists():
                    shutil.rmtree(restore_dir, ignore_errors=True)
                restore_dir.mkdir(parents=True, exist_ok=True)
                # 解压数据库
                new_db = restore_dir / db_member
                with zf.open(db_member) as src, open(new_db, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # 解压附件（保留 uploads/ 前缀，便于整体重命名）
                for name in upload_members:
                    target = restore_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

                # 校验新数据库：能打开 + 有核心表
                probe = sqlite3.connect(new_db)
                try:
                    row = probe.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE "
                        "name IN ('invoice','reimburse') AND type='table'"
                    ).fetchone()
                    if (row or (0,))[0] < 2:
                        raise ValueError("备份数据库缺少核心表（invoice / reimburse）")
                    inv_n = probe.execute("SELECT COUNT(*) FROM invoice").fetchone()[0]
                    reb_n = probe.execute("SELECT COUNT(*) FROM reimburse").fetchone()[0]
                finally:
                    probe.close()

            except Exception as e:
                logger.exception("沙盒解压/校验失败，回滚旧数据")
                _rollback_all(db_path, bak_db, upload_dir, bak_uploads)
                return {"ok": False, "message": f"恢复失败，已自动回滚原数据：{e}"}

            # ④ 原子替换：新库 + 新附件移入主目录
            try:
                _rename_with_retry(new_db, db_path)
                if upload_members:
                    new_uploads = restore_dir / _UPLOADS_ARC_PREFIX.rstrip("/")
                    if new_uploads.exists():
                        _rename_with_retry(new_uploads, upload_dir)
                    else:
                        upload_dir.mkdir(parents=True, exist_ok=True)
                else:
                    upload_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.exception("原子替换失败（磁盘不足/权限问题），回滚")
                _rollback_all(db_path, bak_db, upload_dir, bak_uploads)
                return {"ok": False, "message": f"恢复失败，已自动回滚原数据：{e}"}

        # ⑤ 成功：重建 schema（幂等）
        try:
            db_repo.init_schema()
        except Exception as e:
            logger.warning("恢复后 init_schema 异常（不影响已恢复数据）: %s", e)

        # 统计附件数给前端展示
        att_n = 0
        if upload_dir.exists():
            for p in upload_dir.rglob("*"):
                if p.is_file():
                    att_n += 1

        logger.info("恢复成功（旧库 %s, 旧附件 %s；inv=%d, reb=%d, att=%d）",
                    bak_db.name, bak_uploads.name, inv_n, reb_n, att_n)
        return {
            "ok": True,
            "message": "数据恢复成功！请刷新页面或重启软件以加载最新数据。",
            "invoices": inv_n,
            "reimburses": reb_n,
            "attachments": att_n,
            "backup_db": bak_db.name,
            "backup_uploads": bak_uploads.name,
        }

    finally:
        # 模块三·6：恢复成功/失败都清沙盒目录
        try:
            if restore_dir.exists():
                shutil.rmtree(restore_dir, ignore_errors=True)
        except Exception:
            logger.warning("清理恢复沙盒目录失败（可忽略）: %s", restore_dir)


def _rollback_all(db_path: Path, bak_db: Path, upload_dir: Path,
                  bak_uploads: Path) -> None:
    """恢复失败时把数据库 + 附件都还原回备份前的状态（尽力而为）。"""
    # 先断连再改名
    try:
        db_repo.dispose()
    except Exception:
        pass

    # 数据库回滚
    try:
        if db_path.exists():
            db_path.unlink()
        if bak_db.exists():
            _rename_with_retry(bak_db, db_path)
        logger.info("回滚完成：数据库已恢复为备份前的状态")
    except Exception:
        logger.exception("数据库回滚失败，请手动将 %s 改回 %s", bak_db, db_path)

    # 附件目录回滚
    try:
        if upload_dir.exists():
            shutil.rmtree(upload_dir, ignore_errors=True)
        if bak_uploads.exists():
            _rename_with_retry(bak_uploads, upload_dir)
        logger.info("回滚完成：附件已恢复为备份前的状态")
    except Exception:
        logger.exception("附件回滚失败，请手动将 %s 改回 %s", bak_uploads, upload_dir)
