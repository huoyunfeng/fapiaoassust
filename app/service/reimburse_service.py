"""报销单导入用例（应用服务层）：Excel -> 标准化 -> 行级校验 -> 落库。

模板驱动：
1. 读取 Excel 表头，计算指纹；
2. 命中 mapping_cache -> 直接用缓存映射；
3. 未命中 -> 自动推断 + 映射向导；
4. 解析后按 fields_config 落库，自定义字段进 extra_data。
"""
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.domain.dto import ReimburseDTO
from app.domain.normalize import clean, parse_date, to_decimal
from app.infra.excel import read_reimburses
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import (match_repo, reimburse_repo)
from app.service.mapping_service import mapping_service
from app.service.template_service import template_service

logger = get_logger(__name__)


def _to_dto(record: dict, batch_id: str, template_id: Optional[int] = None) -> ReimburseDTO:
    extra = record.get("_extra", {}) or {}
    return ReimburseDTO(
        batch_id=batch_id,
        template_id=template_id,
        reimburse_no=clean(record.get("reimburse_no")),
        submitter=clean(record.get("submitter")),
        reimburse_date=parse_date(record.get("reimburse_date")),
        period_start=parse_date(record.get("period_start")),
        period_end=parse_date(record.get("period_end")),
        expense_type=clean(record.get("expense_type")),
        amount=to_decimal(record.get("amount")),
        invoice_no=clean(record.get("invoice_no")),
        invoice_date=parse_date(record.get("invoice_date")),
        remark=clean(record.get("remark")),
        row_index=record.get("row_index") or 0,
        source_row=record.get("row_index") or 0,
        extra_data=extra,
        split_type=record.get("split_type", "none"),
        parent_id=record.get("parent_id"),
    )


def import_excel(file_name: str, content: bytes, batch_id: str = None,
                 operator: str = None, template_id: int = None,
                 fields_config: dict = None, remember_mapping: bool = True,
                 enterprise_id: int = None, user_id: int = None) -> dict:
    """导入报销单 Excel。

    Args:
        template_id:  指定模板 id（优先）。为 None 时取默认模板。
        fields_config: 临时字段映射（映射向导传入）。优先级高于 template_id。
        remember_mapping: 导入成功后是否回写映射缓存。
    """
    operator = operator or settings.default_operator
    batch_id = batch_id or f"batch_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"

    # ---- 解析模板 / 字段配置 ----
    tpl = None
    pending_template_name = None
    if fields_config is None:
        if template_id is not None:
            tpl = template_service.get(template_id)
            if tpl is None:
                return {"ok": False, "message": f"模板 #{template_id} 不存在"}
        else:
            tpl = template_service.get_default(enterprise_id)
            if tpl is None:
                return {"ok": False, "message": "未找到默认模板，请先在模板管理中创建"}
        fields_config = tpl["fields_config"]
        template_id = tpl["id"] if tpl else None
    else:
        # 自定义映射：模板延迟到导入成功后再创建，避免失败时留下无用模板
        if template_id is None:
            pending_template_name = f"自动模板_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    records, read_errors = read_reimburses(content, fields_config=fields_config)
    errors: List[dict] = [{"row_index": e.get("row_index", 0), "message": e["message"]}
                          for e in read_errors]

    # ---- 一行多票拆行 ----
    options = fields_config.get("options", {}) or {}
    sep = options.get("multi_invoice_separator", ",")
    if sep:
        from app.infra.excel import split_multi_invoice
        records = split_multi_invoice(records, sep)

    rows: List[dict] = []
    auto_no = 0
    for record in records:
        dto = _to_dto(record, batch_id, template_id)
        # 锚点校验：金额必填（设计 §3.3 原则 1）
        if dto.amount is None:
            errors.append({"row_index": dto.row_index,
                           "message": f"金额无效（原始值: {record.get('amount')}），已跳过"})
            continue
        # 报销单号缺失时自动补号（很多报销明细表没有单号列），保证可导入
        if not dto.reimburse_no:
            auto_no += 1
            dto.reimburse_no = f"自动-{batch_id[-6:]}-{auto_no:04d}"
        rows.append({
            "batch_id": dto.batch_id,
            "template_id": dto.template_id,
            "reimburse_no": dto.reimburse_no,
            "submitter": dto.submitter,
            "reimburse_date": dto.reimburse_date,
            "period_start": dto.period_start,
            "period_end": dto.period_end,
            "expense_type": dto.expense_type,
            "amount": str(dto.amount),
            "invoice_no": dto.invoice_no,
            "invoice_date": dto.invoice_date,
            "remark": dto.remark,
            "extra_data": dto.extra_data,
            "source_row": dto.source_row,
            "parent_id": dto.parent_id,
            "split_type": dto.split_type,
            "raw_row": record,
            "row_index": dto.row_index,
        })

    with db.tx() as conn:
        imported = 0
        for r in rows:
            try:
                reimburse_repo.insert(conn, r)
                imported += 1
            except Exception as e:
                errors.append({"row_index": r["row_index"],
                               "message": f"落库失败: {e}"})
        logger.info("报销单导入: batch=%s file=%s 模板=%s 成功 %s 行, 错误 %s 行 (by %s)",
                    batch_id, file_name, template_id, imported, len(errors), operator)

    # ---- 仅在导入成功后才创建自动模板（避免失败时留下无用模板）----
    if imported > 0 and pending_template_name and template_id is None:
        cr = template_service.create(pending_template_name, fields_config,
                                     description="自定义导入时自动生成")
        if cr.get("ok"):
            template_id = cr["template_id"]
            with db.tx() as conn:
                conn.execute("UPDATE reimburse SET template_id = ? WHERE batch_id = ?",
                             (template_id, batch_id))

    # ---- 回写映射缓存（记忆）：移到事务外，避免嵌套锁 ----
    if imported > 0 and remember_mapping and template_id is not None and fields_config:
        try:
            headers = [f["excel_header"] for f in fields_config.get("fields", [])]
            mapping = {f["excel_header"]: f["system_key"]
                       for f in fields_config.get("fields", [])}
            from app.domain.field_registry import header_fingerprint
            fp = header_fingerprint(headers)
            mapping_service.remember(fp, template_id, headers, mapping,
                                     enterprise_id, user_id)
        except Exception as e:
            logger.warning("映射缓存回写失败: %s", e)

    # ---- 结果判定：无成功行时返回失败，前端需给出降级选项（设计 §12.4）----
    if imported == 0:
        detail = errors[0]["message"] if errors else "未解析到任何数据行"
        return {
            "ok": False,
            "message": f"导入失败：{detail}",
            "batch_id": batch_id,
            "imported": 0,
            "errors": errors,
            "template_id": template_id,
            "can_fallback_classic": True,   # 可改用经典模板重试
            "can_manual_entry": True,       # 可转手动录入
        }
    # ---- 导入成功后自动执行一轮匹配，避免用户忘记手动执行而一直显示"未匹配" ----
    match_summary = None
    try:
        from app.service import match_service
        match_summary = match_service.run_matching(operator)
    except Exception as e:
        logger.warning("报销单导入后自动匹配失败: %s", e)
    message = f"导入成功：{imported} 行"
    if match_summary is not None:
        message += (f"；自动匹配 {match_summary['auto_matched']} 对"
                    f"，待人工确认 {match_summary['pending'] + match_summary['conflict']} 对")
    return {"ok": True, "message": message,
            "batch_id": batch_id, "imported": imported, "errors": errors,
            "template_id": template_id, "match": match_summary}


