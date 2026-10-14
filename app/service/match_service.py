"""匹配用例（应用服务层）：执行匹配 / 人工确认 / 拒绝 / 手动匹配 / 解除匹配。

流程（核心闭环 §1.2）：去重后 NORMAL 发票 x 未占用报销单 -> 评分 -> 状态机。
所有写操作在单事务内（I9）；发票状态变更经 status_flow（I7）。
"""
import json
from typing import Dict, List, Optional

from app.config import settings
from app.domain.dto import InvoiceDTO, ReimburseDTO
from app.domain.matcher import MatchEngine, PairScore
from app.domain.normalize import to_decimal
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import (company_repo, invoice_repo, match_repo,
                                  reimburse_repo)
from app.service.status_flow import change_invoice_status

logger = get_logger(__name__)
_engine = MatchEngine()

# 匹配类型常量（与 match_result.CHECK 约束一致）
T_EXACT, T_COMPOSITE, T_FUZZY = "exact", "composite", "fuzzy"
T_MANY_TO_ONE, T_MANUAL = "many_to_one", "manual"


def _inv_dto(row: dict) -> InvoiceDTO:
    return InvoiceDTO(
        invoice_code=row.get("invoice_code") or "",
        invoice_no=row.get("invoice_no") or "",
        invoice_date=row.get("invoice_date"),
        buyer_tax_no=row.get("buyer_tax_no") or "",
        total_amount=to_decimal(row.get("total_amount")),
    )


def _reb_dto(row: dict) -> ReimburseDTO:
    return ReimburseDTO(
        reimburse_no=row.get("reimburse_no") or "",
        reimburse_date=row.get("reimburse_date"),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        amount=to_decimal(row.get("amount")),
        invoice_no=row.get("invoice_no") or "",
    )


# ---------------------------------------------------------------- 执行匹配
def run_matching(operator: str = None) -> dict:
    """执行一轮匹配（全量重评估）。

    先清理上一轮非生效评估记录（生效匹配保留），再对
    NORMAL 且无生效匹配的发票 与 未占用的报销单 重新评分。
    """
    operator = operator or settings.default_operator
    summary = {"cleared": 0, "auto_matched": 0, "suspicious": 0, "conflict": 0,
               "unmatched": 0, "many_to_one": 0, "evaluated_reimburses": 0}

    with db.tx() as conn:
        summary["cleared"] = match_repo.delete_non_active(conn)
        company_tax_nos = company_repo.tax_nos(conn)

        # 候选集：NORMAL 且无生效匹配的发票
        invoices = [r for r in invoice_repo.list(conn, status="NORMAL", limit=100000)
                    if match_repo.active_for_invoice(conn, r["id"]) is None]
        # 未占用报销单
        occupied = match_repo.active_reimburse_ids(conn)
        reimburses = [r for r in reimburse_repo.list(conn, limit=100000)
                      if r["id"] not in occupied]

        inv_rows = {r["id"]: r for r in invoices}
        inv_dtos = {r["id"]: _inv_dto(r) for r in invoices}
        available: set = set(inv_dtos.keys())     # 本轮尚未被占用的发票
        has_candidate: set = set()                # 有过 >=0.7 候选/冲突的发票

        for reb_row in reimburses:
            summary["evaluated_reimburses"] += 1
            reb = _reb_dto(reb_row)

            # ---- 单发票候选：取最高分 ----
            best_id, best_ps = None, None
            for inv_id in list(available):
                ps = _engine.score_pair(inv_dtos[inv_id], reb, company_tax_nos)
                meaningful = ps.score >= settings.suspicious_threshold or (
                    ps.conflict_fields and "amount" in ps.conflict_fields)
                if meaningful and (best_ps is None or ps.score > best_ps.score):
                    best_id, best_ps = inv_id, ps

            if best_id is not None:
                has_candidate.add(best_id)
                status = _engine.decide_status(best_ps)
                mid = _persist_pair(conn, best_id, reb_row["id"], best_ps, status, best_ps.match_type)
                if status == "MATCHED":
                    change_invoice_status(conn, best_id, "MATCHED",
                                          f"自动匹配（score={best_ps.score}, match#{mid}）", operator)
                    available.discard(best_id)
                    summary["auto_matched"] += 1
                elif status == "SUSPICIOUS":
                    summary["suspicious"] += 1
                else:
                    summary["conflict"] += 1
                continue

            # ---- 无单票候选：尝试汇总匹配（多对一）----
            pool = [inv_dtos[i] for i in available]
            combo = _engine.find_many_to_one(pool, reb)
            if combo:
                ids = [i for i in available if inv_dtos[i] in combo]
                for inv_id in ids:
                    ps = PairScore(score=0.9, match_type=T_MANY_TO_ONE,
                                   diff_fields=[f"汇总匹配: {len(combo)} 张发票合计 = 报销单金额"])
                    mid = _persist_pair(conn, inv_id, None, ps, "SUSPICIOUS", T_MANY_TO_ONE)
                    match_repo.insert_pair(conn, mid, inv_id, reb_row["id"])
                    available.discard(inv_id)
                    has_candidate.add(inv_id)
                    summary["many_to_one"] += 1
                    summary["suspicious"] += 1
                continue
        # ---- 无任何候选的发票 -> UNMATCHED ----
        for inv_id in sorted(available):
            if inv_id in has_candidate:
                continue
            _persist_unmatched(conn, inv_id, inv_rows[inv_id])
            summary["unmatched"] += 1

    logger.info("匹配完成: %s (by %s)", summary, operator)
    return summary


