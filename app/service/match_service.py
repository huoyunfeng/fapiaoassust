"""匹配用例（应用服务层）：执行匹配 / 人工确认 / 拒绝 / 手动匹配 / 解除匹配。

流程（核心闭环 §1.2）：去重后 NORMAL 发票 x 未占用报销单 -> 评分 -> 状态机。
所有写操作在单事务内（I9）；发票状态变更经 status_flow（I7）。
"""
import difflib
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

# 不匹配项写入报销单备注时的标记：保留用户原始备注，重复执行可安全覆盖
NOTE_MARK = "【不匹配】"


def _inv_dto(row: dict) -> InvoiceDTO:
    """发票 DTO：带销方名称与项目摘要，供「文本/抬头」维度做相似度比对。"""
    return InvoiceDTO(
        invoice_code=row.get("invoice_code") or "",
        invoice_no=row.get("invoice_no") or "",
        invoice_date=row.get("invoice_date"),
        buyer_tax_no=row.get("buyer_tax_no") or "",
        seller_name=row.get("seller_name") or "",
        item_summary=row.get("item_summary") or "",
        total_amount=to_decimal(row.get("total_amount")),
    )


def _reb_dto(row: dict) -> ReimburseDTO:
    """报销单 DTO：备注剥离系统写入的【不匹配】说明，只留用户原文。"""
    return ReimburseDTO(
        reimburse_no=row.get("reimburse_no") or "",
        reimburse_date=row.get("reimburse_date"),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        amount=to_decimal(row.get("amount")),
        invoice_no=row.get("invoice_no") or "",
        remark=_strip_note(row.get("remark")),
    )


def _strip_note(text: Optional[str]) -> str:
    """剥掉上一次写入的不匹配说明，留下用户原始备注。"""
    if not text:
        return ""
    idx = text.find(NOTE_MARK)
    return text[:idx].rstrip() if idx >= 0 else text


def _write_note(conn, reb_row: dict, notes: Optional[List[str]]) -> None:
    """把"双方同类型字段的不一致项"写入报销单备注。

    notes 为空表示无可写内容（字段都一致，或已匹配），此时清空说明。
    """
    base = _strip_note(reb_row.get("remark"))
    detail = f"{NOTE_MARK}{'；'.join(notes)}"
    text = f"{base} {detail}" if notes and base else (detail if notes else base)
    if text != (reb_row.get("remark") or ""):
        reimburse_repo.update_remark(conn, reb_row["id"], text)


def _detect_conflict(conn, reimburse_id: Optional[int]) -> bool:
    """匹配生效后校验双方分类，不一致则落「分类差异」（模块四）。"""
    if reimburse_id is None:
        return False
    from app.service import category_service   # 惰性导入，避免服务层循环依赖
    return category_service.detect_conflict(conn, reimburse_id) is not None


def _ambiguity_note(tied_ids: List[int], inv_rows: Dict[int, dict], reb_row: dict) -> str:
    """并列候选说明 + 销售方/摘要弱相似度提示（仅作展示参考，不影响判定）。

    典型场景：报销单未填发票号，且多张发票金额、日期完全相同（如连号打车票）。
    """
    reb_text = "".join([
        reb_row.get("expense_type") or "",
        reb_row.get("remark") or "",
        reb_row.get("submitter") or "",
    ])
    parts = []
    for iid in sorted(tied_ids):
        r = inv_rows.get(iid) or {}
        inv_text = (r.get("seller_name") or "") + (r.get("item_summary") or "")
        ratio = (difflib.SequenceMatcher(None, reb_text, inv_text).ratio()
                 if reb_text and inv_text else 0.0)
        label = r.get("invoice_no") or f"#{iid}"
        seller = r.get("seller_name") or "销售方未知"
        parts.append(f"{label}（{seller}，文本相似度 {ratio:.0%}）")
    return (f"存在 {len(tied_ids)} 张金额/日期相同的发票，无法自动区分，"
            f"请人工在匹配中心指定（候选：{'、'.join(parts)}）")


