"""发票导入与人工处理用例（应用服务层）。

编排：解析 -> 标准化 -> 校验 -> 去重 -> 状态机，管理事务边界（I9）。
算法全部委托领域引擎（纯函数），本层不写算法。
"""
import hashlib
import json
import sqlite3
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
from app.domain.categorize import (STANDARD_CATEGORIES, STATUS_CONFIRMED,
                                   STATUS_LEGACY, STATUS_PENDING, categorize_item,
                                   known_category, normalize_category, resolve_category)
from app.repository import db
from app.repository.repos import (alias_repo, category_dict_repo, category_repo,
                                  conflict_repo, invoice_repo, log_repo, match_repo,
                                  reimburse_category_repo, reimburse_repo, rule_repo)
from app.service import audit_service
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
    if ("winerror 32" in e or "being used" in e or "另一个程序" in (error or "")
            or "permission denied" in e or "access is denied" in e):
        return "文件正被其他程序占用或目录无写入权限，请关闭占用程序后重试"
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
                    scope=r.get("scope") or "invoice",
                    action=r.get("action") or "block",
                    priority=r["priority"], stop_on_fail=bool(r["stop_on_fail"]),
                    config=r["config"], tags=r.get("tags") or [],
                    is_core=bool(r.get("is_core")), enabled=bool(r["enabled"]))
            for r in rule_repo.list_enabled(conn)]


def _raw_json(row: dict) -> dict:
    try:
        return json.loads(row.get("raw_json") or "{}")
    except json.JSONDecodeError:
        return {}


def _backfill_dup_fields(row: dict) -> None:
    """DUPLICATE 记录：invoice_code/invoice_no 为避免撞 DB 唯一约束未落列，
    真实号码保存在 raw_json.dup_invoice_code/dup_invoice_no，显示时回填。"""
    raw = _raw_json(row)
    if not row.get("invoice_no") and raw.get("dup_invoice_no"):
        row["invoice_no"] = raw["dup_invoice_no"]
    if not row.get("invoice_code") and raw.get("dup_invoice_code"):
        row["invoice_code"] = raw["dup_invoice_code"]


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
        from app.service import match_service
        try:
            t0 = time.perf_counter()
            # 已有匹配任务在跑时不排队等待：本轮导入的发票由下一轮重算覆盖
            ms = match_service.run_matching(operator, wait_seconds=0)
            logger.info("发票导入后自动匹配: %s (%.0fms)",
                        ms, (time.perf_counter() - t0) * 1000)
        except match_service.MatchBusy:
            logger.info("已有匹配任务正在进行，跳过本次导入后自动匹配")
        except Exception as e:
            logger.warning("发票导入后自动匹配失败: %s", e)
    return results


def assign_owner(invoice_ids: List[int], user_id: int) -> None:
    """把新导入的发票归属到当前登录员工（数据隔离用）。"""
    ids = [int(i) for i in invoice_ids if i]
    if not ids or not user_id:
        return
    with db.tx() as conn:
        conn.execute(
            "UPDATE invoice SET owner_user_id = ? WHERE owner_user_id IS NULL AND id IN ("
            + ", ".join("?" for _ in ids) + ")",
            [user_id] + ids,
        )


def _import_one(file_name: str, content: bytes, operator: str) -> dict:
    file_type = _file_store.detect_type(file_name)
    if not _file_store.is_supported(file_name):
        return {"file_name": file_name, "ok": False, "invoice_id": None,
                "status": None, "message": f"不支持的文件类型: {file_type or '未知'}"}

    file_hash = _file_store.sha256(content)
    # 先只读查一次重复：重复文件不必落盘，避免产生无引用的孤儿文件
    with db.read() as conn:
        existed = invoice_repo.get_by_hash(conn, file_hash)
    # 上次解析失败（ABNORMAL）不算重复：允许用户重传同一文件重新解析，
    # 否则失败记录会一直占着文件哈希，前端只会提示「跳过重复」而发票永远进不来。
    if existed and existed["status"] in (InvoiceStatus.ABNORMAL.value,
                                        InvoiceStatus.PARSING.value):
        existed = None
    if existed:
        return {"file_name": file_name, "ok": True, "skipped": True,
                "invoice_id": existed["id"], "status": existed["status"],
                "message": f"文件已存在（发票 #{existed['id']}），已自动跳过"}

    t_save = time.perf_counter()
    source_path = str(_file_store.save(file_name, content))
    save_ms = (time.perf_counter() - t_save) * 1000
    task = ParseTask(file_name=file_name, file_type=file_type, content=content)

    # 解析（OCR / PDF / OFD 可能耗时数秒到数十秒）必须在事务外完成：
    # 事务内解析会让 BEGIN IMMEDIATE 的写锁被长时间占用，其他写入只能等
    # busy_timeout 后报 database is locked。
    t_parse = time.perf_counter()
    result = _parser_engine.parse(task)
    parse_ms = (time.perf_counter() - t_parse) * 1000
    logger.info("导入分段 %s: 存盘=%.0fms 解析=%.0fms", file_name, save_ms, parse_ms)

    with db.tx() as conn:
        # 不变量 I1 二次确认（并发上传同一文件时兜底）：清理刚写入的副本再跳过
        existed = invoice_repo.get_by_hash(conn, file_hash)
        if existed and existed["status"] in (InvoiceStatus.ABNORMAL.value,
                                             InvoiceStatus.PARSING.value):
            # 软删除旧的失败记录（唯一索引只约束未删除行），本次重新解析入库
            invoice_repo.soft_delete(conn, existed["id"])
            logger.info("重新解析此前失败的发票 #%s（文件 %s）", existed["id"], file_name)
            existed = None
        if existed:
            _file_store.delete(source_path)
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

        t_write = time.perf_counter()

        if result.success:
            # 重复发票（同号码）不跳过：照常导入，由 _validate_and_dedup 标记为 DUPLICATE 状态
            raw["degrade_chain"] = result.degrade_chain
            if result.item_summary_raw:
                raw["item_summary_raw"] = result.item_summary_raw
            # 用户词典记忆：报销内容命中自定义词典时直接展示用户改过的名字
            alias = alias_repo.get(conn, result.dto.item_summary or "")
            if alias:
                result.dto.item_summary = alias
            # 无票号票据特征（车牌号 / 上下车时间 / 票面文字）随 raw_json 持久化，
            # 匹配时由 _inv_dto 回读，供「业务特征碰撞」使用
            if (result.dto.plate_no or result.dto.ride_time
                    or result.dto.traffic_text):
                raw["traffic"] = {
                    "plate_no": result.dto.plate_no,
                    "ride_time": result.dto.ride_time,
                    "text": result.dto.traffic_text,
                }
            t_u = time.perf_counter()
            # update 前主动去重：同号票存在时走 PARSED → DUPLICATE，
            # 避免 SQLite UNIQUE 约束在 update 时拦截（IntegrityError）。
            same_no_hit = None
            if result.dto.invoice_no:
                existing = invoice_repo.fingerprints(conn, exclude_id=invoice_id)
                for exist_id, _hash, code, no in existing:
                    if no and code == (result.dto.invoice_code or "") \
                            and no == result.dto.invoice_no:
                        same_no_hit = exist_id
                        break

            if same_no_hit:
                # 同号票已存在：除 invoice_code/invoice_no 外其余解析字段全部落库
                # （金额/日期/买卖方等用户关心字段照常显示），代码号仅保留在 raw_json
                # 中避免撞 UNIQUE 约束。状态机走 PARSED → DUPLICATE。
                raw["dup_reason"] = (f"同号票 #{same_no_hit} 已存在，导入后标 DUPLICATE "
                                     f"（号={result.dto.invoice_no}）")
                raw["dup_invoice_code"] = result.dto.invoice_code or ""
                raw["dup_invoice_no"] = result.dto.invoice_no or ""
                row_fields = _row_fields_from_dto(result.dto)
                row_fields.pop("invoice_code", None)
                row_fields.pop("invoice_no", None)
                invoice_repo.update(conn, invoice_id, {
                    **row_fields,
                    "parse_engine": result.engine,
                    "parse_confidence": result.confidence,
                    "raw_json": json.dumps(raw, ensure_ascii=False),
                })
                change_invoice_status(conn, invoice_id, InvoiceStatus.PARSED.value,
                                      f"解析成功（engine={result.engine}，同号票 #{same_no_hit}）",
                                      operator)
                change_invoice_status(conn, invoice_id, InvoiceStatus.DUPLICATE.value,
                                      f"同号码发票已存在（#{same_no_hit}），自动标记重复",
                                      operator)
                _scan_risk(conn, invoice_id)
                return {"file_name": file_name, "ok": True, "invoice_id": invoice_id,
                        "status": InvoiceStatus.DUPLICATE.value,
                        "message": f"同号码发票已存在（#{same_no_hit}），已自动标记为重复"}

            try:
                invoice_repo.update(conn, invoice_id, {
                    **_row_fields_from_dto(result.dto),
                    "parse_engine": result.engine,
                    "parse_confidence": result.confidence,
                    "raw_json": json.dumps(raw, ensure_ascii=False),
                })
            except sqlite3.IntegrityError as e:
                # 并发场景：上面主动查重后仍撞约束（同号并发插入），兜底走 DUPLICATE 流程
                msg = str(e)
                if "invoice_code" not in msg and "invoice_no" not in msg:
                    raise
                logger.warning("发票 %s 撞同号约束（%s），兜底走 DUPLICATE", file_name, msg)
                raw["dup_invoice_code"] = result.dto.invoice_code or ""
                raw["dup_invoice_no"] = result.dto.invoice_no or ""
                row_fields = _row_fields_from_dto(result.dto)
                row_fields.pop("invoice_code", None)
                row_fields.pop("invoice_no", None)
                invoice_repo.update(conn, invoice_id, {
                    **row_fields,
                    "parse_engine": result.engine,
                    "parse_confidence": result.confidence,
                    "raw_json": json.dumps(raw, ensure_ascii=False),
                })
                change_invoice_status(conn, invoice_id, InvoiceStatus.PARSED.value,
                                      f"解析成功（engine={result.engine}，同号票撞约束）", operator)
                change_invoice_status(conn, invoice_id, InvoiceStatus.DUPLICATE.value,
                                      f"同号票撞 DB 约束，标记 DUPLICATE", operator)
                _scan_risk(conn, invoice_id)
                return {"file_name": file_name, "ok": True, "invoice_id": invoice_id,
                        "status": InvoiceStatus.DUPLICATE.value,
                        "message": "同号码发票已存在，已自动标记为重复"}
            t_s = time.perf_counter()
            change_invoice_status(conn, invoice_id, InvoiceStatus.PARSED.value,
                                  f"解析成功（engine={result.engine}）", operator)
            t_v = time.perf_counter()
            status, message = _validate_and_dedup(conn, invoice_id, operator)
            _scan_risk(conn, invoice_id)
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