def _persist_pair(conn, invoice_id: int, reimburse_id: Optional[int],
                  ps: PairScore, status: str, match_type: str) -> int:
    return match_repo.insert(conn, {
        "invoice_id": invoice_id,
        "reimburse_id": reimburse_id,
        "match_type": match_type,
        "match_score": ps.score,
        "status": status,
        "diff_fields": ps.diff_fields,
        "message": "; ".join(ps.diff_fields) if ps.diff_fields else
                   (f"字段冲突: {ps.conflict_fields}" if ps.conflict_fields else ""),
    })


def _persist_unmatched(conn, invoice_id: int, inv_row: dict) -> None:
    match_repo.insert(conn, {
        "invoice_id": invoice_id,
        "reimburse_id": None,
        "match_type": T_FUZZY,
        "match_score": 0.0,
        "status": "UNMATCHED",
        "diff_fields": [],
        "message": "无候选报销单（未找到满足条件的配对）",
    })


# ---------------------------------------------------------------- 人工确认/拒绝
def confirm(match_id: int, operator: str = None, note: str = "") -> dict:
    """人工确认 SUSPICIOUS/CONFLICT 匹配 -> CONFIRMED，发票 -> MATCHED。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m["status"] not in ("SUSPICIOUS", "CONFLICT"):
            return {"ok": False, "message": f"仅 SUSPICIOUS/CONFLICT 可确认（当前: {m['status']}）"}

        reb_id = m["reimburse_id"]
        if reb_id is None:
            pairs = match_repo.pairs_of(conn, [match_id]).get(match_id, [])
            reb_id = pairs[0]["reimburse_id"] if pairs else None

        # 占用检查：发票与报销单均不能已有生效匹配
        if match_repo.active_for_invoice(conn, m["invoice_id"]):
            return {"ok": False, "message": "该发票已存在生效匹配"}
        if reb_id and match_repo.active_for_reimburse(conn, reb_id):
            return {"ok": False, "message": "该报销单已存在生效匹配"}

        match_repo.update_status(conn, match_id, "CONFIRMED", operator)
        if note:
            _append_message(conn, match_id, f"确认备注: {note}")
        if reb_id:
            change_invoice_status(conn, m["invoice_id"], "MATCHED",
                                  f"人工确认匹配 (match#{match_id})", operator)
        # 同发票其余待确认候选自动拒绝
        for other in match_repo.list(conn, statuses=["SUSPICIOUS", "CONFLICT"], limit=10000):
            if other["invoice_id"] == m["invoice_id"] and other["id"] != match_id:
                match_repo.update_status(conn, other["id"], "REJECTED", operator)
                _append_message(conn, other["id"], "同发票其他候选已确认，本记录自动拒绝")
        return {"ok": True, "message": "已确认匹配"}


def reject(match_id: int, operator: str = None, note: str = "") -> dict:
    """人工拒绝 SUSPICIOUS/CONFLICT 匹配 -> REJECTED。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m["status"] not in ("SUSPICIOUS", "CONFLICT"):
            return {"ok": False, "message": f"仅 SUSPICIOUS/CONFLICT 可拒绝（当前: {m['status']}）"}
        match_repo.update_status(conn, match_id, "REJECTED", operator)
        if note:
            _append_message(conn, match_id, f"拒绝备注: {note}")
        return {"ok": True, "message": "已拒绝匹配"}