# ---------------------------------------------------------------- 执行匹配
def run_matching(operator: str = None) -> dict:
    """执行一轮匹配（全量重评估）。

    先清理上一轮非生效评估记录（生效匹配保留），再对
    NORMAL 且无生效匹配的发票 与 未占用的报销单 重新评分。
    """
    operator = operator or settings.default_operator
    summary = {"cleared": 0, "auto_matched": 0, "pending": 0, "conflict": 0,
               "unmatched": 0, "many_to_one": 0, "evaluated_reimburses": 0,
               "category_conflict": 0}

    with db.tx() as conn:
        summary["cleared"] = match_repo.delete_non_active(conn)
        company_tax_nos = company_repo.tax_nos(conn)

        # 候选集：NORMAL 且无生效匹配的发票（一次查询取已占用 id，避免逐票 N+1 查询）
        occupied_invoices = match_repo.active_invoice_ids(conn)
        invoices = [r for r in invoice_repo.list(conn, status="NORMAL", limit=100000)
                    if r["id"] not in occupied_invoices]
        # 未占用报销单
        occupied = match_repo.active_reimburse_ids(conn)
        reimburses = [r for r in reimburse_repo.list(conn, limit=100000)
                      if r["id"] not in occupied]

        inv_rows = {r["id"]: r for r in invoices}
        inv_dtos = {r["id"]: _inv_dto(r) for r in invoices}
        available: set = set(inv_dtos.keys())     # 本轮尚未被占用的发票
        has_candidate: set = set()                # 已产过候选（含低置信度兜底）的发票
        reb_map = {r["id"]: r for r in reimburses}

        done: set = set()          # 已在前序轮次完成分配的报销单

        # ================================================================
        # 第一轮：高置信度直配
        #   双方都有发票号且一致 + 金额一致 + 未被安全锁封顶 -> MATCHED
        #   注：无号场景必然触发安全锁封顶 0.94，不可能进入本轮
        # ================================================================
        for reb_row in reimburses:
            summary["evaluated_reimburses"] += 1
            reb = _reb_dto(reb_row)
            reb_no = (reb.invoice_no or "").strip()
            if not reb_no:
                continue                      # 无号：安全锁封顶，交第三轮人工确认
            best = None
            for inv_id in sorted(available):
                inv = inv_dtos[inv_id]
                if (inv.invoice_no or "").strip() != reb_no:
                    continue
                if inv.total_amount is None or reb.amount is None:
                    continue
                if abs(inv.total_amount - reb.amount) > settings.amount_tolerance:
                    continue
                ps = _engine.score_pair(inv, reb, company_tax_nos)
                if ps.is_rejected or ps.capped:
                    continue                  # 淘汰或安全锁未通过 -> 留待人工
                best = (inv_id, ps)
                break
            if best is None:
                continue
            inv_id, ps = best
            mid = _persist_pair(conn, inv_id, reb_row["id"], ps, "MATCHED", ps.match_type)
            change_invoice_status(conn, inv_id, "MATCHED",
                                  f"第一轮高置信直配（score={ps.score}, match#{mid}）", operator)
            available.discard(inv_id)
            done.add(reb_row["id"])
            summary["auto_matched"] += 1
            if _detect_conflict(conn, reb_row["id"]):
                summary["category_conflict"] += 1
            _write_note(conn, reb_row, [])

        # ================================================================
        # 第二轮：多对一组合凑数
        #   仅对第一轮未匹配的报销单；2~5 张发票合计 == 报销金额，且发票在期间内
        #   固定得分 0.90 -> SUSPICIOUS（软锁定，交人工确认）
        # ================================================================
        for reb_row in reimburses:
            if reb_row["id"] in done:
                continue
            reb = _reb_dto(reb_row)
            pool = [inv_dtos[i] for i in sorted(available)]
            combo = _engine.find_many_to_one(pool, reb)
            if not combo:
                continue
            # 按对象身份匹配：InvoiceDTO 是值相等的 dataclass，用 `in` 会把
            # 字段值完全相同的另一张发票也误选进来（导致重复计入汇总）
            ids = [i for i in sorted(available) if any(inv_dtos[i] is d for d in combo)]
            if not ids:
                continue
            msg = (f"汇总匹配：{len(combo)} 张发票合计等于报销单金额 {reb.amount}，"
                   f"请人工确认归属")
            for inv_id in ids:
                ps = PairScore(score=0.90, match_type=T_MANY_TO_ONE,
                               diff_fields=[f"汇总匹配: {len(combo)} 张发票合计 = 报销单金额"],
                               field_notes=[msg], capped=True, cap_reason="组合匹配需人工确认")
                mid = _persist_pair(conn, inv_id, None, ps, "SUSPICIOUS", T_MANY_TO_ONE)
                match_repo.insert_pair(conn, mid, inv_id, reb_row["id"])
                available.discard(inv_id)
                has_candidate.add(inv_id)
                summary["many_to_one"] += 1
            done.add(reb_row["id"])
            summary["pending"] += 1
            _write_note(conn, reb_row, [msg])

        # ================================================================
        # 第三轮：模糊特征兜底
        #   收录条件（任一满足即可）：
        #   a) 总分 >= 0.70；
        #   b) 疑似发票号填错（号不一致但金额一致且日期相近，裁决2）；
        #   c) 低置信度兜底（无号 + 金额误差 <= 1.00 元，裁决4）。
        #   仅号码冲突且不满足「疑似填错」（is_rejected）的候选才淘汰。
        # ================================================================
        for reb_row in reimburses:
            if reb_row["id"] in done:
                continue
            reb = _reb_dto(reb_row)
            candidates: List[tuple] = []
            for inv_id in sorted(available):
                ps = _engine.score_pair(inv_dtos[inv_id], reb, company_tax_nos)
                if ps.is_rejected:
                    continue                  # 号码冲突且无金额/日期佐证 -> 淘汰
                if (ps.score < settings.suspicious_threshold
                        and not ps.suspected_no_error
                        and not ps.is_low_confidence):
                    continue                  # 低于门槛且无兜底通道 -> 不推前端
                candidates.append((inv_id, ps))

            if not candidates:
                if reb.invoice_no:
                    _write_note(conn, reb_row, [
                        f"未找到可比对的发票（发票号 {reb.invoice_no} 无可用发票，"
                        f"可能不存在或已被其他报销单占用）"])
                else:
                    _write_note(conn, reb_row, ["未找到可比对的发票（报销单未填发票号，且金额无匹配）"])
                continue

            # 排序仅用于展示顺序：金额误差小的靠前，其次发票 ID 升序（裁决3：不选定首选）
            candidates.sort(key=lambda t: (t[1].amount_error, t[0]))
            top_ps = candidates[0][1]
            # 平局 = 同分（全部候选都落库展示，不再挑首选）
            tied = [t for t in candidates if t[1].score == top_ps.score]
            tie_note = (_ambiguity_note([t[0] for t in tied], inv_rows, reb_row)
                        if len(tied) > 1 else "")

            for inv_id, ps in candidates:
                notes = list(ps.field_notes)
                if ps.suspected_no_error:
                    notes.append("⚠️ 疑似发票号填错，请核实并修正报销单发票号")
                if ps.is_low_confidence:
                    notes.append(
                        f"[低置信度推荐] 无发票号且金额误差 {ps.amount_error} 元，请重点核查金额")
                if tie_note:
                    notes.append(tie_note)
                if ps.capped:
                    notes.append(f"[ - ] {ps.cap_reason}（安全锁：不可自动通过，请人工确认）")
                ps.field_notes = notes
                _persist_pair(conn, inv_id, reb_row["id"], ps, "SUSPICIOUS", ps.match_type)
                has_candidate.add(inv_id)
            summary["pending"] += 1
            _write_note(conn, reb_row, list(top_ps.field_notes) if not tie_note else [tie_note])

        # ---- 软占用检测：同一发票被多张报销单同时列为候选 -> 记录高亮警告（模块一.2）----
        soft_rows = conn.execute(
            "SELECT id, invoice_id, reimburse_id FROM match_result "
            "WHERE status IN ('UNMATCHED','SUSPICIOUS') AND reimburse_id IS NOT NULL").fetchall()
        by_inv: Dict[int, set] = {}
        row_ids_by_inv: Dict[int, List[int]] = {}
        for r in soft_rows:
            by_inv.setdefault(r["invoice_id"], set()).add(r["reimburse_id"])
            row_ids_by_inv.setdefault(r["invoice_id"], []).append(r["id"])
        for inv_id, rids in by_inv.items():
            if len(rids) <= 1:
                continue
            inv_no = (inv_rows.get(inv_id) or {}).get("invoice_no") or f"#{inv_id}"
            reb_nos = "、".join(
                (reb_map.get(rid) or {}).get("reimburse_no") or f"#{rid}"
                for rid in sorted(rids))
            warn = (f"⚠️ 该发票（{inv_no}）同时被多张报销单作为候选：{reb_nos}，请核实")
            ids = row_ids_by_inv[inv_id]
            marks = ", ".join("?" for _ in ids)
            conn.execute(
                f"UPDATE match_result SET message = message || '；' || ? "
                f"WHERE id IN ({marks})", [warn] + ids)

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
    # 需求4：message 与报销单备注说明同源，统一取 field_notes（智能提醒文案）
    msg = "；".join(ps.field_notes) if ps.field_notes else (
        "; ".join(ps.diff_fields) if ps.diff_fields else
        (f"字段冲突: {ps.conflict_fields}" if ps.conflict_fields else ""))
    return match_repo.insert(conn, {
        "invoice_id": invoice_id,
        "reimburse_id": reimburse_id,
        "match_type": match_type,
        "match_score": ps.score,
        "status": status,
        "diff_fields": ps.diff_fields,
        "message": msg,
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
            # 匹配生效瞬间校验双方分类（模块四）
            _detect_conflict(conn, reb_id)
            # 已生效匹配：清掉报销单备注里的不匹配说明
            reb_row = reimburse_repo.get(conn, reb_id)
            if reb_row:
                _write_note(conn, reb_row, [])
        # 人工终裁：踢出同发票其他候选 + 同报销单未选中的候选
        _finalize_binding(conn, match_id, m["invoice_id"], reb_id, operator)
        return {"ok": True, "message": "已确认匹配"}


def reject(match_id: int, operator: str = None, note: str = "") -> dict:
    """人工拒绝待处理匹配 -> REJECTED（移出待处理列表）。"""
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m["status"] not in ("SUSPICIOUS", "CONFLICT", "UNMATCHED"):
            return {"ok": False, "message": f"仅待处理记录可拒绝（当前: {m['status']}）"}
        match_repo.update_status(conn, match_id, "REJECTED", operator)
        if note:
            _append_message(conn, match_id, f"拒绝备注: {note}")
        return {"ok": True, "message": "已拒绝匹配"}


# ---------------------------------------------------------------- 一键更正
def auto_correct(match_id: int, operator: str = None) -> dict:
    """一键更正（需求5）：以发票为准更正报销单填报字段，并立即重新精确匹配。

    更正字段：发票号、报销金额（= 发票价税合计）。所有更正项留痕（原值 -> 新值）。
    全流程在单个事务内完成：更正 -> 重新评分 -> 状态修改。
    成功：记录 MATCHED，发票 MATCHED，报销单备注清空。
    失败：更正结果仍保留（对齐发票事实），记录保持 UNMATCHED，message 附带原因。
    """
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m["status"] not in ("UNMATCHED", "SUSPICIOUS", "CONFLICT"):
            return {"ok": False, "message": f"仅待处理记录可一键更正（当前: {m['status']}）"}
        if m["reimburse_id"] is None:
            return {"ok": False, "message": "该记录无关联报销单，无法更正"}
        inv_row = invoice_repo.get(conn, m["invoice_id"])
        reb_row = reimburse_repo.get(conn, m["reimburse_id"])
        if inv_row is None or reb_row is None:
            return {"ok": False, "message": "发票或报销单不存在"}
        if inv_row["status"] != "NORMAL":
            return {"ok": False, "message": f"建议发票当前状态为 {inv_row['status']}，不可用于更正"}
        if match_repo.active_for_invoice(conn, m["invoice_id"]):
            return {"ok": False, "message": "建议发票已被其他报销单占用"}
        if match_repo.active_for_reimburse(conn, reb_row["id"]):
            return {"ok": False, "message": "该报销单已存在生效匹配"}

        correct_no = inv_row.get("invoice_no") or ""
        if not correct_no:
            return {"ok": False, "message": "建议发票无发票号，无法更正"}

        # 1) 以发票为准更正报销单字段（同事务，逐项留痕）
        corrections: List[str] = []
        old_no = reb_row.get("invoice_no") or ""
        if old_no != correct_no:
            conn.execute("UPDATE reimburse SET invoice_no = ? WHERE id = ?",
                         (correct_no, reb_row["id"]))
            reb_row["invoice_no"] = correct_no
            corrections.append(f"发票号 {old_no} → {correct_no}")
        # 金额按 Decimal 规范化比较，避免 "29.5" vs "29.50" 字符串误判
        old_amt = to_decimal(reb_row.get("amount"))
        new_amt = to_decimal(inv_row.get("total_amount"))
        if new_amt is not None and old_amt != new_amt:
            conn.execute("UPDATE reimburse SET amount = ? WHERE id = ?",
                         (str(new_amt), reb_row["id"]))
            reb_row["amount"] = str(new_amt)
            corrections.append(f"报销金额 {old_amt} → {new_amt}（按发票价税合计更正）")
        trail = "；".join(corrections) if corrections else "报销单填报字段无需更正"

        # 2) 立即重新评分（更正后应为精确命中）
        ps = _engine.score_pair(_inv_dto(inv_row), _reb_dto(reb_row),
                                company_repo.tax_nos(conn))
        if _engine.decide_status(ps) == "MATCHED":
            match_repo.update_status(conn, match_id, "MATCHED", operator)
            conn.execute(
                "UPDATE match_result SET match_type = 'exact', match_score = ?, message = ? "
                "WHERE id = ?",
                (ps.score, f"一键更正：{trail}，重新精确匹配成功", match_id))
            change_invoice_status(conn, m["invoice_id"], "MATCHED",
                                  f"一键更正后精确匹配 (match#{match_id})", operator)
            _detect_conflict(conn, m["reimburse_id"])
            _write_note(conn, reb_row, [])
            return {"ok": True, "message": f"一键更正成功：{trail}，重新精确匹配通过"}

        # 3) 仍不匹配（如日期超期间等）：更正结果保留，保持 UNMATCHED 并写明原因
        reason = "；".join(ps.field_notes) or (
            "; ".join(ps.diff_fields) if ps.diff_fields else "更正后评分仍不足")
        conn.execute(
            "UPDATE match_result SET status = 'UNMATCHED', match_score = ?, message = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (ps.score, f"一键更正：{trail}；更正后仍不匹配：{reason}", match_id))
        _write_note(conn, reb_row, [f"更正后仍不匹配：{reason}"])
        return {"ok": False, "message": f"已更正：{trail}；但仍不匹配：{reason}，请人工重新检查"}


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
        # 匹配生效瞬间校验双方分类（模块四）
        _detect_conflict(conn, reimburse_id)
        # 已生效匹配：清掉报销单备注里的不匹配说明
        reb_row = reimburse_repo.get(conn, reimburse_id)
        if reb_row:
            _write_note(conn, reb_row, [])
        # 人工终裁：踢出同发票其他候选 + 同报销单未选中的候选
        kicked = _finalize_binding(conn, mid, invoice_id, reimburse_id, operator)
        return {"ok": True,
                "message": "人工匹配成功" + (f"（已踢出 {kicked} 条其他候选）" if kicked else "")}


def unmatch(match_id: int, operator: str = None, reason: str = "") -> dict:
    """解除生效匹配（人工操作，记录原因）。

    解除后两边各自保留解除前的分类，恢复完全独立的孤岛状态（模块三.3）。
    """
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m["status"] not in ("MATCHED", "CONFIRMED"):
            return {"ok": False, "message": f"仅生效匹配可解除（当前: {m['status']}）"}

        reb_id = m["reimburse_id"]
        if reb_id is None:
            pairs = match_repo.pairs_of(conn, [match_id]).get(match_id, [])
            reb_id = pairs[0]["reimburse_id"] if pairs else None

        # 解除前快照双方分类（用于审计留痕，说明两边各自保留）
        from app.service import audit_service, category_service
        detail = "解除后双方分类各自保留"
        if reb_id:
            reb = reimburse_repo.get(conn, reb_id) or {}
            reb_cat, _ = category_service.reimburse_explicit(conn, reb)
            inv_cat = (category_service.invoice_confirmed_map(conn, [m["invoice_id"]])
                       .get(m["invoice_id"]) or "")
            detail = ("解除后双方分类各自保留：发票「%s」/ 报销单「%s」"
                      % (inv_cat or "—", reb_cat or "—"))
            category_service.clear_conflict(conn, reb_id)

        match_repo.update_status(conn, match_id, "REJECTED", operator)
        _append_message(conn, match_id, f"解除匹配: {reason or '无备注'}")
        try:
            change_invoice_status(conn, m["invoice_id"], "NORMAL",
                                  f"解除匹配 (match#{match_id}): {reason or '无备注'}", operator)
        except Exception as e:
            logger.warning("解除匹配时发票状态回转失败: %s", e)
        audit_service.log(conn, "unmatch", "match", match_id,
                          "#%s（发票 #%s）" % (match_id, m["invoice_id"]),
                          "已匹配", "已解除", operator,
                          (reason or "无备注") + "；" + detail)
        return {"ok": True, "message": "匹配已解除，发票回到 NORMAL；双方分类各自保留"}


def _append_message(conn, match_id: int, text: str) -> None:
    m = match_repo.get(conn, match_id)
    msg = (m["message"] + " | " + text) if m.get("message") else text
    conn.execute("UPDATE match_result SET message = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                 (msg, match_id))


def _finalize_binding(conn, match_id: int, invoice_id: int,
                      reb_id: Optional[int], operator: str) -> int:
    """人工终裁收尾（模块一.3）：发票正式绑定后——
    1. 踢出该发票在其他报销单候选池中的候选记录；
    2. 淘汰该报销单未被选中的其他候选发票。
    被踢出的候选记录置为 REJECTED 并留痕，同时写入审计日志。
    """
    kicked: List[int] = []
    for other in match_repo.list(conn, statuses=["UNMATCHED", "SUSPICIOUS", "CONFLICT"],
                                 limit=10000):
        if other["id"] == match_id:
            continue
        same_invoice = other["invoice_id"] == invoice_id
        same_reb = bool(reb_id) and other["reimburse_id"] == reb_id \
            and other["invoice_id"] != invoice_id
        if same_invoice or same_reb:
            match_repo.update_status(conn, other["id"], "REJECTED", operator)
            _append_message(conn, other["id"], "候选流失：归属已确定，本候选自动淘汰")
            kicked.append(other["id"])
    if kicked:
        from app.service import audit_service
        audit_service.log(conn, "match_arbitrate", "match", match_id,
                          "发票 #%s" % invoice_id, "候选池 %d 条" % len(kicked),
                          "正式绑定", operator,
                          "人工终裁踢出候选记录：%s" % kicked)
    return len(kicked)


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
        # 软占用标记：同一发票被多张报销单同时列为候选（待人工终裁，模块一.2）
        pending_rebs: Dict[int, set] = {}
        for r in rows:
            if r["status"] in ("UNMATCHED", "SUSPICIOUS") and r.get("reimburse_id"):
                pending_rebs.setdefault(r["invoice_id"], set()).add(r["reimburse_id"])
        soft = {i for i, s in pending_rebs.items() if len(s) > 1}
        for r in rows:
            r["soft_locked"] = (r["status"] in ("UNMATCHED", "SUSPICIOUS")
                                and r["invoice_id"] in soft)
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
