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
from pathlib import Path
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
from app.domain.categorize import categorize_item
from app.repository import db
from app.repository.repos import (alias_repo, invoice_repo, log_repo,
                                  match_repo, reimburse_repo, rule_repo)
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


def _friendly_parse_error(file_type: str, error: str) -> str:
    """把解析失败的技术原因映射为面向用户的友好提示（不暴露代码/堆栈）。

    具体技术原因仍完整记录在发票 raw_json 与状态日志中，供排查使用。
    """
    e = (error or "").lower()
    if any(k in e for k in ("encrypt", "password", "decrypt")) or "密码" in (error or ""):
        return "文件已加密或受密码保护，无法读取；请解除密码后重新上传"
    if file_type in ("jpg", "jpeg", "png"):
        return "图片无法识别，请确认照片清晰、完整、无遮挡反光后重新上传"
    if file_type == "pdf":
        if "扫描件" in (error or ""):
            return "PDF 为扫描件且无可用文本层，请上传电子发票原件，或拍照后以图片格式上传"
        return "PDF 文件损坏或内容无法识别，请重新导出，或截图后以图片格式上传"
    if file_type == "ofd":
        return "OFD 文件损坏或格式不正确，请从开票平台重新下载后上传"
    if file_type == "xml":
        return "XML 文件损坏或格式不正确，请确认是开票系统导出的发票原件后重新上传"
    return "文件损坏或内容无法识别，请检查文件后重新上传"


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
def import_files(files: List[tuple[str, str, bytes]], operator: str = None,
                 skip_match: bool = False) -> List[dict]:
    """导入发票文件（每个文件独立事务，互不阻塞）。

    files: [(文件名, content_type, 字节内容)]
    skip_match: 跳过导入后的自动匹配（前端逐文件上传显示进度时用，最后统一触发一次）
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
            # 单个文件失败不阻塞整批；给用户友好提示，不抛堆栈/代码错误（技术原因入日志）
            logger.exception("导入 %s 失败", file_name)
            results.append({"file_name": file_name, "ok": False,
                            "invoice_id": None, "status": None,
                            "message": _friendly_parse_error(_file_store.detect_type(file_name), str(e))})
    # 有新发票导入成功后自动执行一轮匹配，报销单页状态随之刷新
    if not skip_match and any(r.get("ok") and not r.get("skipped") for r in results):
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
            return {"file_name": file_name, "ok": True, "skipped": True,
                    "invoice_id": existed["id"], "status": existed["status"],
                    "message": f"文件已存在（发票 #{existed['id']}），已自动跳过"}

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
            # 重复发票（同号码）不跳过：照常导入，由 _validate_and_dedup 标记为 DUPLICATE 状态
            raw["degrade_chain"] = result.degrade_chain
            if result.item_summary_raw:
                raw["item_summary_raw"] = result.item_summary_raw
            # 用户词典记忆：报销内容命中自定义词典时直接展示用户改过的名字
            alias = alias_repo.get(conn, result.dto.item_summary or "")
            if alias:
                result.dto.item_summary = alias
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
                "status": InvoiceStatus.ABNORMAL.value,
                "message": _friendly_parse_error(file_type, result.error)}


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


def update_item_summary(invoice_id: int, item_summary: str, operator: str = None) -> dict:
    """用户手动修改"报销内容"（任意状态都允许，不改状态机）。

    保存逻辑：
    - item_summary 字段存清洗后的截断版（去星号、去多余空白、≤30 字）
    - raw_json.item_summary_user 存用户输入的原始串（前端 tooltip 展示用）
    - raw_json.item_summary_user_edit 标记为 True，后续重解析时不再覆盖
    """
    operator = operator or settings.default_operator
    with db.tx() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return {"ok": False, "message": f"发票 #{invoice_id} 不存在"}

        # 清洗（去星号/多余空白/截断），复用与解析同一套规则
        cleaned = _normalize._clean_summary(item_summary or "")
        if not cleaned:
            cleaned = clean(item_summary or "")[:30]

        # 更新 raw_json：保留原始值 + 标记为用户编辑
        raw = _raw_json(row)
        raw["item_summary_user"] = clean(item_summary or "")
        raw["item_summary_user_edit"] = True

        old_summary = row.get("item_summary") or ""
        invoice_repo.update(conn, invoice_id, {
            "item_summary": cleaned,
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "updated_at": "datetime('now','localtime')",
        })
        # 词典记忆：记录 原始词 -> 新词，同步历史同名词条；以后导入自动套用
        learned = False
        if cleaned and old_summary and cleaned != old_summary:
            alias_repo.upsert(conn, old_summary, cleaned)
            renamed = invoice_repo.rename_item_summary(conn, old_summary, cleaned)
            learned = True
            logger.info("词典学习: %r -> %r（同步历史发票 %s 张）", old_summary, cleaned, renamed)
        logger.info("发票 #%s 报销内容已由 %s 修改: %r -> %r",
                    invoice_id, operator, old_summary, cleaned)
        msg = "已保存，系统已记住该修改，以后导入同类发票将自动应用" if learned else "已保存"
        return {"ok": True, "message": msg, "item_summary": cleaned}


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
    """删除发票（I8: 匹配记录级联删除；若已匹配，报销单自动释放）。

    安全顺序（业务规则）：先查物理文件路径 -> 事务内删数据库（含级联，失败整体回滚）
    -> 事务提交后删物理文件（容错，仅记日志，绝不回滚数据库）。
    删除后自动扫描 DUPLICATE 状态的孤儿记录（dup_of == 被删 id），
    对它们重新跑校验+去重，条件满足时恢复为 NORMAL。
    """
    operator = operator or settings.default_operator
    recovered = []
    with db.tx() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return {"ok": False, "message": f"发票 #{invoice_id} 不存在"}
        # 规则 2/3：删除前先查出物理文件路径（人工录入的发票无文件，为 None）
        source_path = row.get("source_path")

        # 规则 1：同一事务内显式清理关联数据（匹配记录、状态日志），
        # 与外键 ON DELETE CASCADE 双保险，任何一步失败整个事务回滚
        n_match = match_repo.delete_for_invoice(conn, invoice_id)
        log_repo.delete_for_invoice(conn, invoice_id)
        invoice_repo.delete(conn, invoice_id)
        logger.info("发票 #%s 已删除 (status=%s, 清理匹配记录 %s 条, by %s)",
                    invoice_id, row["status"], n_match, operator)

        # 恢复孤儿 DUPLICATE：raw_json.dup_of == 被删 id 的 DUPLICATE 记录重新去重
        dup_rows = conn.execute(
            "SELECT id, raw_json FROM invoice WHERE status = 'DUPLICATE'"
        ).fetchall()
        for dup_id, raw_json in dup_rows:
            try:
                raw = json.loads(raw_json) if raw_json else {}
            except (json.JSONDecodeError, TypeError):
                raw = {}
            if raw.get("dup_of") != invoice_id:
                continue
            new_status, _msg = _validate_and_dedup(conn, dup_id, operator)
            if new_status != "DUPLICATE":
                recovered.append((dup_id, new_status))
                logger.info("删除发票 #%s 后，孤儿 DUPLICATE #%s 恢复为 %s",
                            invoice_id, dup_id, new_status)

    # 规则 3：数据库确认删除成功后，再删物理文件；文件删除失败仅记日志
    file_cleaned = _file_store.delete(source_path) if source_path else False

    msg = "发票已删除（匹配记录已级联删除）"
    if source_path and file_cleaned:
        msg += "，物理文件已清理"
    if recovered:
        msg += f"；{len(recovered)} 张重复发票已自动恢复: " + \
               ", ".join(f"#{i}->{s}" for i, s in recovered)
    return {"ok": True, "message": msg, "recovered": recovered}


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


def invoice_preview(invoice_id: int) -> Optional[dict]:
    """抽屉详情：结构化字段 + 生效匹配记录（报销单号/提交人/金额）+ 原件文件信息。"""
    with db.read() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None:
            return None
        row.pop("raw_json", None)
        m = match_repo.active_for_invoice(conn, invoice_id)
        match = None
        if m:
            reb = reimburse_repo.get(conn, m["reimburse_id"]) if m.get("reimburse_id") else None
            match = {
                "status": m.get("status"),
                "match_type": m.get("match_type"),
                "match_score": m.get("match_score"),
                "reimburse_no": (reb or {}).get("reimburse_no") or m.get("reimburse_no"),
                "submitter": (reb or {}).get("submitter"),
                "amount": (reb or {}).get("amount"),
            }
        return {"invoice": row, "match": match,
                "file_type": (row.get("file_type") or "").lower(),
                "has_file": invoice_file_path(invoice_id) is not None}


def invoice_file_path(invoice_id: int) -> Optional[Path]:
    """发票原件的磁盘绝对路径（安全约束：仅允许上传目录内的文件）。"""
    with db.read() as conn:
        row = invoice_repo.get(conn, invoice_id)
    if row is None:
        return None
    src = row.get("source_path") or ""
    if not src:
        return None
    try:
        p = Path(src).resolve()
        root = Path(settings.upload_dir).resolve()
        if p != root and root not in p.parents:
            return None
        return p if p.is_file() else None
    except OSError:
        return None


def _category_of(item_summary: str, alias_map: Dict[str, str]) -> str:
    """报销内容 -> 标准分类（先套用户词典，再走智能归类，与仪表盘口径一致）。"""
    name = (item_summary or "").strip() or "其他"
    return categorize_item(alias_map.get(name, name))


def list_invoices(status: str = None, keyword: str = None,
                  category: str = None, date_from: str = None, date_to: str = None,
                  limit: int = 500, offset: int = 0) -> List[dict]:
    with db.read() as conn:
        if category:
            # 分类是派生值（用户词典 + 智能归类），需全量取出后过滤再分页
            rows = invoice_repo.list(conn, status=status, keyword=keyword,
                                     date_from=date_from, date_to=date_to,
                                     limit=100000, offset=0)
            alias_map = alias_repo.all(conn)
            rows = [r for r in rows
                    if _category_of(r.get("item_summary"), alias_map) == category]
            rows = rows[offset:offset + limit]
        else:
            rows = invoice_repo.list(conn, status=status, keyword=keyword,
                                     date_from=date_from, date_to=date_to,
                                     limit=limit, offset=offset)
    # 给每条补上 item_summary_raw（优先用户编辑值，其次解析原始值，最后清洗后值兜底）
    for r in rows:
        raw = _raw_json(r)
        user_val = raw.get("item_summary_user") or ""
        parsed_val = raw.get("item_summary_raw") or ""
        r["item_summary_raw"] = user_val or parsed_val or r.get("item_summary") or ""
    return rows


def count_invoices(status: str = None, keyword: str = None, category: str = None,
                   date_from: str = None, date_to: str = None) -> int:
    """发票总数（与 list_invoices 同筛选口径，分页用）。"""
    with db.read() as conn:
        if category:
            rows = invoice_repo.list(conn, status=status, keyword=keyword,
                                     date_from=date_from, date_to=date_to,
                                     limit=100000, offset=0)
            alias_map = alias_repo.all(conn)
            return sum(1 for r in rows
                       if _category_of(r.get("item_summary"), alias_map) == category)
        return invoice_repo.count(conn, status=status, keyword=keyword,
                                  date_from=date_from, date_to=date_to)


def page_of_invoice(invoice_id: int, status: str = None, keyword: str = None,
                    page_size: int = 20, category: str = None,
                    date_from: str = None, date_to: str = None) -> Optional[int]:
    """focus 定位：发票在筛选结果（id 升序）中所在的页码（1 起），不存在返回 None。"""
    with db.read() as conn:
        if invoice_repo.get(conn, invoice_id) is None:
            return None
        if category:
            rows = invoice_repo.list(conn, status=status, keyword=keyword,
                                     date_from=date_from, date_to=date_to,
                                     limit=100000, offset=0)
            alias_map = alias_repo.all(conn)
            rows = [r for r in rows
                    if _category_of(r.get("item_summary"), alias_map) == category]
            pos = next((i for i, r in enumerate(rows) if r["id"] == invoice_id), None)
            return (pos // page_size + 1) if pos is not None else None
        pos = invoice_repo.position_of(conn, invoice_id, status=status, keyword=keyword,
                                       date_from=date_from, date_to=date_to)
    return pos // page_size + 1


def invoice_status_counts() -> Dict[str, int]:
    with db.read() as conn:
        return invoice_repo.count_by_status(conn)


def expense_breakdown(date_from: str = None, date_to: str = None, top_n: int = 5) -> dict:
    """费用构成统计（基于 invoice 表，按 item_summary 分组）。

    业务规则：
    - 分类依据：发票明细里的 item_summary（报销内容/摘要名称）
    - 金额计算：发票的价税合计 total_amount
    - 时间过滤：基于发票的开票日期 invoice_date
    - 空 item_summary 统一归类为 "其他"

    返回结构：
    {
        "total_amount": float,          # 时间段内价税合计总额（元）
        "total_count": int,              # 时间段内总发票张数
        "label_when_all": str,           # 中心标签（历史总发票/本月总发票）
        "items": [                       # 按金额降序，超过 top_n 合并为 "其他"
            {"name": str, "amount": float, "count": int, "percent": float},
            ...
        ]
    }
    """
    with db.read() as conn:
        rows = invoice_repo.aggregate_by_item_summary(conn, date_from, date_to)
        alias_map = alias_repo.all(conn)

    # 智能归类：先套用用户自定义词典（第一优先级，覆盖内置关键词），
    # 再把报销内容映射为标准财务分类后汇总（严禁把原始发票词直接丢给图表）。
    merged: Dict[str, dict] = {}
    for r in rows:
        cat = categorize_item(alias_map.get(r["name"], r["name"]))
        m = merged.setdefault(cat, {"name": cat, "amount": 0.0, "count": 0})
        m["amount"] += float(r["amount"] or 0)
        m["count"] += int(r["count"] or 0)
    rows = sorted(merged.values(), key=lambda x: x["amount"], reverse=True)

    total_amount = sum(r["amount"] for r in rows)
    total_count = sum(r["count"] for r in rows)

    # 标准分类最多 6 个，无需 top_n 截断合并，全部展示（top_n 参数保留兼容）
    items = [{
        "name": r["name"],
        "amount": round(r["amount"], 2),
        "count": r["count"],
        "percent": round(r["amount"] / total_amount * 100, 2) if total_amount > 0 else 0,
    } for r in rows]

    # 中心标签：按前端 filter 口径推断（"总支出" 业务表述）
    from datetime import date
    import datetime as _dt
    label_when_all = "历史总支出"
    if date_from and date_to:
        today = date.today()
        # 本月
        if (date_from == today.replace(day=1).isoformat()
                and date_to == today.isoformat()):
            label_when_all = "本月总支出"
        # 上月
        else:
            first_of_month = today.replace(day=1)
            last_month_end = first_of_month - _dt.timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            if (date_from == last_month_start.isoformat()
                    and date_to == last_month_end.isoformat()):
                label_when_all = "上月总支出"
            # 今年
            elif (date_from == f"{today.year}-01-01"
                  and date_to == today.isoformat()):
                label_when_all = "今年总支出"

    return {
        "total_amount": round(total_amount, 2),
        "total_count": total_count,
        "label_when_all": label_when_all,
        "items": items,
    }


def export_excel(status: str = None, keyword: str = None, ids: list = None) -> bytes:
    """导出发票清单 Excel（支持按当前筛选条件或指定 ids 导出，上限 10 万条）。"""
    import io
    from openpyxl import Workbook
    with db.read() as conn:
        rows = invoice_repo.list(conn, status=status, keyword=keyword, limit=100000, ids=ids)
    wb = Workbook()
    ws = wb.active
    ws.title = "发票清单"
    ws.append(["ID", "状态", "发票号码", "开票日期",
               "购买方名称", "购买方税号", "销售方名称", "销售方税号",
               "金额", "税额", "价税合计", "税率", "报销内容",
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
