"""发票导入与人工处理用例（应用服务层）。

编排：解析 -> 标准化 -> 校验 -> 去重 -> 状态机，管理事务边界（I9）。
算法全部委托领域引擎（纯函数），本层不写算法。
"""
import hashlib
import json
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from app.config import settings
from app.domain import parsers as parsers_mod
from app.domain.dedup import DedupEngine, Fingerprint
from app.domain.dto import InvoiceDTO, Issue, ParseTask, RuleDTO
from app.domain.normalize import NormalizeEngine, clean, to_decimal
from app.domain.rules_engine import RuleEngine
from app.domain.states import InvoiceStatus
from app.domain.validator import InvoiceValidator
from app.infra.filestore import FileStore
from app.infra.logger import get_logger
from app.infra.ocr import register_ocr_backend
from app.repository import db
from app.repository.repos import invoice_repo, log_repo, rule_repo
from app.service.status_flow import change_invoice_status

logger = get_logger(__name__)

_normalize = NormalizeEngine()
_validator = InvoiceValidator()
_rule_engine = RuleEngine()
_dedup = DedupEngine()
_parser_engine = parsers_mod.ParserEngine()
_file_store = FileStore()

# 图片发票（jpg/png）解析依赖 OCR 后端：在服务层装配，保证 Web 与测试路径一致
register_ocr_backend()

# 文件类型 -> 解析链首环节（作为 parse_engine 的初始占位，解析成功后更新）
_CHAIN_HEAD = {"xml": "xml", "ofd": "ofd", "pdf": "pdf_text",
               "jpg": "ocr", "jpeg": "ocr", "png": "ocr"}


# ---------------------------------------------------------------- 转换工具
def _dto_from_row(row: dict) -> InvoiceDTO:
    return InvoiceDTO(
        invoice_code=row.get("invoice_code") or "",
        invoice_no=row.get("invoice_no") or "",
        invoice_date=row.get("invoice_date"),
        buyer_name=row.get("buyer_name") or "",
        buyer_tax_no=row.get("buyer_tax_no") or "",
        seller_name=row.get("seller_name") or "",
        seller_tax_no=row.get("seller_tax_no") or "",
        amount=to_decimal(row.get("amount")),
        tax_amount=to_decimal(row.get("tax_amount")),
        total_amount=to_decimal(row.get("total_amount")),
        tax_rate=row.get("tax_rate") or "",
        item_summary=row.get("item_summary") or "",
        is_red_flush=bool(row.get("is_red_flush")),
    )