def _scan_risk(conn, invoice_id: int) -> None:
    """导入完成后执行轻量风控扫描；开关关闭时内部直接跳过，异常不影响导入。"""
    try:
        from app.service import risk_service
        risk_service.scan_invoice(conn, invoice_id)
    except Exception as e:
        logger.warning("风控扫描失败（不影响导入）: %s", e)


def _validate_and_dedup(conn, invoice_id: int, operator: str) -> tuple[str, str]:
    """PARSED 之后：校验（内置 C1-C5 + 规则）-> 去重 -> NORMAL / ABNORMAL / DUPLICATE。"""
    row = invoice_repo.get(conn, invoice_id)
    dto = _dto_from_row(row)

    issues = _validator.validate(dto) + _rule_engine.validate(dto, _rules(conn))
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
        _scan_risk(conn, invoice_id)
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

        issues = _validator.validate(dto) + _rule_engine.validate(dto, _rules(conn))
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
        # updated_at 由仓储层 update() 自动刷新，这里无需传
        invoice_repo.update(conn, invoice_id, {
            "item_summary": cleaned,
            "raw_json": json.dumps(raw, ensure_ascii=False),
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
    """删除发票（软删除：写入 deleted_at，绝不物理删除）。

    I8: 匹配记录级联删除；已匹配的报销单自动释放。原始附件保留，
    仅在回收站「彻底删除」或自动清理到期后才物理删除。
    删除后自动扫描 DUPLICATE 状态的孤儿记录（dup_of == 被删 id），
    对它们重新跑校验+去重，条件满足时恢复为 NORMAL。
    """
    operator = operator or settings.default_operator
    recovered = []
    batch_id = uuid.uuid4().hex
    with db.tx() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None or row.get("deleted_at"):
            return {"ok": False, "message": f"发票 #{invoice_id} 不存在"}
        # 生效匹配（人工确认/自动匹配）先快照：若存在同号重复发票，删除后应由其接管
        active_snaps = []
        for a in conn.execute(
                "SELECT * FROM match_result WHERE invoice_id=? "
                "AND status IN ('MATCHED','CONFIRMED')", (invoice_id,)).fetchall():
            active_snaps.append({
                "row": dict(a),
                "pairs": [p[0] for p in conn.execute(
                    "SELECT reimburse_id FROM match_pair WHERE match_id=?", (a["id"],)).fetchall()],
            })
        # 规则 1：同一事务内显式清理匹配记录（回收站恢复时发票回到未匹配）
        n_match = match_repo.delete_for_invoice(conn, invoice_id)
        invoice_repo.soft_delete(conn, invoice_id)
        # 删除即退出闭环：清空闭环标记，避免回收站里的发票仍被计为已闭环
        conn.execute("UPDATE invoice SET is_closed = 0, matched_amount = '0.00' WHERE id = ?",
                     (invoice_id,))
        audit_service.log(conn, "delete", "invoice", invoice_id,
                          row.get("invoice_no") or f"#{invoice_id}",
                          "有效", "已删除（移入回收站）", operator,
                          "删除发票，附件保留至彻底删除或自动清理")
        logger.info("发票 #%s 已移入回收站 (status=%s, 清理匹配记录 %s 条, by %s)",
                    invoice_id, row["status"], n_match, operator)

        # 恢复孤儿 DUPLICATE：raw_json.dup_of == 被删 id 的 DUPLICATE 记录重新去重
        dup_rows = conn.execute(
            "SELECT id, raw_json FROM invoice WHERE status = 'DUPLICATE' AND deleted_at IS NULL"
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

        # 重复发票接管：被删的是已确认/自动匹配的发票时，同号重复票继承该生效匹配，
        # 避免报销单被释放、重复票却停在未匹配（账实不符）
        takeover = None
        if active_snaps:
            cand_id = next((i for i, s in recovered if s == "NORMAL"), None)
            no = (row.get("invoice_no") or "").strip()
            if cand_id is None and no:
                cand = conn.execute(
                    "SELECT id FROM invoice WHERE id<>? AND deleted_at IS NULL "
                    "AND status='NORMAL' AND invoice_no=? ORDER BY id LIMIT 1",
                    (invoice_id, no)).fetchone()
                cand_id = cand[0] if cand else None
            if cand_id:
                for snap in active_snaps:
                    m = snap["row"]
                    cur = conn.execute(
                        "INSERT INTO match_result(invoice_id, reimburse_id, match_type, match_score,"
                        " status, diff_fields, match_points, message, warning_msg, detail,"
                        " confirmed_by, confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cand_id, m["reimburse_id"], m["match_type"], m["match_score"],
                         m["status"], m["diff_fields"], m["match_points"],
                         (m["message"] or "") +
                         f"\n[接管] 原发票 #{invoice_id} 已删除，由重复发票 #{cand_id} 承接该匹配",
                         m["warning_msg"], m["detail"], m["confirmed_by"], m["confirmed_at"]))
                    for rid in snap["pairs"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO match_pair(match_id, invoice_id, reimburse_id)"
                            " VALUES(?,?,?)", (cur.lastrowid, cand_id, rid))
                change_invoice_status(conn, cand_id, "MATCHED",
                                      f"重复发票承接 #{invoice_id} 的生效匹配", operator)
                takeover = cand_id
                logger.info("发票 #%s 删除后，重复发票 #%s 接管其生效匹配", invoice_id, cand_id)

        # 删除/接管改变生效明细集合；提交后由统一引擎生成或清理差异。
        from app.service import category_service
        affected = set()
        for snap in active_snaps:
            if snap["row"].get("reimburse_id") is not None:
                affected.add(snap["row"]["reimburse_id"])
            affected.update(snap["pairs"])
        affected.discard(None)
        category_service.defer_recheck(sorted(affected))

    msg = "发票已移入回收站（匹配记录已清理，可在 20 秒内撤销）"
    if takeover:
        msg += f"；重复发票 #{takeover} 已接管原匹配关系"
    if recovered:
        msg += f"；{len(recovered)} 张重复发票已自动恢复: " + \
               ", ".join(f"#{i}->{s}" for i, s in recovered)
    return {"ok": True, "message": msg, "recovered": recovered, "takeover": takeover,
            "ids": [invoice_id], "batch_id": batch_id}


def delete_invoices(invoice_ids: List[int], operator: str = None) -> dict:
    """批量软删除发票：逐条处理，单条失败不影响其余。"""
    operator = operator or settings.default_operator
    deleted, failed, deleted_ids = 0, [], []
    batch_id = uuid.uuid4().hex
    for invoice_id in invoice_ids:
        r = delete_invoice(invoice_id, operator)
        if r["ok"]:
            deleted += 1
            deleted_ids.append(invoice_id)
        else:
            failed.append(invoice_id)
    logger.info("批量删除发票: 成功 %s, 失败 %s (by %s)", deleted, failed, operator)
    return {"ok": not failed, "deleted": deleted, "failed": failed,
            "ids": deleted_ids, "batch_id": batch_id,
            "message": f"已删除 {deleted} 张" + (f"，{len(failed)} 张失败: {failed}" if failed else "")}


def restore_invoices(invoice_ids: List[int], operator: str = None) -> dict:
    """撤销删除：清空 deleted_at（回收站 20 秒撤销 / 回收站恢复共用）。"""
    operator = operator or settings.default_operator
    restored = []
    with db.tx() as conn:
        for invoice_id in invoice_ids:
            row = invoice_repo.get(conn, invoice_id)
            if row is None or not row.get("deleted_at"):
                continue
            invoice_repo.restore(conn, [invoice_id])
            # 原状态为 MATCHED 但匹配记录已随删除清理：回到 NORMAL，避免出现"已匹配"假象
            if row.get("status") == "MATCHED":
                active = conn.execute(
                    "SELECT 1 FROM match_result WHERE invoice_id = ? "
                    "AND status IN ('MATCHED','CONFIRMED') LIMIT 1", (invoice_id,)).fetchone()
                if not active:
                    conn.execute("UPDATE invoice SET status = 'NORMAL', is_closed = 0, "
                                 "matched_amount = '0.00' WHERE id = ?", (invoice_id,))
            else:
                # 恢复的发票重新进入候选池：闭环标记必须清零
                conn.execute("UPDATE invoice SET is_closed = 0, matched_amount = '0.00' "
                             "WHERE id = ?", (invoice_id,))
            restored.append(invoice_id)
            audit_service.log(conn, "restore", "invoice", invoice_id,
                              row.get("invoice_no") or f"#{invoice_id}",
                              "已删除", "已恢复", operator, "撤销删除（回收站恢复）")
    result = {"ok": True, "restored": len(restored), "ids": restored,
              "message": f"已恢复 {len(restored)} 张发票" if restored else "没有可恢复的发票"}
    if restored:
        # 恢复后重新执行匹配：删除时匹配记录已被清理，恢复到未匹配状态只是中间态，
        # 需要重跑一轮匹配把「已匹配发票 ↔ 报销单」的关联关系还原。
        from app.service import match_service   # 惰性导入，避免服务层循环依赖
        try:
            ms = match_service.run_matching(operator, wait_seconds=10)
            result["match"] = ms
            result["message"] = (
                f"已恢复 {len(restored)} 张发票，并重新匹配"
                f"（自动匹配 {ms.get('auto_matched', 0)} 张、待人工 {ms.get('pending', 0)} 张）")
        except Exception as e:      # 匹配失败不影响恢复结果本身
            logger.warning("恢复发票后重新匹配失败: %s", e)
    return result


def list_deleted_invoices(limit: int = 500, offset: int = 0) -> List[dict]:
    """回收站：已删除发票列表（分页）。"""
    with db.read() as conn:
        return invoice_repo.list_deleted(conn, limit=limit, offset=offset)


def count_deleted_invoices() -> int:
    """回收站：已删除发票总数。"""
    with db.read() as conn:
        return invoice_repo.count_deleted(conn)


def purge_invoices(invoice_ids: List[int], operator: str = None) -> dict:
    """彻底删除（物理删除数据库记录 + 清理附件），仅回收站 / 自动清理调用。"""
    operator = operator or settings.default_operator
    purged, paths = 0, []
    with db.tx() as conn:
        for invoice_id in invoice_ids:
            row = invoice_repo.get(conn, invoice_id)
            if row is None or not row.get("deleted_at"):
                continue
            paths.append(row.get("source_path"))
            match_repo.delete_for_invoice(conn, invoice_id)
            log_repo.delete_for_invoice(conn, invoice_id)
            invoice_repo.delete(conn, invoice_id)
            purged += 1
            audit_service.log(conn, "purge", "invoice", invoice_id,
                              row.get("invoice_no") or f"#{invoice_id}",
                              "已删除", "已彻底删除", operator,
                              "回收站彻底删除：数据库记录与附件已清理")
    for p in paths:
        if p:
            _file_store.delete(p)
    logger.info("回收站彻底删除发票 %s 张（附件 %s 个, by %s）",
                purged, len([p for p in paths if p]), operator)
    return {"ok": True, "purged": purged,
            "message": f"已彻底删除 {purged} 张发票"}


def invoice_detail(invoice_id: int) -> Optional[dict]:
    """发票详情（含状态日志、解析信息）。"""
    with db.read() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None or row.get("deleted_at"):
            return None
        _backfill_dup_fields(row)
        row["raw"] = _raw_json(row)
        row["status_log"] = log_repo.list_for_invoice(conn, invoice_id)
        return row


def invoice_preview(invoice_id: int) -> Optional[dict]:
    """抽屉详情：结构化字段 + 生效匹配记录（报销单号/提交人/金额）+ 原件文件信息。"""
    with db.read() as conn:
        row = invoice_repo.get(conn, invoice_id)
        if row is None or row.get("deleted_at"):
            return None
        _backfill_dup_fields(row)
        row.pop("raw_json", None)
        src = row.pop("source_path", "") or ""   # 绝对路径不下发前端
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
        # 费用分类决策（与列表/仪表盘同一口径）
        exp_rows = invoice_repo.expense_rows(conn, ids=[invoice_id])
        dec = _resolve_rows(exp_rows, category_repo.memory_all(conn),
                            alias_repo.all(conn)).get(invoice_id) or {}
    # 就地判断原件存在性（复用安全约束：仅上传目录内），不再开第二个连接查库
    file_path = _safe_upload_path(src)
    has_file = file_path is not None
    pdf_pages = 0
    if has_file and (row.get("file_type") or "").lower() == "pdf":
        pdf_pages = _pdf_page_count(file_path)
    return {"invoice": row, "match": match, "category": dec,
            "file_type": (row.get("file_type") or "").lower(),
            "has_file": has_file, "pdf_pages": pdf_pages}


def _safe_upload_path(src: str) -> Optional[Path]:
    """把 source_path 解析为上传目录内的真实文件（含 basename 兜底）。

    安全约束：结果必须位于 settings.upload_dir 内；否则返回 None。
    basename 兜底用于附件目录迁移 / 换机后旧绝对路径失效的场景，
    保证历史发票仍能预览、下载与清理。
    """
    if not src:
        return None
    try:
        root = Path(settings.upload_dir).resolve()
        raw = Path(src)
        cand = raw if raw.is_absolute() else (root / raw)
        p = cand.resolve()
        if p.is_file() and (p == root or root in p.parents):
            return p
        name = raw.name
        if name:
            # 附件实际布局只有两级：uploads/<文件名>（交接包）与
            # uploads/YYYYMM/<uuid>_文件名（本机导入）。按目录逐层查找，
            # 避免 rglob 全盘扫描导致大附件量下每次预览都卡顿。
            candidates = [root / name]
            try:
                candidates.extend(
                    child / name for child in root.iterdir() if child.is_dir())
            except OSError:
                pass
            for f in candidates:
                if f.is_file():
                    rf = f.resolve()
                    if rf == root or root in rf.parents:
                        return rf
        return None
    except OSError:
        return None


def invoice_file_path(invoice_id: int) -> Optional[Path]:
    """发票原件的磁盘绝对路径（安全约束：仅允许上传目录内的文件）。"""
    with db.read() as conn:
        row = invoice_repo.get(conn, invoice_id)
    if row is None:
        return None
    return _safe_upload_path(row.get("source_path") or "")


def _pdf_page_count(path: Path) -> int:
    """打开 PDF 读取页数；打不开返回 0。"""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


def render_pdf_page(invoice_id: int, page: int = 0, scale: float = 2.5) -> Optional[bytes]:
    """把 PDF 原件的指定页渲染为 PNG 字节（抽屉预览用，与图片发票共用同一预览组件）。"""
    p = invoice_file_path(invoice_id)   # 复用路径安全约束（仅上传目录内）
    if p is None or p.suffix.lower() != ".pdf":
        return None
    import io
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p))
    except Exception:
        return None
    try:
        if not (0 <= page < len(doc)):
            return None
        img = doc[page].render(scale=scale).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None
    finally:
        doc.close()