def manual_create(fields: dict, operator: str = None) -> dict:
    """手动录入单条报销单（设计 §12.4 降级入口）。"""
    operator = operator or settings.default_operator
    reimburse_no = clean(fields.get("reimburse_no"))
    if not reimburse_no:
        return {"ok": False, "message": "报销单号必填"}
    amount = to_decimal(fields.get("amount"))
    if amount is None:
        return {"ok": False, "message": "金额无效"}
    batch_id = f"manual_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    row = {
        "batch_id": batch_id,
        "template_id": None,
        "reimburse_no": reimburse_no,
        "submitter": clean(fields.get("submitter")),
        "reimburse_date": parse_date(fields.get("reimburse_date")),
        "period_start": parse_date(fields.get("period_start")),
        "period_end": parse_date(fields.get("period_end")),
        "expense_type": clean(fields.get("expense_type")),
        "amount": str(amount),
        "invoice_no": clean(fields.get("invoice_no")),
        "invoice_date": parse_date(fields.get("invoice_date")),
        "remark": clean(fields.get("remark")),
        "extra_data": {},
        "split_type": "none",
        "raw_row": dict(fields),
        "row_index": 1,
    }
    with db.tx() as conn:
        reimburse_repo.insert(conn, row)
    logger.info("手动录入报销单: %s 金额=%s (by %s)", reimburse_no, amount, operator)
    return {"ok": True, "message": f"已手动录入报销单 {reimburse_no}", "batch_id": batch_id}


def _release_active_matches(conn, reimburse_ids: List[int], operator: str) -> List[int]:
    """删除报销单前，把关联的 MATCHED 发票转回 NORMAL。"""
    released: List[int] = []
    for rid in reimburse_ids:
        active = match_repo.active_for_reimburse(conn, rid)
        if active:
            from app.service.status_flow import change_invoice_status
            try:
                change_invoice_status(conn, active["invoice_id"], "NORMAL",
                                      f"报销单 #{rid} 删除，解除匹配", operator)
                released.append(active["invoice_id"])
            except Exception as e:
                logger.warning("释放发票 #%s 匹配失败: %s", active["invoice_id"], e)
    return released


