"""回收站与自动清理服务（模块四：高危操作防御）。

核心约定：
1. 常规删除只写 deleted_at（软删除），绝不物理删除；
2. 彻底删除只允许在回收站执行（物理删除数据库记录 + 清理附件）；
3. 自动清理周期由设置页-数据管理配置（7/15/30/90 天，0 表示永久保留）；
4. 自动清理在应用启动时执行一次，并由后台线程每日检查一次。
"""
import threading
from datetime import datetime, timedelta

from app.repository import db
from app.repository.repos import invoice_repo, reimburse_repo
from app.service import invoice_service, reimburse_service
from app.infra.logger import get_logger

logger = get_logger(__name__)

KV_KEY = "recycle_auto_clean_days"
DEFAULT_DAYS = 30
ALLOWED_DAYS = (7, 15, 30, 90, 0)   # 0 = 永久保留（不自动删除）

_cleanup_lock = threading.Lock()
_cleanup_thread = None


def get_auto_clean_days() -> int:
    """读取回收站自动清理周期（天），0 表示永久保留。"""
    with db.read() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (KV_KEY,)).fetchone()
    try:
        days = int(row["v"]) if row else DEFAULT_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


def set_auto_clean_days(days) -> dict:
    """持久化自动清理周期（仅允许 7/15/30/90/0）。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return {"ok": False, "message": "清理周期参数无效"}
    if days not in ALLOWED_DAYS:
        return {"ok": False, "message": "清理周期仅支持 7 / 15 / 30 / 90 天或永久保留"}
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v, "
            "updated_at = datetime('now','localtime')",
            (KV_KEY, str(days)))
    return {"ok": True, "days": days,
            "message": "已保存（永久保留）" if days == 0 else f"已保存（{days} 天后自动清理）"}


def list_deleted(limit: int = 500) -> dict:
    """回收站列表：发票 + 报销单，按删除时间倒序。"""
    inv = invoice_service.list_deleted_invoices(limit=limit)
    reb = reimburse_service.list_deleted_reimburses(limit=limit)
    items = []
    for r in inv:
        items.append({
            "kind": "invoice", "id": r["id"], "label": r.get("invoice_no") or f"#{r['id']}",
            "sub": (r.get("buyer_name") or "") + (" · " + (r.get("seller_name") or "") if r.get("seller_name") else ""),
            "amount": r.get("total_amount"),
            "invoice_date": r.get("invoice_date"),
            "deleted_at": r.get("deleted_at"),
            "has_file": bool(r.get("source_path")),
        })
    for r in reb:
        items.append({
            "kind": "reimburse", "id": r["id"], "label": r.get("reimburse_no") or f"#{r['id']}",
            "sub": (r.get("submitter") or "") + (" · " + (r.get("expense_type") or "") if r.get("expense_type") else ""),
            "amount": r.get("amount"),
            "invoice_date": r.get("reimburse_date"),
            "deleted_at": r.get("deleted_at"),
            "has_file": False,
        })
    items.sort(key=lambda x: x.get("deleted_at") or "", reverse=True)
    return {"items": items[:limit], "total": len(items),
            "auto_clean_days": get_auto_clean_days()}


def restore(kind: str, ids) -> dict:
    """从回收站恢复（清空 deleted_at）。"""
    ids = [int(i) for i in ids]
    if not ids:
        return {"ok": False, "message": "未选择要恢复的数据"}
    if kind == "invoice":
        return invoice_service.restore_invoices(ids)
    if kind == "reimburse":
        return reimburse_service.restore_reimburses(ids)
    return {"ok": False, "message": f"未知数据类型: {kind}"}


def purge(kind: str, ids) -> dict:
    """彻底删除（物理删除 + 清理附件），不可恢复。"""
    ids = [int(i) for i in ids]
    if not ids:
        return {"ok": False, "message": "未选择要彻底删除的数据"}
    if kind == "invoice":
        return invoice_service.purge_invoices(ids)
    if kind == "reimburse":
        return reimburse_service.purge_reimburses(ids)
    return {"ok": False, "message": f"未知数据类型: {kind}"}


def auto_cleanup(operator: str = None) -> dict:
    """扫描并物理删除超过配置周期的回收站数据，写审计日志。

    days=0（永久保留）时直接跳过。
    """
    days = get_auto_clean_days()
    if days <= 0:
        return {"ok": True, "purged": 0, "days": 0}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.read() as conn:
        inv_rows = invoice_repo.deleted_before(conn, cutoff)
        reb_rows = reimburse_repo.deleted_before(conn, cutoff)
    inv_ids = [r["id"] for r in inv_rows]
    reb_ids = [r["id"] for r in reb_rows]
    if not inv_ids and not reb_ids:
        return {"ok": True, "purged": 0, "days": days, "cutoff": cutoff}

    total = 0
    if inv_ids:
        total += invoice_service.purge_invoices(inv_ids, operator).get("purged", 0)
    if reb_ids:
        total += reimburse_service.purge_reimburses(reb_ids, operator).get("purged", 0)

    from app.service import audit_service
    with db.tx() as conn:
        audit_service.log(
            conn, "recycle_auto_cleanup", "recycle", "",
            f"超期 {days} 天", f"回收站 {total} 条", "已彻底删除",
            operator or "system",
            f"系统自动清理了超期 {days} 天的回收站数据 {total} 条"
            f"（发票 {len(inv_ids)} 张 / 报销单 {len(reb_ids)} 条）")
    logger.info("回收站自动清理：超期 %s 天，清理 %s 条（发票 %s / 报销单 %s）",
                days, total, len(inv_ids), len(reb_ids))
    return {"ok": True, "purged": total, "days": days,
            "invoice": len(inv_ids), "reimburse": len(reb_ids), "cutoff": cutoff}


def _daily_loop():
    """后台每日检查：每小时唤醒一次，跨天时执行清理（避免精确到 0 点的复杂性）。"""
    import time
    last_day = datetime.now().date()
    while True:
        time.sleep(3600)
        today = datetime.now().date()
        if today != last_day:
            last_day = today
            try:
                auto_cleanup()
            except Exception as e:      # 清理失败不影响主服务
                logger.warning("回收站每日自动清理失败: %s", e)


def start_background_cleaner() -> None:
    """启动每日自动清理后台线程（幂等，重复调用不会创建多条线程）。"""
    global _cleanup_thread
    with _cleanup_lock:
        if _cleanup_thread and _cleanup_thread.is_alive():
            return
        _cleanup_thread = threading.Thread(target=_daily_loop, daemon=True,
                                           name="recycle-auto-cleanup")
        _cleanup_thread.start()
        logger.info("回收站自动清理后台线程已启动")