def _resolve_rows(rows: List[dict], memory: Dict[tuple, dict],
                  alias_map: Dict[str, str] = None,
                  valid_categories: List[str] = None) -> Dict[int, dict]:
    """批量把发票行解析为分类决策（纯计算，不访问数据库）。

    行需含：id、item_summary、expense_type、submitter、manual_category，
    以及能判断"是否有报销单"的字段（reimburse_id 或 expense_type 非 NULL）。
    valid_categories: 当前字典全量分类名（含禁用），用于保留历史原文。
    """
    alias_map = alias_map or {}
    out: Dict[int, dict] = {}
    for r in rows:
        item = (r.get("item_summary") or "").strip()
        sub = (r.get("submitter") or "").strip()
        if "reimburse_id" in r:
            has_reb = r.get("reimburse_id") is not None
        else:
            # light 查询无 reimburse_id 列：未匹配时 LEFT JOIN 出来的 expense_type 为 NULL
            has_reb = r.get("expense_type") is not None
        # 报销单侧人工分类（reimburse_category）优先于导入的费用类型：
        # 报销单已明确分类时，关联发票/差异报告等页面必须同步展示同一分类
        exp = (r.get("expense_type") or "").strip()
        out[r["id"]] = resolve_category(
            alias_map.get(item, item),
            expense_type=exp,
            submitter=sub,
            manual_category=r.get("manual_category") or "",
            item_memory=memory.get(("item", item)),
            submitter_memory=memory.get(("submitter", sub)) if sub else None,
            has_reimburse=has_reb,
            reimburse_manual_category=r.get("reimburse_manual_category") or "",
            valid_categories=valid_categories,
        )
    return out


