"""报销单导入用例（应用服务层）：Excel -> 标准化 -> 行级校验 -> 落库。"""
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from app.config import settings
from app.domain.dto import ReimburseDTO
from app.domain.normalize import clean, parse_date, to_decimal
from app.infra.excel import read_reimburses
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import company_repo, invoice_repo, match_repo, reimburse_repo

logger = get_logger(__name__)


def _to_dto(record: dict, batch_id: str) -> ReimburseDTO:
    return ReimburseDTO(
        batch_id=batch_id,
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
        row_index=int(record.get("row_index") or 0),
    )


def import_excel(file_name: str, content: bytes, batch_id: str = None,
                 operator: str = None) -> dict:
    """导入报销单 Excel。有效行落库，无效行逐行报告错误。"""
    operator = operator or settings.default_operator
    batch_id = batch_id or f"batch_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"

    records, read_errors = read_reimburses(content)
    rows: List[dict] = []
    errors: List[dict] = [{"row_index": e.get("row_index", 0), "message": e["message"]}
                          for e in read_errors]

    for record in records:
        dto = _to_dto(record, batch_id)
        if not dto.reimburse_no:
            errors.append({"row_index": dto.row_index, "message": "报销单号为空，已跳过"})
            continue
        if dto.amount is None:
            errors.append({"row_index": dto.row_index,
                           "message": f"金额无效（原始值: {record.get('amount')}），已跳过"})
            continue
        rows.append({
            "batch_id": dto.batch_id,
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
        logger.info("报销单导入: batch=%s file=%s 成功 %s 行, 错误 %s 行 (by %s)",
                    batch_id, file_name, imported, len(errors), operator)

    return {"batch_id": batch_id, "imported": imported, "errors": errors}


def _release_active_matches(conn, reimburse_ids: List[int], operator: str) -> List[int]:
    """删除报销单前，把关联的 MATCHED 发票转回 NORMAL（级联删除会清掉匹配行）。"""
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


def list_reimburses(batch_id: str = None, keyword: str = None) -> List[dict]:
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
