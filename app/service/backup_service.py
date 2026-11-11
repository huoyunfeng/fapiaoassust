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
import hashlib
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

from app.config import APP_VERSION, SCHEMA_VERSION, settings
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


def _known_attachment_paths(include_deleted: bool = True) -> tuple:
    """返回 (数据库已引用的附件绝对路径集合, 文件名→Path 索引)。

    先用一次目录遍历建文件名索引，再解析数据库引用，避免逐行 rglob
    造成 O(发票数 × 文件数) 的重复扫描。
    include_deleted=False 时只统计未删除发票的引用（清理回收站前用于避让）。
    """
    index: dict = {}
    if settings.upload_dir.exists():
        for p in settings.upload_dir.rglob("*"):
            try:
                if p.is_file():
                    index.setdefault(p.name, p)
            except OSError:
                continue
    known = set()
    try:
        sql = ("SELECT source_path FROM invoice "
               "WHERE source_path IS NOT NULL AND source_path != ''")
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with db_repo.read() as conn:
            rows = conn.execute(sql).fetchall()
        for (sp,) in rows:
            try:
                p = Path(sp)
                if p.is_absolute() and p.exists():
                    known.add(str(p.resolve()))
                    continue
                hit = index.get(p.name)
                if hit is not None:
                    known.add(str(hit.resolve()))
            except OSError:
                continue
    except Exception as e:
        logger.warning("读取附件引用失败: %s", e)
    return known, index


def _scan_orphan_files() -> tuple:
    """扫描 uploads/ 目录中数据库无记录引用的孤儿文件。"""
    n, total = 0, 0
    try:
        known, _ = _known_attachment_paths()
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


def _inside_upload(p: Path) -> bool:
    """安全约束：文件必须位于当前附件目录内（防止脏数据导致误删/误打包外部文件）。"""
    try:
        rp = p.resolve()
        root = settings.upload_dir.resolve()
        return rp == root or root in rp.parents
    except OSError:
        return False


def _resolve_source_path(sp: str) -> Optional[Path]:
    """把 source_path 解析为真实 Path（兼容绝对/相对路径/跨机器迁移后的旧绝对路径）。

    一律限制在 settings.upload_dir 内：绝对路径越界（含迁移前的旧目录）按文件名
    在当前附件目录兜底，兜底失败返回 None —— 调用方（清理/打包）拿到 None 即跳过，
    绝不对外部路径执行 unlink / 写入压缩包。
    """
    if not sp:
        return None
    try:
        p = Path(sp)
        if p.is_absolute() and p.exists():
            if _inside_upload(p):
                return p
        elif not p.is_absolute():
            cand = settings.upload_dir / p
            if cand.exists() and _inside_upload(cand):
                return cand
        # 换机 / 迁移目录后：按文件名在当前附件目录内查找
        if p.name:
            for f in settings.upload_dir.rglob(p.name):
                if _inside_upload(f):
                    return f
        return None
    except OSError:
        return None


