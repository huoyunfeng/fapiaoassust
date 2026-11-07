"""数据交接包（P2-3 企业版多设备数据交接，破除孤岛）。

子设备：导出自录的发票 + 报销单 + 关联附件为独立 zip。
主设备：导入交接包，按发票号/报销单号去重合并，绝不覆盖本机原始账目。

包结构：
  handover.json      —— {"kind": "handover", "app_version": ..., "invoices": [...], "reimburses": [...]}
  uploads/<文件名>   —— 发票关联的原始附件（按文件名平铺，import 时落入本机 uploads/）

安全边界：
- 仅导出未删除（deleted_at IS NULL）数据；匹配结果不随包走（导入后跑「重新匹配」即可）。
- 导入一律 INSERT，主键冲突/业务键重复 → 跳过，绝不 UPDATE 本机已有账目。
- 列级兼容：导入时按目标表 PRAGMA 实际列过滤字段，跨版本结构差异自动适配。
"""
import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from app.config import APP_VERSION, settings
from app.infra.logger import get_logger
from app.repository import db as db_repo
from app.service import audit_service, backup_service

logger = get_logger(__name__)

# 导出包保留份数（按修改时间留最新 N 份，避免 data/handover 无限增长）
_MAX_EXPORT_FILES = 10


# ==============================================================
# 导出（子设备）
# ==============================================================