def manual_match(invoice_id: int, reimburse_id: int, operator: str = None) -> dict:
    """人工指定匹配（manual 类型，直接生效）。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        inv = invoice_repo.get(conn, invoice_id)
        reb = reimburse_repo.get(conn, reimburse_id)
        if inv is None or reb is None:
            return {"ok": False, "message": "发票或报销单不存在"}
        if inv["status"] != "NORMAL":
            return {"ok": False, "message": f"仅 NORMAL 发票可匹配（当前: {inv['status']}）"}
        if match_repo.active_for_invoice(conn, invoice_id):
            return {"ok": False, "message": "该发票已存在生效匹配"}
        if match_repo.active_for_reimburse(conn, reimburse_id):
            return {"ok": False, "message": "该报销单已存在生效匹配"}

        mid = match_repo.insert(conn, {
            "invoice_id": invoice_id, "reimburse_id": reimburse_id,
            "match_type": T_MANUAL, "match_score": 1.0, "status": "CONFIRMED",
            "diff_fields": [], "message": "人工指定匹配",
            "confirmed_by": operator, "confirmed_at": None,
        })
        change_invoice_status(conn, invoice_id, "MATCHED",
                              f"人工指定匹配 (match#{mid})", operator)
        for other in match_repo.list(conn, statuses=["SUSPICIOUS", "CONFLICT"], limit=10000):
            if other["invoice_id"] == invoice_id:
                match_repo.update_status(conn, other["id"], "REJECTED", operator)
                _append_message(conn, other["id"], "同发票已人工指定匹配，本记录自动拒绝")
        return {"ok": True, "message": "人工匹配成功"}


def unmatch(match_id: int, operator: str = None, reason: str = "") -> dict:
    """解除生效匹配（人工操作，记录原因）。

    注意：设计 §3.2 中 MATCHED 为终态，但错误匹配必须可撤销，
    此处按操作员动作处理：匹配 -> REJECTED，发票 MATCHED -> NORMAL（均留痕）。
    """
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m["status"] not in ("MATCHED", "CONFIRMED"):
            return {"ok": False, "message": f"仅生效匹配可解除（当前: {m['status']}）"}
        match_repo.update_status(conn, match_id, "REJECTED", operator)
        _append_message(conn, match_id, f"解除匹配: {reason or '无备注'}")
        try:
            change_invoice_status(conn, m["invoice_id"], "NORMAL",
                                  f"解除匹配 (match#{match_id}): {reason or '无备注'}", operator)
        except Exception as e:
            logger.warning("解除匹配时发票状态回转失败: %s", e)
        return {"ok": True, "message": "匹配已解除，发票回到 NORMAL"}


def _append_message(conn, match_id: int, text: str) -> None:
    m = match_repo.get(conn, match_id)
    msg = (m["message"] + " | " + text) if m.get("message") else text
    conn.execute("UPDATE match_result SET message = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                 (msg, match_id))


# ---------------------------------------------------------------- 查询
def list_matches(statuses: List[str] = None) -> List[dict]:
    with db.read() as conn:
        rows = match_repo.list(conn, statuses=statuses)
        pairs = match_repo.pairs_of(conn, [r["id"] for r in rows])
        reb_ids = {p["reimburse_id"] for ps in pairs.values() for p in ps}
        reb_names = {}
        if reb_ids:
            for r in reimburse_repo.list(conn, limit=100000):
                if r["id"] in reb_ids:
                    reb_names[r["id"]] = r
        for r in rows:
            r["pair_reimburses"] = [reb_names.get(p["reimburse_id"]) for p in pairs.get(r["id"], [])]
            r["pair_reimburses"] = [x for x in r["pair_reimburses"] if x]
        return rows


def match_counts() -> Dict[str, int]:
    with db.read() as conn:
        return match_repo.count_by_status(conn)


def matchable_options() -> dict:
    """人工匹配下拉数据：可用发票（NORMAL 无生效匹配）与未占用报销单。"""
    with db.read() as conn:
        invoices = [r for r in invoice_repo.list(conn, status="NORMAL", limit=100000)
                    if match_repo.active_for_invoice(conn, r["id"]) is None]
        occupied = match_repo.active_reimburse_ids(conn)
        reimburses = [r for r in reimburse_repo.list(conn, limit=100000)
                      if r["id"] not in occupied]
        return {"invoices": invoices, "reimburses": reimburses}