def resolve_categories(conn, invoice_ids) -> Dict[int, dict]:
    """按统一口径批量解析发票分类决策（报销单列表等页面联动展示复用）。"""
    if not invoice_ids:
        return {}
    exp_rows = invoice_repo.expense_rows(conn, ids=list(invoice_ids))
    return _resolve_rows(exp_rows, category_repo.memory_all(conn), alias_repo.all(conn),
                         category_dict_repo.names(conn, enabled_only=False))


def _attach_categories(conn, rows: List[dict]) -> None:
    """给发票行附上费用分类决策（列表展示用）：就地写入，避免二次查询。"""
    if not rows:
        return
    ids = [r["id"] for r in rows]
    exp_rows = invoice_repo.expense_rows(conn, ids=ids)
    by_id = {e["id"]: e for e in exp_rows}
    decisions = _resolve_rows(exp_rows, category_repo.memory_all(conn), alias_repo.all(conn),
                              category_dict_repo.names(conn, enabled_only=False))
    disabled = set(category_dict_repo.disabled_names(conn))
    manual_versions = {iid: int(d.get("version") or 0)
                       for iid, d in category_repo.decisions(conn, ids).items()}
    for r in rows:
        d = decisions.get(r["id"]) or {}
        e = by_id.get(r["id"]) or {}
        r["category"] = d.get("category") or ""
        r["category_status"] = d.get("status") or ""
        r["category_source"] = d.get("source") or ""
        r["category_suggest"] = d.get("suggested") or ""
        r["category_hint"] = d.get("hint") or ""
        # 分类已被字典禁用：历史数据保留原文并标灰，禁止清洗成「其他费用」
        if r["category"] and r["category"] in disabled:
            r["category_status"] = STATUS_LEGACY
            r["category_hint"] = "该分类已在字典中禁用，历史数据保留原文展示"
        r["category_raw"] = d.get("raw_category") or ""
        r["category_version"] = manual_versions.get(r["id"], 0)
        r["reimburse_no"] = e.get("reimburse_no") or ""
        r["expense_type"] = e.get("expense_type") or ""
        # 匹配状态与分类差异（已匹配但双方分类不一致 -> 列表黄色标记提示）
        rid = e.get("reimburse_id")
        r["reimburse_id"] = rid
        r["category_matched"] = rid is not None
        conflict = (rid is not None
                    and (e.get("conflict_invoice_category") or "") != ""
                    and (e.get("conflict_resolution") or "") == "")
        r["category_conflict"] = bool(conflict)
        r["category_conflict_detail"] = (
            "发票「%s」与报销单「%s」分类不一致"
            % (e.get("conflict_invoice_category"), e.get("conflict_reimburse_category"))
            if conflict else "")


def list_invoices(status: str = None, keyword: str = None,
                  category: str = None, date_from: str = None, date_to: str = None,
                  limit: int = 500, offset: int = 0,
                  since: str = None, owner_user_id: int = None,
                  cat_status: str = None) -> List[dict]:
    """since: 冷热分离下界（按开票日期，None = 取全部历史）。
    owner_user_id: 数据隔离（员工只能取自己的发票；财务/管理员传 None）。
    cat_status='pending': 只看系统推测、待财务确认的分类。
    """
    with db.read() as conn:
        if cat_status == "pending" or category:
            if cat_status == "pending":
                ids = _pending_ids(conn, status, keyword, date_from, date_to,
                                   since=since, owner_user_id=owner_user_id)
            else:
                ids = _category_ids(conn, category, status, keyword, date_from, date_to,
                                    since=since, owner_user_id=owner_user_id)
            page_ids = ids[offset:offset + limit]
            rows = (invoice_repo.list(conn, ids=page_ids, limit=len(page_ids))
                    if page_ids else [])
        else:
            rows = invoice_repo.list(conn, status=status, keyword=keyword,
                                     date_from=date_from, date_to=date_to,
                                     limit=limit, offset=offset, since=since,
                                     owner_user_id=owner_user_id)
        _attach_categories(conn, rows)
    # 给每条补上 item_summary_raw（优先用户编辑值，其次解析原始值，最后清洗后值兜底）
    for r in rows:
        raw = _raw_json(r)
        user_val = raw.get("item_summary_user") or ""
        parsed_val = raw.get("item_summary_raw") or ""
        r["item_summary_raw"] = user_val or parsed_val or r.get("item_summary") or ""
        _backfill_dup_fields(r)
    return rows


# 仪表盘统计口径：排除异常/重复发票（aggregate_by_item_summary 同样排除）。
# 分类下钻来自仪表盘，列表必须沿用同一口径，否则会出现"点开 12 张却看到 15 张"。
_STATS_EXCLUDED = ("ABNORMAL", "DUPLICATE")


def _drill_exclude(status: str, category: str) -> Optional[tuple]:
    """分类下钻且未显式指定状态时，排除异常/重复发票；用户显式选状态则以状态为准。"""
    return _STATS_EXCLUDED if (category and not status) else None


def _category_ids(conn, category: str, status: str = None, keyword: str = None,
                  date_from: str = None, date_to: str = None,
                  since: str = None, owner_user_id: int = None) -> List[int]:
    """分类过滤后的发票 id 列表（id 升序）。分类是派生值，只能取摘要列内存过滤。"""
    rows = invoice_repo.expense_rows(
        conn, status=status, keyword=keyword, date_from=date_from, date_to=date_to,
        exclude_status=_drill_exclude(status, category), light=True, since=since,
        owner_user_id=owner_user_id)
    decisions = _resolve_rows(rows, category_repo.memory_all(conn), alias_repo.all(conn),
                              category_dict_repo.names(conn, enabled_only=False))
    return sorted(i for i, d in decisions.items() if d["category"] == category)


def _pending_ids(conn, status: str = None, keyword: str = None,
                 date_from: str = None, date_to: str = None,
                 since: str = None, owner_user_id: int = None) -> List[int]:
    """待确认分类的发票 id 列表（仪表盘黄色提示条下钻用）。"""
    rows = invoice_repo.expense_rows(
        conn, status=status, keyword=keyword, date_from=date_from, date_to=date_to,
        exclude_status=_drill_exclude(status, "pending"), light=True, since=since,
        owner_user_id=owner_user_id)
    decisions = _resolve_rows(rows, category_repo.memory_all(conn), alias_repo.all(conn),
                              category_dict_repo.names(conn, enabled_only=False))
    return sorted(i for i, d in decisions.items() if d["status"] == STATUS_PENDING)


def count_invoices(status: str = None, keyword: str = None, category: str = None,
                   date_from: str = None, date_to: str = None,
                   since: str = None, owner_user_id: int = None,
                   cat_status: str = None) -> int:
    """发票总数（与 list_invoices 同筛选口径，分页用）。"""
    with db.read() as conn:
        if cat_status == "pending":
            return len(_pending_ids(conn, status, keyword, date_from, date_to,
                                    since=since, owner_user_id=owner_user_id))
        if category:
            return len(_category_ids(conn, category, status, keyword, date_from, date_to,
                                     since=since, owner_user_id=owner_user_id))
        return invoice_repo.count(conn, status=status, keyword=keyword,
                                  date_from=date_from, date_to=date_to, since=since,
                                  owner_user_id=owner_user_id)