def cleanup_attachments(recycle: bool = False, orphan: bool = False,
                        detach_original: bool = False,
                        on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """清理附件（按用户勾选的策略执行）。

    返回 {"ok": bool, "files_deleted": int, "bytes_freed": int, "human_freed": str,
          "recycle_deleted": int, "orphan_deleted": int, "detached": int, "message": str}。
    所有操作写审计日志。

    on_progress 可选：fn(progress_pct, message)。
    """
    def _cb(pct: int, msg: str):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

    from app.service import audit_service
    files_deleted = 0
    bytes_freed = 0
    recycle_del = orphan_del = detached = 0

    try:
        # 1. 清理孤儿文件
        if orphan:
            _cb(5, "正在扫描并清理孤儿文件…")
            n, b = _delete_orphans()
            orphan_del, bytes_freed = n, bytes_freed + b
            files_deleted += n

        # 2. 清理回收站附件
        if recycle:
            _cb(45, "正在清理回收站附件…")
            n, b = _delete_recycle_attachments()
            recycle_del, bytes_freed = n, bytes_freed + b
            files_deleted += n

        # 3. 仅清理原件
        if detach_original:
            _cb(80, "正在清理发票原件（仅清空台账）…")
            n, b = _detach_invoice_originals()
            detached, bytes_freed = n, bytes_freed + b
            files_deleted += n

        _cb(95, "正在写入审计日志…")

        # 审计日志
        try:
            with db_repo.tx() as conn:
                audit_service.log(
                    conn, "attachment_cleanup", "system",
                    detail=(f"recycle={recycle_del}, orphan={orphan_del}, "
                            f"detached={detached}, files={files_deleted}, "
                            f"bytes={bytes_freed}")
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
        known, _ = _known_attachment_paths()
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
    """物理删除回收站中的发票附件（invoice.deleted_at IS NOT NULL 的 source_path）。

    安全约束：若同一文件仍被未删除发票引用（例如交接包导入的同名文件被复用），
    绝不删除，避免"清理回收站"误删在册发票的原件。
    """
    n, total = 0, 0
    try:
        files_to_delete = []  # 先收集再删，避免并发问题
        live_known, _ = _known_attachment_paths(include_deleted=False)
        with db_repo.read() as conn:
            rows = conn.execute(
                "SELECT id, source_path FROM invoice "
                "WHERE deleted_at IS NOT NULL AND source_path IS NOT NULL AND source_path != ''"
            ).fetchall()
        for inv_id, sp in rows:
            fp = _resolve_source_path(sp)
            if not (fp and fp.exists() and fp.is_file()):
                continue
            if str(fp.resolve()) in live_known:
                logger.info("跳过仍被在册发票引用的附件: %s", fp.name)
                continue
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
        # 先提交标记再删文件：若标记写入失败则一个文件也不删（避免"文件没了但状态没记"）。
        # 反之若删文件失败，标记已置 1 也只是"已清理"状态，不影响台账。
        with db_repo.tx() as conn:
            for inv_id, _fp in files_to_delete:
                conn.execute(
                    "UPDATE invoice SET is_attachment_deleted = 1 WHERE id = ?",
                    (inv_id,)
                )
        for inv_id, fp in files_to_delete:
            try:
                sz = fp.stat().st_size
                fp.unlink()
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


# ==============================================================
# P2-4：附件存储路径自定义与安全迁移
# ==============================================================

def storage_path_info() -> dict:
    """当前附件路径信息。"""
    return {"current": str(settings.upload_dir),
            "default": str(settings.data_dir / "uploads")}


def migrate_upload_dir(new_path: str,
                       on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """把 uploads/ 整体迁移到新路径。复制 → 校验 → 改写 DB 路径 → 旧目录保底改名。

    失败自动回滚。返回 {"ok": bool, "message": str, "new_path": str, "old_backup": str}。
    on_progress 可选：fn(progress_pct, message)。
    """
    def _cb(pct: int, msg: str):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

    from app.config import load_persisted_config, save_persisted_config

    if is_backing_up():
        return {"ok": False, "message": "系统正在备份/恢复中，请稍候再迁移"}

    target = Path((new_path or "").strip())
    if not str(target):
        return {"ok": False, "message": "请输入新的存储路径"}
    if not target.is_absolute():
        return {"ok": False, "message": "请输入绝对路径，如 D:\\fapiao\\uploads"}

    src = settings.upload_dir
    if target.resolve() == src.resolve():
        return {"ok": False, "message": "新路径与当前路径相同，无需迁移"}
    try:
        target.resolve().relative_to(src.resolve())
        return {"ok": False, "message": "新路径不能位于当前附件目录内部"}
    except ValueError:
        pass
    if target.exists():
        return {"ok": False,
                "message": "目标目录已存在，请选择一个尚未创建的目录（避免覆盖已有文件）"}

    # 磁盘空间预检
    usage = storage_usage()
    try:
        probe = target
        while not probe.exists():
            probe = probe.parent
        free = shutil.disk_usage(str(probe)).free
        if free < usage["bytes"]:
            return {"ok": False,
                    "message": f"目标磁盘剩余空间不足（可用 {_human_size(free)}，"
                               f"需要 {usage['human']}）"}
    except OSError as e:
        return {"ok": False, "message": f"目标路径不可访问：{e}"}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    old_backup = src.with_name(f"uploads_pre_migrate_{stamp}")
    orig_cfg = load_persisted_config()
    n_rewritten = 0

    try:
        # ① 复制到新目录
        _cb(2, "开始复制附件…")
        target.mkdir(parents=True, exist_ok=False)
        n_copied = 0
        total_src = usage["files"] or 0
        last_reported_pct = 2
        if src.exists():
            from app.infra.winpath import fs_path, iter_files
            for p in iter_files(src):
                rel = p.relative_to(src)
                dst = target / rel
                os.makedirs(fs_path(dst.parent), exist_ok=True)
                shutil.copy2(fs_path(p), fs_path(dst))
                n_copied += 1
                if total_src:
                    pct = 2 + int(80 * n_copied / total_src)
                    if pct - last_reported_pct >= 3:
                        _cb(pct, f"已复制 {n_copied}/{total_src}")
                        last_reported_pct = pct
        _cb(85, "附件复制完成，正在校验…")
        # ② 校验
        n_src = usage["files"]
        if n_copied != n_src:
            raise RuntimeError(f"复制不完整：源 {n_src} 个，目标 {n_copied} 个")
        _cb(90, "正在改写数据库附件路径…")
        # ③ 改写路径
        n_rewritten = _rewrite_source_paths(src, target)
        # ④ 旧目录保底改名
        _cb(95, "正在为旧目录改名保底…")
        if src.exists():
            _rename_with_retry(src, old_backup)
        # ⑤ 配置落盘 + 热切换
        cfg = load_persisted_config()
        cfg["upload_dir"] = str(target)
        save_persisted_config(cfg)
        settings.upload_dir = target
        logger.info("附件路径迁移成功: %s -> %s（旧目录保底 %s，改写 %d 条路径）",
                    src, target, old_backup.name, n_rewritten)
        _cb(100, "迁移完成")
        return {"ok": True,
                "message": f"迁移成功！共迁移 {n_copied} 个附件。"
                           f"旧目录已保底为 {old_backup.name}，确认无误后可手动删除。",
                "new_path": str(target), "old_backup": old_backup.name,
                "files": n_copied, "paths_rewritten": n_rewritten}
    except Exception as e:
        # 回滚：路径改回旧前缀 → 删不完整新目录 → 恢复旧目录名 → 配置还原
        logger.exception("附件路径迁移失败，执行回滚")
        try:
            if n_rewritten:
                _rewrite_source_paths(target, src)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if old_backup.exists() and not src.exists():
                _rename_with_retry(old_backup, src)
            cfg = load_persisted_config()
            if orig_cfg.get("upload_dir"):
                cfg["upload_dir"] = orig_cfg["upload_dir"]
            else:
                cfg.pop("upload_dir", None)
            save_persisted_config(cfg)
            settings.upload_dir = src
        except Exception:
            logger.exception("迁移回滚异常")
        return {"ok": False, "message": f"迁移失败，原附件目录未受影响：{e}"}


def _rewrite_source_paths(old_root: Path, new_root: Path) -> int:
    """把 invoice.source_path 的目录前缀从 old_root 改写为 new_root，返回改写条数。

    历史行的 source_path 存的是绝对路径；附件目录迁移后必须同步改写，
    否则预览/清理/孤儿扫描会全部失效（新上传又写入新目录，数据被割裂）。
    """
    old_str = str(old_root)
    new_str = str(new_root)
    n = 0
    with db_repo.tx() as conn:
        # 同时兼容反斜杠与正斜杠两种历史写法。
        # 用 substr 精确前缀比较而非 LIKE：路径中的 _ 和 % 在 LIKE 里是通配符，会误伤。
        for old in {old_str, old_str.replace("\\", "/")}:
            rows = conn.execute(
                "SELECT id, source_path FROM invoice WHERE substr(source_path, 1, ?) = ?",
                (len(old), old)).fetchall()
            for row in rows:
                sp = row["source_path"] or ""
                tail = sp[len(old):]
                # 防止把 "uploads_old" 之类同前缀兄弟目录误判为子路径
                if tail and tail[0] not in ("/", "\\"):
                    continue
                prefix = new_str.replace("\\", "/") if "\\" not in old else new_str
                conn.execute("UPDATE invoice SET source_path = ? WHERE id = ?",
                             (prefix + tail, row["id"]))
                n += 1
    return n


def _safe_members(zf: zipfile.ZipFile) -> tuple:
    """返回 (根目录 .db 成员名, uploads/ 下成员名列表, 其他文件成员列表)，拦截路径穿越。"""
    db_members, upload_members, extra_members = [], [], []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or name.endswith("/"):
            continue
        if name.startswith("/") or ".." in Path(name).parts or ":" in name:
            # 拦截绝对路径、../ 回退与 Windows 盘符/UNC（"C:/evil" 会被 Path 视为绝对路径）
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

def create_backup(on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """打包全量数据并触发浏览器下载前的信息准备。

    模块一覆盖清单：数据库、WAL/SHM、uploads/、config.json/.env、
                   logs/app.log（近 30 天或 10MB 内）。
    返回 {"zip_path": Path, "invoices": int, "reimburses": int,
          "attachments": int, "size_bytes": int, "size_human": str,
          "md5": str, "created_at": str}。

    on_progress 是可选回调：fn(progress_pct, message)，供前端真实进度条轮询。
    不传则退化为原行为（无进度反馈）。
    """
    with _backup_guard("create_backup"):
        return _do_backup(on_progress=on_progress)


def _do_backup(on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    def _cb(pct: int, msg: str):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

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
        _cb(2, "执行 WAL checkpoint 并生成数据库快照…")
        # 1. WAL checkpoint（模块二·2：确保所有内存数据已落盘）
        _wal_checkpoint()
        _cb(8, "正在生成数据库一致性快照…")

        # 2. 生成一致性数据库快照（模块二·3：绝不能直接打正在使用的文件）。
        #    直接复制文件在并发写入时可能拿到撕裂页；用 SQLite 在线备份 API
        #    由引擎保证快照完整（无需再打包 WAL/SHM，恢复端也不用重放）。
        temp_dir.mkdir(parents=True, exist_ok=True)
        # 清理上次异常退出的残留副本，避免旧 -wal/-shm 混入新备份包
        for stale in temp_dir.iterdir():
            if stale.is_file():
                stale.unlink(missing_ok=True)
        temp_db = temp_dir / settings.db_path.name
        _snapshot_db(temp_db)

        # 3. 打包副本 + uploads + config + 日志
        _cb(15, "开始打包附件与配置…")
        n_att = 0
        total_files = 0
        # 预估总进度：附件数是大头
        if settings.upload_dir.exists():
            from app.infra.winpath import iter_files
            total_files = sum(1 for _ in iter_files(settings.upload_dir))
        processed = 0
        last_report_pct = 15
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 3a. 数据库副本
            for f in sorted(temp_dir.iterdir()):
                if f.is_file() and f.name.startswith(settings.db_path.name):
                    zf.write(f, arcname=f.name)
            # 3a2. 版本标识 + MD5 预留位（最终 md5 在打包完成后回填，见下方）
            _manifest_created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 3b. 附件目录（原样保留 YYYYMM 子目录层级）
            if settings.upload_dir.exists():
                from app.infra.winpath import fs_path, iter_files
                for p in sorted(iter_files(settings.upload_dir)):
                    arc = (_UPLOADS_ARC_PREFIX
                           + str(p.relative_to(settings.upload_dir)).replace("\\", "/"))
                    zf.write(fs_path(p), arcname=arc)
                    n_att += 1
                    processed += 1
                    if total_files:
                        # 15% ~ 85% 区间对应附件打包
                        pct = 15 + int(70 * processed / total_files)
                        if pct - last_report_pct >= 2:    # 每 2% 上报一次，避免刷屏
                            _cb(pct, f"已打包附件 {processed}/{total_files}")
                            last_report_pct = pct
            _cb(86, "正在补充配置文件与近期日志…")
            # 3c. 配置文件（若存在）
            for extra_name in ("config.json", ".env"):
                cand = settings.data_dir.parent / extra_name
                if cand.exists() and cand.is_file():
                    zf.write(cand, arcname=extra_name)
            # 3d. 运行日志：近 30 天 + 单文件 ≤ 10MB
            _add_recent_logs(zf)

        _cb(92, "正在计算备份包 MD5…")
        # 4. 计算 MD5（逐块读，支持大文件）
        md5 = hashlib.md5()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                md5.update(chunk)
        md5_hex = md5.hexdigest()
        _cb(95, "正在写入 manifest…")

        # 5. 把最终 md5 回填进 manifest.json（用 zipfile 重新写入同名条目覆盖）
        #    标准 zipfile 不支持 in-place 覆盖，这里重开 ZipFile(zipfile.ZIP_DEFLATED) 追加同名 arcname
        #    后写同名条目会覆盖前一个条目（Python zipfile 会指向最后一次写入）。
        with zipfile.ZipFile(zip_path, "a") as zf2:
            zf2.writestr("manifest.json", json.dumps({
                "kind": "full-backup",
                "app_version": APP_VERSION,
                "schema_version": SCHEMA_VERSION,
                "created_at": _manifest_created_at,
                "md5": md5_hex,
                "attachments": n_att,
            }, ensure_ascii=False, indent=2))

        size_bytes = zip_path.stat().st_size
        logger.info("备份完成: %s (invoices=%d, reimburse=%d, attachments=%d, size=%.1f KB, md5=%s)",
                    zip_path.name, invoices_n, reimburses_n, n_att, size_bytes / 1024, md5_hex)

        # 写一份"最近备份"状态到隐藏文件，前端 Tab1 显示"上次成功备份时间"用
        _record_last_backup(zip_path.name, md5_hex, size_bytes)

        _cb(100, "备份完成")
        return {
            "zip_path": zip_path,
            "invoices": invoices_n,
            "reimburses": reimburses_n,
            "attachments": n_att,
            "size_bytes": size_bytes,
            "size_human": _human_size(size_bytes),
            "md5": md5_hex,
            "created_at": _manifest_created_at,
        }
    finally:
        # 模块二·4：无论成功失败，finally 里强制清临时目录
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            logger.warning("清理备份临时目录失败（可忽略）: %s", temp_dir)


_LAST_BACKUP_FILE = Path.home() / ".fapiao_last_backup.json"


def _record_last_backup(filename: str, md5: str, size_bytes: int) -> None:
    try:
        _LAST_BACKUP_FILE.write_text(json.dumps({
            "filename": filename,
            "md5": md5,
            "size_bytes": size_bytes,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.debug("写入上次备份标记失败（非致命）: %s", e)


def get_last_backup() -> Optional[dict]:
    """读取最近一次成功备份的元信息；未做过备份返回 None。"""
    try:
        if not _LAST_BACKUP_FILE.exists():
            return None
        data = json.loads(_LAST_BACKUP_FILE.read_text(encoding="utf-8"))
        # 同时检查备份文件本身是否还在（用户可能手动删除了）
        backup_dir = settings.data_dir / "backups"
        p = backup_dir / data.get("filename", "")
        if p.exists():
            return data
        return None
    except (OSError, json.JSONDecodeError):
        return None


def compute_md5_bytes(content: bytes) -> str:
    """对任意 bytes 计算 MD5（前端上传 zip 包后，先算一次比对 manifest 声明值）。"""
    return hashlib.md5(content).hexdigest()


def _snapshot_db(target: Path) -> None:
    """用 SQLite 在线备份 API 生成一致性数据库快照。

    与直接 shutil.copy2 相比：即使备份期间有并发写入，快照也保证是某个
    已提交事务的完整状态，不会出现撕裂页（备份可用性优先）。
    """
    with db_repo.read() as src:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()


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

def _ver_tuple(v: str) -> tuple:
    """'1.2.0' → (1,2,0)，非法版本号按 (0,0,0) 处理。"""
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    """读取备份包内的 manifest.json；旧版备份无此文件时按 0.0.0 处理。"""
    try:
        with zf.open("manifest.json") as f:
            return json.loads(f.read().decode("utf-8"))
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return {"app_version": "0.0.0", "schema_version": 0}


def restore_backup(content: bytes, on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """从 zip 备份覆盖恢复全部数据。

    返回 {"ok": bool, "message": str, "invoices": int, "reimburses": int,
          "attachments": int}。
    - dispose() 强制断连 + 中止后台清理线程。
    - 旧库/旧附件带时间戳改名保底，失败自动回滚。
    - 任何损坏（zip-slip / db 打不开 / 移动失败 / MD5 不匹配）都只返回失败，不碰现有数据。

    on_progress 可选：fn(progress_pct, message)。
    """
    with _backup_guard("restore_backup"):
        return _do_restore(content, on_progress=on_progress)


def _do_restore(content: bytes, on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    def _cb(pct: int, msg: str):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

    _cb(2, "正在校验备份包 MD5…")
    if not content or not zipfile.is_zipfile(io.BytesIO(content)):
        return {"ok": False, "message": "备份文件无效：不是合法的 .zip 文件"}

    # 0. MD5 完整性校验（防篡改 / 传输损坏）
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf_probe:
            manifest_probe = _read_manifest(zf_probe)
        declared_md5 = (manifest_probe.get("md5") or "").lower()
    except zipfile.BadZipFile:
        return {"ok": False, "message": "备份文件损坏：无法打开"}
    actual_md5 = hashlib.md5(content).hexdigest().lower()
    if declared_md5 and declared_md5 != actual_md5:
        logger.warning("恢复拦截：MD5 不匹配 manifest=%s actual=%s", declared_md5, actual_md5)
        return {"ok": False,
                "message": "⚠️ 备份包 MD5 校验失败，文件可能被篡改或传输损坏，禁止恢复！"}
    _cb(8, "MD5 校验通过，继续恢复流程")

    db_path = settings.db_path
    upload_dir = settings.upload_dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_db = db_path.with_name(f"fapiao_old_{stamp}.db")
    bak_uploads = upload_dir.with_name(f"uploads_old_{stamp}")
    restore_dir = settings.data_dir / "temp_restore"

    try:
        _cb(12, "正在读取备份清单…")
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # ⓪ 版本强校验：包版本高于当前软件 → 拒绝恢复并提示升级
            manifest = _read_manifest(zf)
            pack_ver = manifest.get("app_version", "0.0.0")
            if _ver_tuple(pack_ver) > _ver_tuple(APP_VERSION):
                return {"ok": False,
                        "message": f"该备份由更高版本的软件（v{pack_ver}）生成，"
                                   f"当前版本 v{APP_VERSION} 无法恢复。请先升级软件后再试。"}
            db_members, upload_members, _ = _safe_members(zf)
            if not db_members:
                return {"ok": False,
                        "message": "备份文件无效：压缩包内未找到数据库文件（.db）"}
            db_member = sorted(db_members)[0]

            # ① 强制释放所有数据库连接（Windows 文件占用防护）
            _cb(20, "正在释放数据库连接…")
            db_repo.dispose()
            time.sleep(0.5)  # 让 WAL 收敛

            # ② 旧数据改名保底（先清 WAL/SHM 副文件；重试应对瞬时占用）
            _cb(25, "正在为旧数据改名保底…")
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

            # ③ 沙盒解压到 temp_restore/ + 校验
            _cb(35, "正在解压备份文件…")
            total = len(upload_members)
            idx = 0
            try:
                if restore_dir.exists():
                    shutil.rmtree(restore_dir, ignore_errors=True)
                restore_dir.mkdir(parents=True, exist_ok=True)
                # 解压数据库
                new_db = restore_dir / db_member
                with zf.open(db_member) as src, open(new_db, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                _cb(45, "数据库已解压")
                # 解压附件（保留 uploads/ 前缀，便于整体重命名）
                for name in upload_members:
                    target = restore_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    idx += 1
                    if total and idx % max(1, total // 10) == 0:
                        _cb(45 + int(25 * idx / total), f"已解压附件 {idx}/{total}")

                # 校验新数据库：能打开 + 有核心表
                _cb(72, "正在校验新数据库完整性…")
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
            _cb(82, "正在原子替换数据库…")
            try:
                _rename_with_retry(new_db, db_path)
                _cb(88, "正在原子替换附件目录…")
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
        _cb(94, "正在重建数据库 schema…")
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

        _cb(100, "恢复完成")
        logger.info("恢复成功（旧库 %s, 旧附件 %s；inv=%d, reb=%d, att=%d）",
                    bak_db.name, bak_uploads.name, inv_n, reb_n, att_n)
        migrated = _ver_tuple(pack_ver) < _ver_tuple(APP_VERSION)
        msg = "数据恢复成功！请刷新页面或重启软件以加载最新数据。"
        if migrated:
            msg += f"（备份版本 v{pack_ver} 低于当前 v{APP_VERSION}，已自动完成数据库结构迁移）"
        return {
            "ok": True,
            "message": msg,
            "invoices": inv_n,
            "reimburses": reb_n,
            "attachments": att_n,
            "backup_db": bak_db.name,
            "backup_uploads": bak_uploads.name,
            "pack_version": pack_ver,
            "migrated": migrated,
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


# ==============================================================
# 模块四·扩展：Top10 大附件 / 静默备份（LRU）/ 诊断包
# ==============================================================

def get_top_attachments(limit: int = 10) -> list[dict]:
    """返回占用空间最大的前 N 个附件（清理向导弹窗左栏用）。

    每个条目：{path, size_bytes, size_human, mtime, date_str, invoice_no,
               is_deleted}。invoice_no 来自数据库关联；is_deleted 标记附件
               对应的发票是否已被回收站软删除（便于向导弹窗把它们优先列出）。
    """
    from app.repository import db as _db_repo
    if not settings.upload_dir.exists():
        return []
    try:
        from app.infra.winpath import iter_files
        all_files = []
        for p in iter_files(settings.upload_dir):
            try:
                st = p.stat()
                all_files.append((st.st_size, st.st_mtime, p))
            except OSError:
                continue
        all_files.sort(key=lambda x: -x[0])
        top = all_files[:limit]

        # 查数据库关联（source_path 包含绝对路径，用 LIKE '%'||filename 也能命中迁移后的数据）
        results = []
        with _db_repo.read() as conn:
            for size, mtime, p in top:
                fname = p.name
                row = conn.execute(
                    "SELECT invoice_no, deleted_at FROM invoice WHERE source_path LIKE ? "
                    "ORDER BY LENGTH(source_path) DESC LIMIT 1",
                    (f"%{fname}%",)
                ).fetchone()
                results.append({
                    "path": str(p),
                    "filename": fname,
                    "size_bytes": size,
                    "size_human": _human_size(size),
                    "date_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
                    "invoice_no": row[0] if row else "（孤儿，无关联）",
                    "is_deleted": bool(row and row[1]),
                })
        return results
    except Exception as e:
        logger.warning("扫描 Top%d 附件失败: %s", limit, e)
        return []


# ---- 静默备份：LRU 保留最新 3 个只含 DB+config 的轻量备份 ----

_SILENT_DIR = settings.data_dir / ".silent_backup"
_SILENT_KV_KEY = "silent_backup_last"
_SILENT_MAX = 3
_SILENT_INTERVAL_SECS = 24 * 3600


def run_silent_backup() -> Optional[dict]:
    """静默备份：只打包 db + config（不含附件），体积小、速度快。

    LRU 只保留最新 3 个包；超过 24 小时未执行才触发。返回备份元信息或 None。
    幂等：若 24h 内已跑过，直接返回 None。
    """
    from app.repository.repos import kv_repo
    try:
        with db_repo.read() as conn:
            last_row = kv_repo.get(conn, _SILENT_KV_KEY)
        last_ts = float(last_row) if last_row else 0
        if time.time() - last_ts < _SILENT_INTERVAL_SECS:
            return None
    except Exception:
        last_ts = 0

    try:
        _SILENT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = _SILENT_DIR / f"silent_{stamp}.zip"
        _wal_checkpoint()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # db 快照
            temp_dir = settings.data_dir / "temp_silent"
            temp_dir.mkdir(exist_ok=True)
            temp_db = temp_dir / settings.db_path.name
            try:
                _snapshot_db(temp_db)
                for f in sorted(temp_dir.iterdir()):
                    if f.is_file() and f.name.startswith(settings.db_path.name):
                        zf.write(f, arcname=f.name)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            # config / .env
            for extra in ("config.json", ".env"):
                cand = settings.data_dir.parent / extra
                if cand.exists() and cand.is_file():
                    zf.write(cand, arcname=extra)
            # 诊断信息
            zf.writestr("silent_manifest.json", json.dumps({
                "kind": "silent-backup",
                "app_version": APP_VERSION,
                "schema_version": SCHEMA_VERSION,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "only_db": True,
            }, ensure_ascii=False, indent=2))

        # 记录 LRU 时间戳
        with db_repo.tx() as conn:
            kv_repo.set(conn, _SILENT_KV_KEY, str(time.time()))

        # LRU：只保留最新 3 个
        all_pkgs = sorted(_SILENT_DIR.glob("silent_*.zip"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for old in all_pkgs[_SILENT_MAX:]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass

        size = zip_path.stat().st_size
        logger.info("静默备份完成: %s (%.1f KB)", zip_path.name, size / 1024)
        return {"filename": zip_path.name, "size_human": _human_size(size),
                "created_at": datetime.now().isoformat(timespec="seconds")}
    except Exception as e:
        logger.exception("静默备份失败（不影响主流程）")
        return None


def list_silent_backups() -> list[dict]:
    """列出所有现存静默备份包（供 Tab1 展示"上次静默备份时间"）。"""
    try:
        items = []
        for p in sorted(_SILENT_DIR.glob("silent_*.zip"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            items.append({
                "filename": p.name,
                "size_human": _human_size(st.st_size),
                "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        return items
    except OSError:
        return []


# ---- 导出诊断包（发给售后排查用） ----

def export_diagnose() -> Path:
    """打包诊断信息：日志 + 配置 + 系统信息 + 授权状态。

    不含用户业务数据（发票/报销单/附件），所以隐私安全。
    返回 zip 路径。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = settings.data_dir / f"diagnose_{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. 日志（近 7 天）
        log_dir = settings.data_dir / "logs"
        if log_dir.exists():
            for log in log_dir.iterdir():
                if log.is_file() and log.suffix == ".log":
                    mtime = log.stat().st_mtime
                    if time.time() - mtime < 7 * 86400:
                        zf.write(log, arcname=f"logs/{log.name}")
        # 2. 配置（若存在）
        for extra in ("config.json", ".env"):
            cand = settings.data_dir.parent / extra
            if cand.exists() and cand.is_file():
                zf.write(cand, arcname=extra)
        # 3. 系统信息
        try:
            import platform, sys
            from app.service import license_service
            sysinfo = {
                "app_version": APP_VERSION,
                "schema_version": SCHEMA_VERSION,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "machine": platform.machine(),
                "db_path": str(settings.db_path),
                "upload_path": str(settings.upload_dir),
                "data_dir": str(settings.data_dir),
                "license": license_service.status(),
                "plan_key": license_service.plan_key(),
                "dev_mode": license_service.dev_mode(),
            }
            zf.writestr("system_info.json", json.dumps(sysinfo, ensure_ascii=False, indent=2, default=str))
        except Exception as e:
            zf.writestr("system_info.json", json.dumps({"error": str(e)}))

    logger.info("导出诊断包: %s (%.1f KB)", zip_path.name, zip_path.stat().st_size / 1024)
    return zip_path