def delete_reimburse(reimburse_id: int, operator: str = None) -> dict:
    """删除报销单：同一事务内释放发票 -> 显式清理匹配记录 -> 删报销单（失败整体回滚）。

    报销单为 Excel 导入解析后的数据行，原始 Excel 未落盘，无物理文件需清理。
    """
    operator = operator or settings.default_operator
    with db.tx() as conn:
        row = reimburse_repo.get(conn, reimburse_id)
        if row is None:
            return {"ok": False, "message": f"报销单 #{reimburse_id} 不存在"}
        _release_active_matches(conn, [reimburse_id], operator)
        n_match = match_repo.delete_for_reimburse(conn, reimburse_id)
        reimburse_repo.delete(conn, reimburse_id)
        # 汇总匹配的 match_pair 删空后，清理悬空的父级 match_result，防脏数据
        n_orphan = match_repo.delete_unpaired_summary(conn)
        logger.info("报销单 #%s 已删除 (清理匹配记录 %s 条、孤儿汇总匹配 %s 条, by %s)",
                    reimburse_id, n_match, n_orphan, operator)
        return {"ok": True, "message": "报销单已删除（关联匹配已级联删除）"}


def delete_batch(batch_id: str, operator: str = None) -> dict:
    """删除导入批次：同一事务内连带清理批次内全部报销单产生的匹配记录。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        ids = [r["id"] for r in reimburse_repo.list(conn, batch_id=batch_id, limit=100000)]
        _release_active_matches(conn, ids, operator)
        n_match = 0
        for rid in ids:
            n_match += match_repo.delete_for_reimburse(conn, rid)
        n = reimburse_repo.delete_batch(conn, batch_id)
        n_orphan = match_repo.delete_unpaired_summary(conn)
        logger.info("批次 %s 已删除 %s 行 (清理匹配记录 %s 条、孤儿汇总匹配 %s 条, by %s)",
                    batch_id, n, n_match, n_orphan, operator)
        return {"ok": True, "message": f"批次 {batch_id} 已删除 {n} 行（关联匹配已级联删除）"}


def list_reimburses(batch_id: str = None, keyword: str = None,
                    template_id: int = None) -> List[dict]:
    with db.read() as conn:
        rows = reimburse_repo.list(conn, batch_id=batch_id, keyword=keyword)
        active_ids = match_repo.active_reimburse_ids(conn)
    for r in rows:
        r["matched"] = r["id"] in active_ids
    return rows


def list_batches() -> List[dict]:
    with db.read() as conn:
        return reimburse_repo.batches(conn)


def reimburse_count() -> int:
    with db.read() as conn:
        return reimburse_repo.count(conn)


def expense_breakdown(date_from: str = None, date_to: str = None, top_n: int = 5) -> dict:
    """费用构成统计（基于 reimburse 表）。

    返回结构：
    {
        "total_amount": float,          # 时间段内总金额（元）
        "total_count": int,              # 时间段内总笔数
        "label_when_all": str,           # 中心标签（历史总报销/本月总报销）
        "items": [                       # 按金额降序，超过 top_n 合并为 "其他"
            {"name": str, "amount": float, "count": int, "percent": float},
            ...
        ]
    }
    """
    with db.read() as conn:
        rows = reimburse_repo.aggregate_by_expense_type(conn, date_from, date_to)

    total_amount = sum((r["amount"] or 0) for r in rows)
    total_count = sum((r["count"] or 0) for r in rows)

    top = rows[:top_n]
    rest = rows[top_n:]
    items = []
    for r in top:
        amt = float(r["amount"] or 0)
        pct = (amt / total_amount * 100) if total_amount > 0 else 0
        items.append({
            "name": r["name"],
            "amount": round(amt, 2),
            "count": int(r["count"] or 0),
            "percent": round(pct, 2),
        })
    if rest:
        other_amt = sum(float(r["amount"] or 0) for r in rest)
        other_cnt = sum(int(r["count"] or 0) for r in rest)
        other_pct = (other_amt / total_amount * 100) if total_amount > 0 else 0
        items.append({
            "name": "其他",
            "amount": round(other_amt, 2),
            "count": other_cnt,
            "percent": round(other_pct, 2),
        })

    # 中心标签：按前端 filter 口径推断
    from datetime import date
    label_when_all = "历史总报销"
    if date_from and date_to:
        today = date.today()
        # 本月
        if (date_from == today.replace(day=1).isoformat()
                and date_to == today.isoformat()):
            label_when_all = "本月总报销"
        # 上月
        else:
            first_of_month = today.replace(day=1)
            import datetime as _dt
            last_month_end = first_of_month - _dt.timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            if (date_from == last_month_start.isoformat()
                    and date_to == last_month_end.isoformat()):
                label_when_all = "上月总报销"
            # 今年
            elif (date_from == f"{today.year}-01-01"
                  and date_to == today.isoformat()):
                label_when_all = "今年总报销"

    return {
        "total_amount": round(total_amount, 2),
        "total_count": total_count,
        "label_when_all": label_when_all,
        "items": items,
    }
