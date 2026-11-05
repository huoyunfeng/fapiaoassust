"""多设备数据交接包：子设备导出 zip（台账 + 附件），主设备导入并去重合并。"""
import json
import time
import zipfile
from io import BytesIO
from pathlib import Path

from app.infra.filestore import FileStore
from app.repository import db
from app.repository.repos import invoice_repo, reimburse_repo

_VERSION = 1
_INVOICE_INSERT_FIELDS = set(invoice_repo.INSERT_FIELDS)
_REIMBURSE_INSERT_FIELDS = set(reimburse_repo.INSERT_FIELDS)


def counts() -> dict:
    with db.read() as conn:
        inv = conn.execute(
            "SELECT COUNT(*) AS n FROM invoice WHERE deleted_at IS NULL").fetchone()["n"]
        reb = conn.execute(
            "SELECT COUNT(*) AS n FROM reimburse WHERE deleted_at IS NULL").fetchone()["n"]
    return {"invoices": inv, "reimburses": reb}


def build_package() -> bytes:
    """导出为 zip：exchange.json + attachments/。"""
    with db.read() as conn:
        invs = [dict(r) for r in conn.execute(
            "SELECT * FROM invoice WHERE deleted_at IS NULL ORDER BY id ASC")]
        rebs = [dict(r) for r in conn.execute(
            "SELECT * FROM reimburse WHERE deleted_at IS NULL ORDER BY id ASC")]

    buf = BytesIO()
    store = FileStore()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for inv in invs:
            src = inv.get("source_path")
            if not src:
                continue
            p = Path(src)
            if not p.is_file():
                continue
            arc = f"attachments/{inv['id']}_{p.name}"
            zf.write(p, arc)
            inv["_attachment"] = arc
        payload = {
            "version": _VERSION,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "counts": {"invoices": len(invs), "reimburses": len(rebs)},
            "invoices": invs,
            "reimburses": rebs,
        }
        zf.writestr("exchange.json", json.dumps(payload, ensure_ascii=False))
    return buf.getvalue()


def _clean(row: dict, allowed: set) -> dict:
    skip = {"id", "created_at", "updated_at", "deleted_at"}
    return {k: v for k, v in row.items() if k in allowed and k not in skip}


def import_package(content: bytes) -> dict:
    with zipfile.ZipFile(BytesIO(content)) as zf:
        if "exchange.json" not in zf.namelist():
            raise ValueError("交接包缺少 exchange.json")
        payload = json.loads(zf.read("exchange.json").decode("utf-8"))
        invoices = payload.get("invoices") or []
        reimburses = payload.get("reimburses") or []
        store = FileStore()
        imported_inv = imported_reb = skipped_inv = skipped_reb = 0

        with db.tx() as conn:
            for row in invoices:
                no = (row.get("invoice_no") or "").strip()
                code = (row.get("invoice_code") or "").strip()
                exists = None
                if no:
                    exists = conn.execute(
                        "SELECT id FROM invoice WHERE invoice_no = ? AND invoice_code = ? "
                        "AND deleted_at IS NULL", (no, code)).fetchone()
                if exists is None and row.get("file_hash"):
                    exists = conn.execute(
                        "SELECT id FROM invoice WHERE file_hash = ? AND deleted_at IS NULL",
                        (row["file_hash"],)).fetchone()
                if exists:
                    skipped_inv += 1
                    continue

                data = _clean(row, _INVOICE_INSERT_FIELDS)
                # 匹配状态依赖本地 match_result，跨设备只保留台账状态
                if data.get("status") in ("MATCHED", "CONFIRMED"):
                    data["status"] = "NORMAL"
                arc = row.get("_attachment")
                if arc and arc in zf.namelist():
                    saved = store.save(Path(arc).name.split("_", 1)[-1], zf.read(arc))
                    data["source_path"] = str(saved)
                    data["file_hash"] = store.sha256(zf.read(arc))
                invoice_repo.insert(conn, data)
                imported_inv += 1

            for row in reimburses:
                no = (row.get("reimburse_no") or "").strip()
                batch = (row.get("batch_id") or "").strip()
                payload_row = row.get("raw_row") or ""
                if no:
                    exists = conn.execute(
                        "SELECT id FROM reimburse WHERE batch_id = ? AND reimburse_no = ? "
                        "AND deleted_at IS NULL", (batch, no)).fetchone()
                else:
                    exists = conn.execute(
                        "SELECT id FROM reimburse WHERE source_row = ? AND raw_row = ? "
                        "AND deleted_at IS NULL",
                        (row.get("source_row"), payload_row)).fetchone()
                if exists:
                    skipped_reb += 1
                    continue
                data = _clean(row, _REIMBURSE_INSERT_FIELDS)
                reimburse_repo.insert(conn, data)
                imported_reb += 1

    return {"invoices_imported": imported_inv, "invoices_skipped": skipped_inv,
            "reimburses_imported": imported_reb, "reimburses_skipped": skipped_reb}