def export_handover() -> dict:
    """导出交接包。返回 {"zip_path": Path, "invoices": n, "reimburses": n, "attachments": n}。"""
    backup_service.assert_not_backing_up()
    out_dir = settings.data_dir / "handover"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = out_dir / f"handover_{stamp}.zip"

    with db_repo.read() as conn:
        inv_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM invoice WHERE deleted_at IS NULL").fetchall()]
        reb_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM reimburse WHERE deleted_at IS NULL").fetchall()]

    # 附件：收集 source_path 对应的真实文件（平铺进包，按文件名）
    att_files = []
    seen_names = set()
    for r in inv_rows:
        fp = backup_service._resolve_source_path(r.get("source_path"))
        if fp and fp.exists() and fp.is_file() and fp.name not in seen_names:
            seen_names.add(fp.name)
            att_files.append(fp)

    payload = {
        "kind": "handover",
        "app_version": APP_VERSION,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "invoices": inv_rows,
        "reimburses": reb_rows,
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("handover.json",
                    json.dumps(payload, ensure_ascii=False, default=str))
        for fp in att_files:
            zf.write(fp, arcname=f"uploads/{fp.name}")

    logger.info("交接包导出: %s (inv=%d, reb=%d, att=%d)",
                zip_path.name, len(inv_rows), len(reb_rows), len(att_files))
    _prune_exports(out_dir)
    return {"zip_path": zip_path, "invoices": len(inv_rows),
            "reimburses": len(reb_rows), "attachments": len(att_files)}


def _prune_exports(out_dir: Path) -> None:
    """只保留最新 _MAX_EXPORT_FILES 份交接包导出文件（下载后旧包可安全清理）。"""
    try:
        files = sorted(out_dir.glob("handover_*.zip"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[_MAX_EXPORT_FILES:]:
            try:
                old.unlink()
                logger.info("清理历史交接包: %s", old.name)
            except OSError:
                continue
    except OSError:
        pass


# ==============================================================
# 导入（主设备）：去重合并，绝不覆盖
# ==============================================================

def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _inv_dup_where(row: dict) -> tuple:
    """发票去重键：优先发票号；无号退化为 (购买方,销售方,金额,开票日期)。"""
    no = (row.get("invoice_no") or "").strip()
    if no:
        return ("invoice_no = ? AND deleted_at IS NULL", (no,))
    return ("IFNULL(invoice_no,'') = '' AND IFNULL(buyer_name,'') = ? "
            "AND IFNULL(seller_name,'') = ? AND IFNULL(amount,'') = ? "
            "AND IFNULL(invoice_date,'') = ? AND deleted_at IS NULL",
            (row.get("buyer_name") or "", row.get("seller_name") or "",
             str(row.get("amount") or ""), row.get("invoice_date") or ""))


def _reb_dup_where(row: dict) -> tuple:
    """报销单去重键：优先单号；无单号退化为 (提交人,金额,报销日期)。"""
    no = (row.get("reimburse_no") or "").strip()
    if no:
        return ("reimburse_no = ? AND deleted_at IS NULL", (no,))
    return ("IFNULL(reimburse_no,'') = '' AND IFNULL(submitter,'') = ? "
            "AND IFNULL(amount,'') = ? AND IFNULL(reimburse_date,'') = ? "
            "AND deleted_at IS NULL",
            (row.get("submitter") or "", str(row.get("amount") or ""),
             row.get("reimburse_date") or ""))


def _attachment_target(blob: bytes, base: str) -> tuple:
    """确定附件在本机的落地路径，返回 (path, need_write)。

    同名且内容相同 → 直接复用本机已有文件（重复导入不产生副本）；
    同名但内容不同 → 追加 __dupeN 后缀另存，绝不覆盖本机原件。
    """
    def _same(p: Path) -> bool:
        try:
            from app.infra.winpath import read_bytes
            return os.path.getsize(fs_path(p)) == len(blob) and read_bytes(p) == blob
        except OSError:
            return False

    from app.infra.winpath import fs_path
    target = settings.upload_dir / base
    if not os.path.exists(fs_path(target)):
        return target, True
    if _same(target):
        return target, False
    i = 1
    while True:
        cand = settings.upload_dir / f"{target.stem}__dupe{i}{target.suffix}"
        if not os.path.exists(fs_path(cand)):
            return cand, True
        if _same(cand):
            return cand, False
        i += 1


def import_handover(content: bytes) -> dict:
    """导入交接包。返回 {ok, message, invoices_added/skipped, reimburses_added/skipped,
    attachments_added}。"""
    if not content or not zipfile.is_zipfile(io.BytesIO(content)):
        return {"ok": False, "message": "交接包无效：不是合法的 .zip 文件"}

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        # 沙盒读取 handover.json（不落盘，纯内存校验）
        try:
            payload = json.loads(zf.read("handover.json").decode("utf-8"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
            # 用户误传了全量备份包时给出准确指引
            try:
                m = json.loads(zf.read("manifest.json").decode("utf-8"))
                if m.get("kind") == "full-backup":
                    return {"ok": False,
                            "message": "文件类型不符：这是全量备份包，"
                                       "请使用上方「恢复数据」功能导入"}
            except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
                pass
            return {"ok": False, "message": "交接包无效：缺少 handover.json 或格式损坏"}
        if payload.get("kind") != "handover":
            return {"ok": False, "message": "文件类型不符：这是全量备份包，"
                                            "请使用「恢复数据」功能导入"}
        invoices = payload.get("invoices") or []
        reimburses = payload.get("reimburses") or []

        # 附件解压到 uploads/：同名同内容复用，同名不同内容改名保留（绝不覆盖本机原件）。
        # name_map 记录包内文件名 -> 本机实际路径，导入时回写 source_path。
        att_added = 0
        name_map = {}
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith("uploads/") or name.endswith("/"):
                continue
            if ".." in Path(name).parts:
                continue
            base = Path(name).name
            blob = zf.read(info)
            target, need_write = _attachment_target(blob, base)
            if need_write:
                from app.infra.winpath import write_bytes
                write_bytes(target, blob)
                att_added += 1
            name_map[base] = target

    inv_added = inv_skip = reb_added = reb_skip = 0
    with db_repo.tx() as conn:
        inv_cols = _table_columns(conn, "invoice") - {"id"}
        reb_cols = _table_columns(conn, "reimburse") - {"id"}

        for row in invoices:
            where, params = _inv_dup_where(row)
            if conn.execute(f"SELECT 1 FROM invoice WHERE {where} LIMIT 1",
                            params).fetchone():
                inv_skip += 1
                continue
            # 文件指纹也可能是同一张票（同文件改了票号）：按唯一索引口径再判一次
            fh = row.get("file_hash")
            if fh and conn.execute(
                    "SELECT 1 FROM invoice WHERE file_hash = ? AND deleted_at IS NULL LIMIT 1",
                    (fh,)).fetchone():
                inv_skip += 1
                continue
            data = {k: v for k, v in row.items() if k in inv_cols and k != "id"}
            data.pop("deleted_at", None)
            data.pop("matched", None)  # 匹配状态不随包走，导入后重跑匹配
            # 附件已落到本机 uploads/：source_path 改为本机真实路径，
            # 否则会指向子设备的绝对路径（预览/清理必然失败）
            base = Path(str(row.get("source_path") or "")).name
            if base and base in name_map:
                data["source_path"] = str(name_map[base])
            keys = list(data.keys())
            try:
                conn.execute(
                    f"INSERT INTO invoice ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [data[k] for k in keys])
            except sqlite3.IntegrityError:
                # 单行冲突（如发票代码+号重复）只跳过该行，不能让整包导入失败
                logger.warning("交接包发票行冲突，已跳过: %s", row.get("invoice_no") or row.get("file_hash"))
                inv_skip += 1
                continue
            inv_added += 1

        for row in reimburses:
            where, params = _reb_dup_where(row)
            if conn.execute(f"SELECT 1 FROM reimburse WHERE {where} LIMIT 1",
                            params).fetchone():
                reb_skip += 1
                continue
            data = {k: v for k, v in row.items() if k in reb_cols and k != "id"}
            data.pop("deleted_at", None)
            data.pop("matched", None)
            keys = list(data.keys())
            try:
                conn.execute(
                    f"INSERT INTO reimburse ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [data[k] for k in keys])
            except sqlite3.IntegrityError:
                logger.warning("交接包报销单行冲突，已跳过: %s", row.get("reimburse_no"))
                reb_skip += 1
                continue
            reb_added += 1

        audit_service.log(conn, "handover_import", "system",
                          detail=(f"inv+{inv_added}/skip{inv_skip}, "
                                  f"reb+{reb_added}/skip{reb_skip}, "
                                  f"att+{att_added}"))

    logger.info("交接包导入: inv+%d(skip%d) reb+%d(skip%d) att+%d",
                inv_added, inv_skip, reb_added, reb_skip, att_added)
    return {"ok": True,
            "message": f"交接包导入完成：新增发票 {inv_added} 张（重复跳过 {inv_skip}），"
                       f"新增报销单 {reb_added} 条（跳过 {reb_skip}），"
                       f"附件 {att_added} 个。导入数据未参与匹配，请执行「重新匹配」。",
            "invoices_added": inv_added, "invoices_skipped": inv_skip,
            "reimburses_added": reb_added, "reimburses_skipped": reb_skip,
            "attachments_added": att_added}
