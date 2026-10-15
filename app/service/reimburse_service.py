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
    return {"ok": True, "message": f"导入成功：{imported} 行",
            "batch_id": batch_id, "imported": imported, "errors": errors,
            "template_id": template_id}


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
    operator = operator or settings.default_operator
    with db.tx() as conn:
        row = reimburse_repo.get(conn, reimburse_id)
        if row is None:
            return {"ok": False, "message": f"报销单 #{reimburse_id} 不存在"}
        _release_active_matches(conn, [reimburse_id], operator)
        reimburse_repo.delete(conn, reimburse_id)
        return {"ok": True, "message": "报销单已删除（关联匹配已级联删除）"}


def delete_batch(batch_id: str, operator: str = None) -> dict:
    operator = operator or settings.default_operator
    with db.tx() as conn:
        ids = [r["id"] for r in reimburse_repo.list(conn, batch_id=batch_id, limit=100000)]
        _release_active_matches(conn, ids, operator)
        n = reimburse_repo.delete_batch(conn, batch_id)
        return {"ok": True, "message": f"批次 {batch_id} 已删除 {n} 行"}


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