def page_of_invoice(invoice_id: int, status: str = None, keyword: str = None,
                    page_size: int = 20, category: str = None,
                    date_from: str = None, date_to: str = None,
                    since: str = None, owner_user_id: int = None,
                    cat_status: str = None) -> Optional[int]:
    """focus 定位：发票在筛选结果（id 升序）中所在的页码（1 起），不存在返回 None。"""
    with db.read() as conn:
        if invoice_repo.get(conn, invoice_id) is None:
            return None
        if cat_status == "pending" or category:
            if cat_status == "pending":
                ids = _pending_ids(conn, status, keyword, date_from, date_to,
                                   since=since, owner_user_id=owner_user_id)
            else:
                ids = _category_ids(conn, category, status, keyword, date_from, date_to,
                                    since=since, owner_user_id=owner_user_id)
            pos = ids.index(invoice_id) if invoice_id in ids else None
            return (pos // page_size + 1) if pos is not None else None
        pos = invoice_repo.position_of(conn, invoice_id, status=status, keyword=keyword,
                                       date_from=date_from, date_to=date_to,
                                       since=since, owner_user_id=owner_user_id)
    return pos // page_size + 1


def invoice_status_counts(owner_user_id: int = None) -> Dict[str, int]:
    """各状态发票数量；owner_user_id 非空时只统计该员工自己的发票。"""
    with db.read() as conn:
        return invoice_repo.count_by_status(conn, owner_user_id=owner_user_id)


def expense_breakdown(date_from: str = None, date_to: str = None, top_n: int = 5,
                      owner_user_id: int = None) -> dict:
    """费用构成统计：报销单（业务实质）> 模板 > 发票（税务凭证）。

    分类优先级（见 domain/categorize.resolve_category）：
    1. 人工修正 → 已确认
    2. 报销单填写的费用类型（非空）→ 已确认（明细明显不符时附带轻量提示）
    3. 纠正记忆（提交人 / 发票明细）→ 已确认
    4. 发票明细智能推测 → 待确认（有报销单）/ 未报销推测（无报销单）

    待确认部分金额照常计入图表（防漏算），但逐分类与整体都标记 pending_*。

    返回结构：
    {
        "total_amount", "total_count", "label_when_all",
        "pending_amount", "pending_count", "has_pending",
        "items": [{"name","amount","count","percent",
                   "pending_amount","pending_count","has_pending"}]
    }
    """
    with db.read() as conn:
        rows = invoice_repo.expense_rows(conn, date_from=date_from, date_to=date_to,
                                        exclude_status=_STATS_EXCLUDED,
                                        owner_user_id=owner_user_id)
        memory = category_repo.memory_all(conn)
        alias_map = alias_repo.all(conn)
        dict_names = category_dict_repo.names(conn, enabled_only=False)
    decisions = _resolve_rows(rows, memory, alias_map, dict_names)

    merged: Dict[str, dict] = {}
    total_amount = 0.0
    pending_amount = 0.0
    pending_count = 0
    conflict_amount = 0.0
    conflict_count = 0
    for r in rows:
        # 统计口径（模块五 + 差异兜底）：
        # 1. 发票自身已确认分类（人工/纠正记忆）优先——一对多差异场景按发票明细归类；
        # 2. 否则已匹配且报销单有分类 -> 用报销单分类（日常经强制联动两边一致）；
        # 3. 否则用发票推测分类（待确认照常计入）。
        d = decisions[r["id"]]
        inv_confirmed = (d["status"] == STATUS_CONFIRMED
                         and d.get("source") in ("manual", "memory"))
        reb_cat = ""
        reb_status = STATUS_CONFIRMED
        if not inv_confirmed and r.get("reimburse_id") is not None:
            raw_reb = ((r.get("reimburse_manual_category") or "").strip()
                       or (r.get("expense_type") or "").strip())
            if raw_reb:
                known = known_category(raw_reb, dict_names)
                reb_cat = known or raw_reb
                reb_status = STATUS_CONFIRMED if known else STATUS_LEGACY
        if reb_cat:
            d = {"category": reb_cat, "status": reb_status}
        amount = float(r.get("total_amount") or 0)
        m = merged.setdefault(d["category"], {
            "name": d["category"], "amount": 0.0, "count": 0,
            "pending_amount": 0.0, "pending_count": 0,
            "conflict_amount": 0.0, "conflict_count": 0})
        m["amount"] += amount
        m["count"] += 1
        total_amount += amount
        if d["status"] == STATUS_PENDING:
            m["pending_amount"] += amount
            m["pending_count"] += 1
            pending_amount += amount
            pending_count += 1
        if ((r.get("conflict_invoice_category") or "") != ""
                and (r.get("conflict_resolution") or "") == ""):
            m["conflict_amount"] += amount
            m["conflict_count"] += 1
            conflict_amount += amount
            conflict_count += 1

    rows_out = sorted(merged.values(), key=lambda x: x["amount"], reverse=True)
    total_count = sum(r["count"] for r in rows_out)
    items = [{
        "name": r["name"],
        "amount": round(r["amount"], 2),
        "count": r["count"],
        "percent": round(r["amount"] / total_amount * 100, 2) if total_amount > 0 else 0,
        "pending_amount": round(r["pending_amount"], 2),
        "pending_count": r["pending_count"],
        "has_pending": r["pending_count"] > 0,
        "conflict_amount": round(r["conflict_amount"], 2),
        "conflict_count": r["conflict_count"],
        "has_conflict": r["conflict_count"] > 0,
    } for r in rows_out]

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
            elif (date_from == today.year.__str__() + "-01-01"
                  and date_to == today.isoformat()):
                label_when_all = "今年总支出"

    return {
        "total_amount": round(total_amount, 2),
        "total_count": total_count,
        "pending_amount": round(pending_amount, 2),
        "pending_count": pending_count,
        "has_pending": pending_count > 0,
        "conflict_amount": round(conflict_amount, 2),
        "conflict_count": conflict_count,
        "has_conflict": conflict_count > 0,
        "label_when_all": label_when_all,
        "items": items,
    }


def _apply_manual_category(conn, invoice_id: int, category: str,
                           learn_submitter: bool = True,
                           learn: bool = True,
                           expected_version: Optional[int] = None) -> dict:
    """在调用方事务内写入人工分类并学习纠正记忆。

    学习规则（喂给系统经验）：
    - 该提交人：记录「原填写的费用类型 -> 修正后分类」（原值可为空）
    - 该发票明细：记录「原分类 -> 修正后分类」，下次同类明细优先使用

    learn=False（批量修改 / 一键同步）：强制不学习，防止管理动作污染 AI 记忆。
    expected_version: 乐观锁版本；不匹配时抛 CategoryLockError（整笔回滚）。
    """
    row = invoice_repo.get(conn, invoice_id)
    if row is None or row.get("deleted_at"):
        return {"ok": False, "message": "发票不存在或已删除", "learned": []}
    exp_rows = invoice_repo.expense_rows(conn, ids=[invoice_id])
    memory = category_repo.memory_all(conn)
    before = _resolve_rows(exp_rows, memory, alias_repo.all(conn),
                           category_dict_repo.names(conn, enabled_only=False)).get(
        invoice_id) or {}
    item_row = exp_rows[0] if exp_rows else {}

    new_version = category_repo.upsert(conn, invoice_id, category,
                                       expected_version=expected_version)

    learned = []
    if learn and before.get("category") != category:
        sub = (item_row.get("submitter") or "").strip()
        exp = (item_row.get("expense_type") or "").strip()
        if learn_submitter and sub:
            category_repo.memory_upsert(conn, "submitter", sub, exp, category)
            learned.append("提交人")
        item = (row.get("item_summary") or "").strip()
        if item:
            category_repo.memory_upsert(conn, "item", item,
                                        before.get("category") or "", category)
            learned.append("发票明细")
    return {"ok": True, "learned": learned, "before": before.get("category") or "",
            "version": new_version}


def _invoice_label(row: dict) -> str:
    no = (row.get("invoice_no") or "").strip()
    return ("#%s %s" % (row.get("id"), no)).strip()


def _category_context(conn, invoice_id: int) -> dict:
    """读取有效发票及其生效主从关系；软删除记录一律视为不存在。"""
    row = invoice_repo.get(conn, invoice_id)
    if row is None or row.get("deleted_at"):
        return {"ok": False, "message": "发票不存在或已删除"}
    exp_rows = invoice_repo.expense_rows(conn, ids=[invoice_id])
    reb_id = exp_rows[0].get("reimburse_id") if exp_rows else None
    linked_ids = (match_repo.matched_invoice_ids(conn, [reb_id]).get(reb_id) or []) \
        if reb_id is not None else []
    return {"ok": True, "row": row, "exp_rows": exp_rows, "reimburse_id": reb_id,
            "linked_ids": linked_ids,
            "one_to_one": reb_id is not None and len(linked_ids) <= 1}


def preflight_invoice_category_conn(conn, invoice_id: int, category: str, *,
                                    sync_reimburse: Optional[bool] = None,
                                    version: Optional[int] = None,
                                    reimburse_version: Optional[int] = None) -> dict:
    """所有发票分类入口共用的写入前策略：有效性、乐观锁及 1:1 明确选择。"""
    category = (category or "").strip()
    if category not in category_dict_repo.names(conn, enabled_only=False):
        return {"ok": False, "message": "无效的费用分类，请从分类字典中选择"}
    ctx = _category_context(conn, invoice_id)
    if not ctx.get("ok"):
        return ctx
    cur = category_repo.get(conn, invoice_id)
    cur_ver = int(cur["version"]) if cur else 0
    if version is not None and int(version) != cur_ver:
        return {"ok": False, "stale": True, "stale_ids": [invoice_id],
                "message": "数据已被他人修改，请刷新后重试"}
    reb_id = ctx["reimburse_id"]
    rc = reimburse_category_repo.get(conn, reb_id) or {} if reb_id is not None else {}
    if ctx["one_to_one"] and sync_reimburse is None:
        return {"ok": False, "need_confirm": True, "confirm": "one_to_one",
                "message": "当前为 1 对 1 匹配，修改发票分类是否同步更新报销单总账？",
                "context": ctx, "version": cur_ver,
                "reimburse_category": rc.get("category") or "",
                "reimburse_category_source": rc.get("source") or "",
                "reimburse_version": int(rc.get("version") or 0),
                "overwrite_manual": (rc.get("source") or "") == "manual"}
    if ctx["one_to_one"] and sync_reimburse and reimburse_version is not None:
        if int(reimburse_version) != int(rc.get("version") or 0):
            return {"ok": False, "stale": True, "stale_ids": [reb_id],
                    "message": "数据已被他人修改，请刷新后重试"}
    return {"ok": True, "context": ctx, "version": cur_ver,
            "reimburse_version": int(rc.get("version") or 0)}


def apply_invoice_category_conn(conn, invoice_id: int, category: str, *,
                                learn: bool = True, learn_submitter: bool = True,
                                sync_reimburse: bool = True,
                                expected_version: Optional[int] = None,
                                expected_reimburse_version: Optional[int] = None) -> dict:
    """同事务应用发票人工分类与主从联动，不直接生成或删除冲突。"""
    category = (category or "").strip()
    ctx = _category_context(conn, invoice_id)
    if not ctx.get("ok"):
        return ctx
    if category not in category_dict_repo.names(conn, enabled_only=False):
        return {"ok": False, "message": "无效的费用分类，请从分类字典中选择"}
    row = ctx["row"]
    reb_id = ctx["reimburse_id"]
    one_to_one = ctx["one_to_one"]
    res = _apply_manual_category(conn, invoice_id, category,
                                 learn_submitter=learn_submitter, learn=learn,
                                 expected_version=expected_version)
    if not res.get("ok"):
        return res
    linked_sync = False
    reimburse_version = None
    if reb_id is not None and one_to_one and sync_reimburse:
        reb_res = apply_reimburse_category_conn(
            conn, reb_id, category, expected_version=expected_reimburse_version,
            sync_invoices=False, source="linked", audit=False)
        if not reb_res.get("ok"):
            return reb_res
        linked_sync = True
        reimburse_version = reb_res.get("version")
    return {"ok": True, "learned": res.get("learned") or [],
            "before": res.get("before") or "", "version": res.get("version"),
            "reimburse_version": reimburse_version, "matched": reb_id is not None,
            "reimburse_id": reb_id, "one_to_one": one_to_one,
            "linked_sync": linked_sync, "mismatch": False}


def set_invoice_category(invoice_id: int, category: str,
                         operator: str = "admin",
                         sync_reimburse: Optional[bool] = None,
                         version: Optional[int] = None,
                         reimburse_version: Optional[int] = None) -> dict:
    """确认/修正一张发票的费用分类（人工决策最高优先），并按「主从法则」联动：

    - 未匹配：只改发票自身（孤岛）；
    - 一对一匹配：先返回 need_confirm，由前端询问「是否同步更新报销单总账」；
    - 一对多匹配：只改当前这张发票，不动报销单，差异自动打标。
    报销单已被人工确认时，need_confirm 载荷会带上 overwrite_manual 警告标记。
    """
    from app.repository.repos import CategoryLockError
    from app.service import category_service

    category = (category or "").strip()
    try:
        with db.tx() as conn:
            pre = preflight_invoice_category_conn(
                conn, invoice_id, category, sync_reimburse=sync_reimburse,
                version=version, reimburse_version=reimburse_version)
            if not pre.get("ok"):
                pre.pop("context", None)
                return pre
            ctx = pre["context"]
            row = ctx["row"]
            reb_id = ctx["reimburse_id"]
            linked_ids = ctx["linked_ids"]
            before = _resolve_rows(ctx["exp_rows"], category_repo.memory_all(conn),
                                   alias_repo.all(conn),
                                   category_dict_repo.names(conn, enabled_only=False)).get(
                invoice_id) or {}
            res = apply_invoice_category_conn(
                conn, invoice_id, category, learn=True,
                learn_submitter=not (reb_id is not None and len(linked_ids) > 1),
                sync_reimburse=bool(sync_reimburse), expected_version=version,
                expected_reimburse_version=reimburse_version)
            if not res.get("ok"):
                raise RuntimeError(res.get("message") or "分类修改失败")
            linked_sync = res["linked_sync"]
            if linked_sync:
                audit_msg = "一对一，连带同步报销单分类（来源 linked）"
            elif reb_id is not None:
                audit_msg = "仅改当前发票，提交后统一重算分类差异"
            else:
                audit_msg = "未匹配发票，仅改自身"
            audit_service.log(
                conn, "invoice_category", "invoice", invoice_id, _invoice_label(row),
                before.get("category") or "", category, operator,
                ("已匹配发票（报销单 #%s）：%s" % (reb_id, audit_msg))
                if reb_id else audit_msg)
    except CategoryLockError as e:
        return {"ok": False, "stale": True, "message": e.message}

    checked = category_service.recheck_after_commit([reb_id] if reb_id is not None else [])
    mismatch = reb_id is not None and checked.get(reb_id) is not None
    msg = "已确认分类为「%s」" % category
    if res.get("learned"):
        msg += "，系统已记住该修正（%s），下次自动套用" % "、".join(res["learned"])
    if linked_sync:
        msg += "；一对一匹配，已连带同步报销单分类"
    elif reb_id is not None:
        msg += "；仅修改当前发票" + ("，与报销单分类不一致，已标记分类差异" if mismatch else "")
    return {"ok": True, "message": msg, "category": category,
            "status": STATUS_CONFIRMED, "learned": res.get("learned") or [],
            "matched": reb_id is not None, "reimburse_id": reb_id,
            "linked_sync": linked_sync, "mismatch": mismatch,
            "version": res.get("version")}


def set_invoices_category(invoice_ids: List[int], category: str,
                          operator: str = "admin",
                          versions: Dict[int, int] = None,
                          sync_reimburse: Optional[bool] = None,
                          reimburse_versions: Dict[int, int] = None) -> dict:
    """批量分类共用单条预检策略；任何确认或版本问题都在写入前返回。"""
    from app.repository.repos import CategoryLockError
    from app.service import category_service

    ids = list(dict.fromkeys(int(i) for i in (invoice_ids or [])))
    versions = {int(k): int(v) for k, v in (versions or {}).items()}
    reimburse_versions = {int(k): int(v) for k, v in (reimburse_versions or {}).items()}
    category = (category or "").strip()
    if not ids:
        return {"ok": False, "message": "未选择任何发票"}
    try:
        with db.tx() as conn:
            contexts = {}
            confirmations = []
            for iid in ids:
                pre = preflight_invoice_category_conn(
                    conn, iid, category, sync_reimburse=sync_reimburse,
                    version=versions.get(iid),
                    reimburse_version=None)
                if pre.get("need_confirm"):
                    confirmations.append(pre)
                    continue
                if not pre.get("ok"):
                    pre.pop("context", None)
                    return pre
                contexts[iid] = pre["context"]
            if confirmations:
                reb_versions = {}
                overwrite = False
                for pre in confirmations:
                    rid = pre["context"]["reimburse_id"]
                    reb_versions[str(rid)] = pre["reimburse_version"]
                    overwrite = overwrite or bool(pre.get("overwrite_manual"))
                return {"ok": False, "need_confirm": True, "confirm": "one_to_one",
                        "message": "所选发票包含 1 对 1 匹配，是否同步更新对应报销单总账？",
                        "reimburse_versions": reb_versions,
                        "overwrite_manual": overwrite}
            # 所有发票版本和有效性与 1:1 决策先通过，再统一校验将被覆盖的总账版本。
            for iid, ctx in contexts.items():
                rid = ctx["reimburse_id"]
                if ctx["one_to_one"] and sync_reimburse and rid in reimburse_versions:
                    rc = reimburse_category_repo.get(conn, rid) or {}
                    if int(reimburse_versions[rid]) != int(rc.get("version") or 0):
                        return {"ok": False, "stale": True, "stale_ids": [rid],
                                "message": "数据已被他人修改，请刷新后重试"}

            unmatched_done = []
            linked_rebs = []
            detail_rebs = []
            affected_reb_ids = set()
            for iid, ctx in contexts.items():
                rid = ctx["reimburse_id"]
                if rid is not None:
                    affected_reb_ids.add(rid)
                res = apply_invoice_category_conn(
                    conn, iid, category, learn=False, learn_submitter=False,
                    sync_reimburse=bool(sync_reimburse),
                    expected_version=versions.get(iid),
                    expected_reimburse_version=reimburse_versions.get(rid))
                if not res.get("ok"):
                    raise RuntimeError(res.get("message") or "批量修改失败")
                if rid is None:
                    unmatched_done.append(iid)
                elif res.get("linked_sync"):
                    if rid not in linked_rebs:
                        linked_rebs.append(rid)
                elif rid not in detail_rebs:
                    detail_rebs.append(rid)
            audit_service.log(
                conn, "invoice_category_batch", "invoice", ",".join(map(str, ids)),
                "批量修改 %d 张发票" % len(ids), "", category, operator,
                "未匹配仅改自身 %d 张；一对一同步报销单 %s；仅改明细并统一重算 %s"
                % (len(unmatched_done), linked_rebs or "无", detail_rebs or "无"))
    except CategoryLockError as e:
        return {"ok": False, "stale": True, "message": e.message}

    checked = category_service.recheck_after_commit(sorted(affected_reb_ids))
    mismatch_rebs = sorted(rid for rid, cf in checked.items() if cf is not None)
    msg = "已批量修改 %d 张发票的费用分类为「%s」" % (len(ids), category)
    parts = []
    if unmatched_done:
        parts.append("未匹配 %d 张仅改自身" % len(unmatched_done))
    if linked_rebs:
        parts.append("连带同步 %d 张报销单" % len(linked_rebs))
    if mismatch_rebs:
        parts.append("%d 张报销单保留分类差异" % len(mismatch_rebs))
    if parts:
        msg += "：" + "；".join(parts)
    return {"ok": True, "message": msg, "category": category,
            "updated": len(ids), "unmatched": len(unmatched_done),
            "linked_reimburses": linked_rebs, "mismatch_reimburses": mismatch_rebs,
            "matched": len(ids) - len(unmatched_done)}


# ---------------------------------------------------------------- 相似记录三层判定
# 规则二：销方核心字号提取时剔除的公司类后缀
_SELLER_STRIP = ("股份有限公司", "有限责任公司", "有限公司", "分公司",
                 "集团公司", "集团", "公司", "旗舰店", "专营店", "专卖店")
# 规则三：摘要核心词提取时的停用词（整词或尾部无意义词）
_SUMMARY_STOP = ("服务", "货物", "劳务", "费用", "详见销货清单",
                 "电子发票", "普通发票", "专用发票", "发票", "其他")


def _core_seller_terms(seller_name: str) -> List[str]:
    """销方名称 -> 核心字号查询词（剔除公司类后缀；再生成去地域前缀变体）。

    例：「北京星巴克咖啡有限公司」-> ["北京星巴克咖啡", "星巴克咖啡"]
    （去掉前 2 字地域前缀兜底，覆盖「上海星巴克咖啡」这类同品牌异地主体）
    """
    s = "".join((seller_name or "").split())
    if not s:
        return []
    for suf in _SELLER_STRIP:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[:-len(suf)]
            break
    terms = [s] if len(s) >= 2 else []
    # 去地域前缀变体（如「星巴克咖啡」），要求 >=4 字避免泛词误伤（如「销售方」）
    if len(s) - 2 >= 4:
        terms.append(s[2:])     # 去 2 字地域前缀
    if len(s) - 3 >= 4:
        terms.append(s[3:])     # 去 3 字地域前缀（如「北京市」）
    seen, out = set(), []
    for t in terms:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _core_summary_words(item_summary: str) -> List[str]:
    """发票摘要 -> 核心词组（按 * 分割，去尾部停用词，>=2 字）。

    例：「*餐饮服务*星巴克咖啡」-> ["餐饮", "星巴克咖啡"]
    """
    words = []
    for part in (item_summary or "").replace("　", "*").split("*"):
        p = part.strip()
        if not p:
            continue
        if p in _SUMMARY_STOP:
            continue
        stem = p
        changed = True
        while changed and len(stem) > 2:
            changed = False
            for suf in _SUMMARY_STOP:
                if stem.endswith(suf) and len(stem) - len(suf) >= 2:
                    stem = stem[:-len(suf)]
                    changed = True
        for w in (stem, p):
            if len(w) >= 2 and w not in words:
                words.append(w)
    return words


def similar_unmatched(invoice_id: int) -> dict:
    """三层相似记录查询（统一批量确认面板用）。

    规则一（最高优先）：归类映射大类——摘要命中同一财务大类（如餐饮类）；
    规则二：销方核心字号——销方名称含同一核心字号（如「星巴克」）；
    规则三（兜底）：摘要核心词模糊匹配。
    返回每条记录的命中原因 reason 与命中词 hits（前端高亮用），
    以及整体判定依据 basis（顶部人性化提示用）。
    无论当前发票是否已匹配，都返回全部相似记录（含匹配状态）。
    """
    with db.read() as conn:
        target = invoice_repo.get(conn, invoice_id)
        if target is None:
            return {"ok": False, "message": "发票不存在或已删除", "items": []}
        # 相似池含全部有效发票（已匹配的也在内，由财务勾选后按主从法则处理），
        # 只排除异常/重复等统计排除状态
        rows = invoice_repo.expense_rows(conn, exclude_status=_STATS_EXCLUDED,
                                        light=True)
        others = [r for r in rows if r["id"] != invoice_id]

        t_summary = target.get("item_summary") or ""
        t_seller = target.get("seller_name") or ""

        # 规则一：归类大类（「其他费用」是兜底桶，不构成有效相似依据）
        big = categorize_item(t_summary)
        rule1 = big if big != "其他费用" else ""
        # 规则二：销方核心字号
        seller_terms = _core_seller_terms(t_seller)
        # 规则三：摘要核心词
        kw_terms = _core_summary_words(t_summary)

        found: Dict[int, dict] = {}
        basis: List[str] = []
        used1 = used2 = used3 = False
        for r in others:
            hits, reason = [], ""
            # 规则一
            if rule1 and categorize_item(r.get("item_summary") or "") == rule1:
                reason = f"同属「{rule1}」大类"
                used1 = True
            # 规则二
            cand_seller = "".join((r.get("seller_name") or "").split())
            for t in seller_terms:
                if t and t in cand_seller:
                    hits.append(t)
                    if not reason:
                        reason = f"同一商家「{t}」"
                        used2 = True
                    break
            # 规则三
            cand_sum = r.get("item_summary") or ""
            for w in kw_terms:
                if w in cand_sum:
                    hits.append(w)
                    if not reason:
                        reason = f"关键词「{w}」"
                        used3 = True
            if reason:
                found[r["id"]] = {"row": r, "reason": reason, "hits": hits}

        if used1:
            basis.append(f"「{rule1}」归类大类")
        if used2:
            basis.append("商家名称")
        if used3:
            basis.append("摘要关键词")

        cand = list(found.values())[:50]
        ids = [v["row"]["id"] for v in cand]
        details = {r["id"]: r for r in invoice_repo.list(conn, ids=ids, limit=len(ids))} if ids else {}
        decisions = resolve_categories(conn, ids)
        manual_versions = {iid: int(x.get("version") or 0)
                           for iid, x in category_repo.decisions(conn, ids).items()}
        reb_nos = {r["id"]: r.get("reimburse_no") or ""
                   for r in reimburse_repo.list(conn, limit=100000)}
    items = []
    for v in cand:
        r = v["row"]
        iid = r["id"]
        d = details.get(iid) or {}
        dec = decisions.get(iid) or {}
        reb_id = r.get("reimburse_id")
        items.append({
            "id": iid,
            "invoice_no": d.get("invoice_no") or "",
            "invoice_date": d.get("invoice_date") or "",
            "total_amount": d.get("total_amount") or "",
            "category_version": manual_versions.get(iid, 0),
            "item_summary": d.get("item_summary") or "",
            "seller_name": d.get("seller_name") or "",
            "category": dec.get("category") or "",
            "matched": reb_id is not None,
            "reimburse_no": reb_nos.get(reb_id) or "" if reb_id else "",
            "reason": v["reason"],
            "hits": v["hits"],
        })
    return {"ok": True, "items": items, "basis": basis, "target": {
        "id": invoice_id,
        "item_summary": t_summary,
        "invoice_no": target.get("invoice_no") or "",
        "seller_name": t_seller,
    }}


def apply_reimburse_category_conn(conn, reimburse_id: int, category: str,
                                  operator: str = "admin",
                                  expected_version: Optional[int] = None,
                                  sync_invoices: bool = True,
                                  source: str = "manual",
                                  audit: bool = True) -> dict:
    """报销单分类唯一写入服务，可按业务场景决定是否同步全部明细。"""
    from app.service import category_service

    category = (category or "").strip()
    if category not in category_dict_repo.names(conn, enabled_only=False):
        return {"ok": False, "message": "无效的费用分类，请从分类字典中选择"}
    rb = reimburse_repo.get(conn, reimburse_id)
    if rb is None or rb.get("deleted_at"):
        return {"ok": False, "message": "报销单不存在或已删除"}
    cur = reimburse_category_repo.get(conn, reimburse_id)
    cur_ver = int(cur["version"]) if cur else 0
    if expected_version is not None and int(expected_version) != cur_ver:
        return {"ok": False, "stale": True,
                "message": "数据已被他人修改，请刷新后重试"}
    ids = match_repo.matched_invoice_ids(conn, [reimburse_id]).get(reimburse_id) or []
    old_cat, _ = category_service.reimburse_explicit(conn, rb)
    new_version = reimburse_category_repo.upsert(
        conn, reimburse_id, category, source, expected_version=expected_version)
    if sync_invoices:
        for iid in ids:
            res = apply_invoice_category_conn(
                conn, iid, category, learn=False, learn_submitter=False,
                sync_reimburse=False)
            if not res.get("ok"):
                raise RuntimeError(res.get("message") or "同步发票分类失败")
    if audit:
        detail = ("已匹配：强制联动同步 %d 张关联发票 %s（不学习记忆）" % (len(ids), ids)
                  if sync_invoices and ids else
                  ("未匹配状态：仅改报销单，独立于发票" if not ids else "仅更新报销单分类"))
        audit_service.log(conn, "reimburse_category", "reimburse", reimburse_id,
                          rb.get("reimburse_no") or "", old_cat, category, operator, detail)
    matched = bool(ids)
    message = ("已将报销单及 %d 张关联发票的分类统一为「%s」" % (len(ids), category)
               if sync_invoices and ids else
               "已更新报销单分类为「%s」（未同步其他发票）" % category)
    return {"ok": True, "message": message, "category": category,
            "status": STATUS_CONFIRMED, "learned": [], "matched": matched,
            "mode": "sync" if sync_invoices and ids else "self",
            "invoice_count": len(ids) if sync_invoices else 0,
            "version": new_version}


def set_reimburse_category(reimburse_id: int, category: str,
                           operator: str = "admin", mode: str = "sync",
                           version: Optional[int] = None) -> dict:
    """修改报销单分类（主从法则）：

    - 未匹配：只写报销单侧分类，绝不触碰任何发票（孤岛）；
    - 已匹配：无条件强制联动全部关联发票（主账分类变了，明细必须跟）；
    - version 为乐观锁版本，过期返回 stale 提示。
    mode 参数仅为兼容旧接口保留，已匹配时固定按 sync 执行。
    """
    category = (category or "").strip()
    from app.service import category_service
    with db.tx() as conn:
        res = apply_reimburse_category_conn(
            conn, reimburse_id, category, operator, expected_version=version)
    if not res.get("ok"):
        return res
    category_service.recheck_after_commit([reimburse_id])
    return res


def set_reimburses_category(reimburse_ids: List[int], category: str,
                            operator: str = "admin",
                            versions: Dict[int, int] = None) -> dict:
    """批量确认报销单分类（单事务；全部预检通过后逐条强制联动明细）。

    批量动作一律 learn=False；versions 提供时逐条校验乐观锁。
    """
    ids = list(dict.fromkeys(int(i) for i in (reimburse_ids or [])))
    category = (category or "").strip()
    versions = {int(k): int(v) for k, v in (versions or {}).items()}
    if not ids:
        return {"ok": False, "message": "未选择任何报销单"}
    from app.service import category_service
    synced = 0
    with db.tx() as conn:
        if category not in category_dict_repo.names(conn, enabled_only=False):
            return {"ok": False, "message": "无效的费用分类，请从分类字典中选择"}
        stale_ids = []
        for rid in ids:
            rb = reimburse_repo.get(conn, rid)
            if rb is None or rb.get("deleted_at"):
                return {"ok": False, "message": "报销单 #%s 不存在或已删除" % rid}
            cur = reimburse_category_repo.get(conn, rid)
            cur_ver = int(cur["version"]) if cur else 0
            if rid in versions and versions[rid] != cur_ver:
                stale_ids.append(rid)
        if stale_ids:
            return {"ok": False, "stale": True, "stale_ids": stale_ids,
                    "message": "数据已被他人修改，请刷新后重试"}
        for rid in ids:
            res = apply_reimburse_category_conn(
                conn, rid, category, operator, expected_version=versions.get(rid))
            if not res.get("ok"):
                raise RuntimeError(res.get("message") or "批量修改失败")
            synced += int(res.get("invoice_count") or 0)
        audit_service.log(
            conn, "reimburse_category_batch", "reimburse", ",".join(map(str, ids)),
            "批量确认 %d 张报销单" % len(ids), "", category, operator,
            "批量确认报销单分类，同步发票 %d 张（不学习记忆）" % synced)
    category_service.recheck_after_commit(ids)
    return {"ok": True, "updated": len(ids), "invoice_synced": synced,
            "message": "已将 %d 张报销单的分类统一为「%s」，同步 %d 张关联发票"
                       % (len(ids), category, synced)}


def export_excel(status: str = None, keyword: str = None, ids: list = None,
                 owner_user_id: int = None) -> bytes:
    """导出发票清单 Excel（支持按当前筛选条件或指定 ids 导出，上限 10 万条）。"""
    import io
    from openpyxl import Workbook
    from app.infra.excel import safe_cell
    # 与全量重算串行：避免导出到重算中途的半成品数据
    from app.service.task_lock import HEAVY_TASK_LOCK
    with HEAVY_TASK_LOCK, db.read() as conn:
        rows = invoice_repo.list(conn, status=status, keyword=keyword, limit=100000,
                                 ids=ids, owner_user_id=owner_user_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "发票清单"
    ws.append(["ID", "状态", "发票号码", "开票日期",
               "购买方名称", "购买方税号", "销售方名称", "销售方税号",
               "金额", "税额", "价税合计", "税率", "报销内容",
               "导入时间"])
    for i in rows:
        ws.append([safe_cell(v) for v in [
            i["id"], i["status"], i["invoice_no"],
            i["invoice_date"], i["buyer_name"], i["buyer_tax_no"],
            i["seller_name"], i["seller_tax_no"],
            str(i["amount"]) if i["amount"] is not None else "",
            str(i["tax_amount"]) if i["tax_amount"] is not None else "",
            str(i["total_amount"]) if i["total_amount"] is not None else "",
            str(i["tax_rate"]) if i.get("tax_rate") is not None else "",
            i["item_summary"] or "",
            i.get("created_at") or "",
        ]])
    for col, w in zip("ABCDEFGHIJKLMN",
                      (6, 10, 22, 12, 28, 20, 28, 20, 12, 10, 12, 8, 30, 20)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_excel_stream(status: str = None, keyword: str = None, ids: list = None,
                        owner_user_id: int = None):
    """流式导出发票清单：write_only 写临时文件后分块下发，5 万条不占大内存。

    返回字节块迭代器；生成结束或客户端中断都会清理临时文件。
    """
    import os
    import tempfile
    from openpyxl import Workbook
    from app.infra.excel import safe_cell
    from app.service.task_lock import HEAVY_TASK_LOCK

    def gen():
        tmp_path = None
        try:
            with HEAVY_TASK_LOCK, db.read() as conn:
                rows = invoice_repo.list(conn, status=status, keyword=keyword,
                                         limit=100000, ids=ids,
                                         owner_user_id=owner_user_id)
            wb = Workbook(write_only=True)
            ws = wb.create_sheet("发票清单")
            ws.append(["ID", "状态", "发票号码", "开票日期",
                       "购买方名称", "购买方税号", "销售方名称", "销售方税号",
                       "金额", "税额", "价税合计", "税率", "报销内容",
                       "导入时间"])
            for i in rows:
                ws.append([safe_cell(v) for v in [
                    i["id"], i["status"], i["invoice_no"],
                    i["invoice_date"], i["buyer_name"], i["buyer_tax_no"],
                    i["seller_name"], i["seller_tax_no"],
                    str(i["amount"]) if i["amount"] is not None else "",
                    str(i["tax_amount"]) if i["tax_amount"] is not None else "",
                    str(i["total_amount"]) if i["total_amount"] is not None else "",
                    str(i["tax_rate"]) if i.get("tax_rate") is not None else "",
                    i["item_summary"] or "",
                    i.get("created_at") or "",
                ]])
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=".xlsx", dir=str(settings.data_dir))
            tmp_path = tmp.name
            tmp.close()
            wb.save(tmp_path)
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return gen()