def _row_fields_from_dto(dto: InvoiceDTO) -> dict:
    def s(v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else str(v)
    return {
        "invoice_code": dto.invoice_code,
        "invoice_no": dto.invoice_no,
        "invoice_date": dto.invoice_date,
        "buyer_name": dto.buyer_name,
        "buyer_tax_no": dto.buyer_tax_no,
        "seller_name": dto.seller_name,
        "seller_tax_no": dto.seller_tax_no,
        "amount": s(dto.amount),
        "tax_amount": s(dto.tax_amount),
        "total_amount": s(dto.total_amount),
        "tax_rate": dto.tax_rate,
        "item_summary": dto.item_summary,
        "is_red_flush": 1 if dto.is_red_flush else 0,
    }


def _rules(conn) -> List[RuleDTO]:
    return [RuleDTO(id=r["id"], name=r["name"], rule_type=r["rule_type"],
                    priority=r["priority"], stop_on_fail=bool(r["stop_on_fail"]),
                    config=r["config"], enabled=bool(r["enabled"]))
            for r in rule_repo.list_enabled(conn)]


def _raw_json(row: dict) -> dict:
    try:
        return json.loads(row.get("raw_json") or "{}")
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- 导入
def import_files(files: List[tuple[str, str, bytes]], operator: str = None) -> List[dict]:
    """导入发票文件（每个文件独立事务，互不阻塞）。

    files: [(文件名, content_type, 字节内容)]
    返回: [{file_name, ok, invoice_id, status, message}]
    """
    operator = operator or settings.default_operator
    results = []
    for file_name, _ct, content in files:
        try:
            t0 = time.perf_counter()
            r = _import_one(file_name, content, operator)
            logger.info("导入耗时 %s: %.0fms (status=%s)",
                        file_name, (time.perf_counter() - t0) * 1000, r.get("status"))
            results.append(r)
        except Exception as e:
            logger.exception("导入 %s 失败", file_name)
            results.append({"file_name": file_name, "ok": False,
                            "invoice_id": None, "status": None,
                            "message": f"导入异常: {e}"})
    # 有新发票导入成功后自动执行一轮匹配，报销单页状态随之刷新
    if any(r.get("ok") for r in results):
        try:
            from app.service import match_service
            t0 = time.perf_counter()
            ms = match_service.run_matching(operator)
            logger.info("发票导入后自动匹配: %s (%.0fms)",
                        ms, (time.perf_counter() - t0) * 1000)
        except Exception as e:
            logger.warning("发票导入后自动匹配失败: %s", e)
    return results


def _import_one(file_name: str, content: bytes, operator: str) -> dict:
    file_type = _file_store.detect_type(file_name)
    if not _file_store.is_supported(file_name):
        return {"file_name": file_name, "ok": False, "invoice_id": None,
                "status": None, "message": f"不支持的文件类型: {file_type or '未知'}"}

    file_hash = _file_store.sha256(content)
    t_save = time.perf_counter()
    source_path = str(_file_store.save(file_name, content))
    save_ms = (time.perf_counter() - t_save) * 1000
    task = ParseTask(file_name=file_name, file_type=file_type, content=content)

    with db.tx() as conn:
        # 不变量 I1：同一文件 hash 只能存在一条发票 -> 直接拦截
        existed = invoice_repo.get_by_hash(conn, file_hash)
        if existed:
            return {"file_name": file_name, "ok": False, "invoice_id": existed["id"],
                    "status": existed["status"],
                    "message": f"文件已存在（发票 #{existed['id']}，I1: 文件哈希唯一），未重复导入"}

        raw: dict = {"file_name": file_name, "degrade_chain": [], "issues": []}
        invoice_id = invoice_repo.insert(conn, {
            "file_hash": file_hash,
            "source_path": source_path,
            "file_type": file_type,
            "invoice_no": "",
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "parse_engine": _CHAIN_HEAD.get(file_type, "xml"),
            "parse_confidence": 0.0,
            "status": InvoiceStatus.PARSING.value,
        })

        # 解析（引擎为纯函数，在事务内调用无副作用）
        t_parse = time.perf_counter()
        result = _parser_engine.parse(task)
        parse_ms = (time.perf_counter() - t_parse) * 1000
        t_write = time.perf_counter()
        logger.info("导入分段 %s: 存盘=%.0fms 解析=%.0fms", file_name, save_ms, parse_ms)

        if result.success:
            raw["degrade_chain"] = result.degrade_chain
            t_u = time.perf_counter()
            invoice_repo.update(conn, invoice_id, {
                **_row_fields_from_dto(result.dto),
                "parse_engine": result.engine,
                "parse_confidence": result.confidence,
                "raw_json": json.dumps(raw, ensure_ascii=False),
            })
            t_s = time.perf_counter()
            change_invoice_status(conn, invoice_id, InvoiceStatus.PARSED.value,
                                  f"解析成功（engine={result.engine}）", operator)
            t_v = time.perf_counter()
            status, message = _validate_and_dedup(conn, invoice_id, operator)
            logger.info("导入分段 %s: 存盘=%.0fms 解析=%.0fms 落库=%.0fms (update=%.0f "
                        "status=%.0f validate=%.0f)", file_name, save_ms, parse_ms,
                        (time.perf_counter() - t_write) * 1000, (t_s - t_u) * 1000,
                        (t_v - t_s) * 1000, (time.perf_counter() - t_v) * 1000)
            return {"file_name": file_name, "ok": True, "invoice_id": invoice_id,
                    "status": status, "message": message}

        # 全部失败 -> ABNORMAL（不阻塞其他文件）
        raw["degrade_chain"] = result.degrade_chain
        raw["error"] = result.error
        invoice_repo.update(conn, invoice_id, {
            "raw_json": json.dumps(raw, ensure_ascii=False),
        })
        change_invoice_status(conn, invoice_id, InvoiceStatus.ABNORMAL.value,
                              f"解析失败: {result.error}", operator)
        return {"file_name": file_name, "ok": False, "invoice_id": invoice_id,
                "status": InvoiceStatus.ABNORMAL.value, "message": f"解析失败: {result.error}"}


def _validate_and_dedup(conn, invoice_id: int, operator: str) -> tuple[str, str]:
    """PARSED 之后：校验（内置 C1-C5 + 规则）-> 去重 -> NORMAL / ABNORMAL / DUPLICATE。"""
    row = invoice_repo.get(conn, invoice_id)
    dto = _dto_from_row(row)

    issues = _validator.validate(dto) + _rule_engine.evaluate(dto, _rules(conn))
    errors = [i for i in issues if i.severity == "ERROR"]

    raw = _raw_json(row)
    raw["issues"] = [i.to_dict() for i in issues]

    if errors:
        invoice_repo.update(conn, invoice_id, {
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "is_red_flush": 1 if dto.is_red_flush else 0,
        })
        change_invoice_status(conn, invoice_id, InvoiceStatus.ABNORMAL.value,
                              "校验失败: " + "; ".join(i.message for i in errors), operator)
        return InvoiceStatus.ABNORMAL.value, "; ".join(i.message for i in errors)

    # 去重（DUPLICATE 存量不参与判重）
    fp = Fingerprint(file_hash=row["file_hash"], invoice_code=dto.invoice_code,
                     invoice_no=dto.invoice_no)
    hit = _dedup.check(fp, invoice_repo.fingerprints(conn, exclude_id=invoice_id))
    if hit:
        raw["dup_of"] = hit.existing_id
        raw["dup_reason"] = hit.message
        invoice_repo.update(conn, invoice_id, {
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "is_red_flush": 1 if dto.is_red_flush else 0,
        })
        change_invoice_status(conn, invoice_id, InvoiceStatus.DUPLICATE.value,
                              hit.message, operator)
        return InvoiceStatus.DUPLICATE.value, hit.message

    invoice_repo.update(conn, invoice_id, {
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "is_red_flush": 1 if dto.is_red_flush else 0,
    })
    warns = [i for i in issues if i.severity == "WARN"]
    msg = "校验通过" + ("（警告: " + "; ".join(i.message for i in warns) + "）" if warns else "")
    change_invoice_status(conn, invoice_id, InvoiceStatus.NORMAL.value, msg, operator)
    return InvoiceStatus.NORMAL.value, msg


# ---------------------------------------------------------------- 人工录入
def manual_create(fields: dict, operator: str = None) -> dict:
    """人工录入发票（MANUAL 引擎，解析链终点）。"""
    operator = operator or settings.default_operator
    dto = _normalize.normalize(InvoiceDTO(
        invoice_code=clean(fields.get("invoice_code")),
        invoice_no=clean(fields.get("invoice_no")),
        invoice_date=fields.get("invoice_date"),
        buyer_name=clean(fields.get("buyer_name")),
        buyer_tax_no=clean(fields.get("buyer_tax_no")),
        seller_name=clean(fields.get("seller_name")),
        seller_tax_no=clean(fields.get("seller_tax_no")),
        amount=to_decimal(fields.get("amount")),
        tax_amount=to_decimal(fields.get("tax_amount")),
        total_amount=to_decimal(fields.get("total_amount")),
        tax_rate=clean(fields.get("tax_rate")),
        item_summary=clean(fields.get("item_summary")),
    ))

    errors = [i for i in _validator.validate(dto) if i.severity == "ERROR"]
    if errors:
        return {"ok": False, "invoice_id": None,
                "message": "; ".join(i.message for i in errors)}

    # 人工录入无文件：file_hash 取字段指纹 + 随机盐，语义判重由 I2 承担
    digest = hashlib.sha256(
        (json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
         + uuid.uuid4().hex).encode("utf-8")
    ).hexdigest()

    with db.tx() as conn:
        fp = Fingerprint(file_hash=digest, invoice_code=dto.invoice_code,
                         invoice_no=dto.invoice_no)
        hit = _dedup.check(fp, invoice_repo.fingerprints(conn))
        if hit:
            return {"ok": False, "invoice_id": None, "message": hit.message}

        invoice_id = invoice_repo.insert(conn, {
            "file_hash": digest,
            "file_type": "pdf",  # CHECK 约束需合法值；人工录入无文件，取最常用类型占位
            "invoice_no": "",
            "raw_json": json.dumps({"manual_entry": True, "operator": operator,
                                    "degrade_chain": [], "issues": []}, ensure_ascii=False),
            "parse_engine": "manual",
            "parse_confidence": 1.0,
            "status": InvoiceStatus.PARSING.value,
        })
        invoice_repo.update(conn, invoice_id, _row_fields_from_dto(dto))
        change_invoice_status(conn, invoice_id, InvoiceStatus.PARSED.value,
                              "人工录入", operator)
        status, message = _validate_and_dedup(conn, invoice_id, operator)
        return {"ok": status == InvoiceStatus.NORMAL.value, "invoice_id": invoice_id,
                "status": status, "message": message}


# ---------------------------------------------------------------- 人工处理
def fix_abnormal(invoice_id: int, fields: dict, operator: str = None,
                 reason: str = "") -> dict:
    """人工修正 ABNORMAL 发票：重新标准化 + 校验，通过则转 NORMAL。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return {"ok": False, "message": f"发票 #{invoice_id} 不存在"}
        if row["status"] != InvoiceStatus.ABNORMAL.value:
            return {"ok": False, "message": f"仅 ABNORMAL 状态可修正（当前: {row['status']}）"}

        dto = _dto_from_row(row)
        for key, val in fields.items():
            if key in ("invoice_code", "invoice_no", "buyer_name", "buyer_tax_no",
                       "seller_name", "seller_tax_no", "tax_rate", "item_summary"):
                setattr(dto, key, clean(val))
            elif key in ("amount", "tax_amount", "total_amount"):
                setattr(dto, key, to_decimal(val))
            elif key == "invoice_date":
                dto.invoice_date = _normalize.parse_date(val)
        dto = _normalize.normalize(dto)

        issues = _validator.validate(dto) + _rule_engine.evaluate(dto, _rules(conn))
        errors = [i for i in issues if i.severity == "ERROR"]

        # 修正后的语义判重（防止修正成一张已存在的发票）
        if not errors:
            fp = Fingerprint(file_hash=row["file_hash"], invoice_code=dto.invoice_code,
                             invoice_no=dto.invoice_no)
            hit = _dedup.check(fp, invoice_repo.fingerprints(conn, exclude_id=invoice_id))
            if hit and hit.kind == "code_no":
                errors.append(Issue("DUPLICATE_CODE_NO", hit.message, "ERROR"))

        raw = _raw_json(row)
        raw["issues"] = [i.to_dict() for i in issues]
        if errors:
            invoice_repo.update(conn, invoice_id,
                                {"raw_json": json.dumps(raw, ensure_ascii=False)})
            return {"ok": False, "message": "; ".join(i.message for i in errors)}

        invoice_repo.update(conn, invoice_id, {
            **_row_fields_from_dto(dto),
            "raw_json": json.dumps(raw, ensure_ascii=False),
        })
        change_invoice_status(conn, invoice_id, InvoiceStatus.NORMAL.value,
                              f"人工修正: {reason or '无备注'}", operator)
        return {"ok": True, "message": "修正成功，发票已转为 NORMAL"}


def confirm_not_duplicate(invoice_id: int, operator: str = None, reason: str = "") -> dict:
    """人工确认 DUPLICATE 发票并非重复 -> NORMAL。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return {"ok": False, "message": f"发票 #{invoice_id} 不存在"}
        if row["status"] != InvoiceStatus.DUPLICATE.value:
            return {"ok": False, "message": f"仅 DUPLICATE 状态可确认（当前: {row['status']}）"}
        try:
            change_invoice_status(conn, invoice_id, InvoiceStatus.NORMAL.value,
                                  f"人工确认非重复: {reason or '无备注'}", operator)
        except Exception as e:
            # I2 唯一索引冲突：另一张同(代码,号码)的有效发票存在
            return {"ok": False, "message": f"无法确认为非重复（可能与其他有效发票冲突）: {e}"}
        return {"ok": True, "message": "已确认非重复，发票转为 NORMAL"}


def delete_invoice(invoice_id: int, operator: str = None) -> dict:
    """删除发票（I8: 匹配记录级联删除；若已匹配，报销单自动释放）。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return {"ok": False, "message": f"发票 #{invoice_id} 不存在"}
        invoice_repo.delete(conn, invoice_id)
        logger.info("发票 #%s 已删除 (status=%s, by %s)", invoice_id, row["status"], operator)
        return {"ok": True, "message": "发票已删除（匹配记录已级联删除）"}


def delete_invoices(invoice_ids: List[int], operator: str = None) -> dict:
    """批量删除发票：逐条删除，单条失败不影响其余。"""
    operator = operator or settings.default_operator
    deleted, failed = 0, []
    for invoice_id in invoice_ids:
        r = delete_invoice(invoice_id, operator)
        if r["ok"]:
            deleted += 1
        else:
            failed.append(invoice_id)
    logger.info("批量删除发票: 成功 %s, 失败 %s (by %s)", deleted, failed, operator)
    return {"ok": not failed, "deleted": deleted,
            "failed": failed,
            "message": f"已删除 {deleted} 张" + (f"，{len(failed)} 张失败: {failed}" if failed else "")}


def invoice_detail(invoice_id: int) -> Optional[dict]:
    """发票详情（含状态日志、解析信息）。"""
    with db.read() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return None
        row["raw"] = _raw_json(row)
        row["status_log"] = log_repo.list_for_invoice(conn, invoice_id)
        return row


def list_invoices(status: str = None, keyword: str = None) -> List[dict]:
    with db.read() as conn:
        return invoice_repo.list(conn, status=status, keyword=keyword)


def invoice_status_counts() -> Dict[str, int]:
    with db.read() as conn:
        return invoice_repo.count_by_status(conn)


def export_excel(status: str = None, keyword: str = None) -> bytes:
    """导出发票清单 Excel（支持按当前筛选条件导出，上限 10 万条）。"""
    import io
    from openpyxl import Workbook
    with db.read() as conn:
        rows = invoice_repo.list(conn, status=status, keyword=keyword, limit=100000)
    wb = Workbook()
    ws = wb.active
    ws.title = "发票清单"
    ws.append(["ID", "状态", "发票号码", "开票日期",
               "购买方名称", "购买方税号", "销售方名称", "销售方税号",
               "金额", "税额", "价税合计", "税率", "开票内容",
               "导入时间"])
    for i in rows:
        ws.append([
            i["id"], i["status"], i["invoice_no"],
            i["invoice_date"], i["buyer_name"], i["buyer_tax_no"],
            i["seller_name"], i["seller_tax_no"],
            str(i["amount"]) if i["amount"] is not None else "",
            str(i["tax_amount"]) if i["tax_amount"] is not None else "",
            str(i["total_amount"]) if i["total_amount"] is not None else "",
            str(i["tax_rate"]) if i.get("tax_rate") is not None else "",
            i["item_summary"] or "",
            i.get("created_at") or "",
        ])
    for col, w in zip("ABCDEFGHIJKLMN",
                      (6, 10, 22, 12, 28, 20, 28, 20, 12, 10, 12, 8, 30, 20)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
