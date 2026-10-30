"""匹配用例（应用服务层）：执行匹配 / 人工确认 / 拒绝 / 手动匹配 / 解除匹配。

流程（核心闭环 §1.2）：去重后 NORMAL 发票 x 未占用报销单 -> 评分 -> 状态机。
所有写操作在单事务内（I9）；发票状态变更经 status_flow（I7）。
"""
import difflib
import json
from decimal import Decimal
from typing import Dict, List, Optional

from app.config import settings
from app.domain.dto import InvoiceDTO, ReimburseDTO
from app.domain.features import with_reimburse_features
from app.domain.matcher import MatchEngine, PairScore
from app.domain.normalize import to_decimal
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import (category_repo, company_repo, invoice_repo,
                                  match_repo, reimburse_repo)
from app.service.status_flow import change_invoice_status

logger = get_logger(__name__)
_engine = MatchEngine()

# 匹配类型常量（与 match_result.CHECK 约束一致）
T_EXACT, T_COMPOSITE, T_FUZZY = "exact", "composite", "fuzzy"
T_MANY_TO_ONE, T_MANUAL = "many_to_one", "manual"

# 不匹配项写入报销单备注时的标记：保留用户原始备注，重复执行可安全覆盖
NOTE_MARK = "【不匹配】"

# 人工干预原因预设（与前端下拉框保持一致；"特殊情况" 必须附备注）
REASON_OPTIONS = ["业务人员已确认", "员工补了凭证", "票号填错已核实", "特殊情况"]


def _validate_reason_option(reason_option: str, reason: str = "") -> Optional[str]:
    """统一校验 reason_option：若选了「特殊情况」，reason 必须非空。返回 None 表示合法；否则返回错误文案。"""
    if reason_option == "特殊情况" and not (reason or "").strip():
        return "选择「特殊情况」时必须填写备注说明"
    return None


class StaleError(Exception):
    """并发失效：目标数据状态已被其它页面改变（API 层据此返回 409 STALE）。"""

    def __init__(self, message: str = "该数据已失效，已为您刷新"):
        super().__init__(message)
        self.message = message


def _guard_status(m: dict, expect_status: str) -> None:
    """乐观并发校验：前端期望状态与库内不一致，判定该条数据已失效。"""
    if expect_status and m["status"] != expect_status:
        raise StaleError("该数据已失效（当前状态：%s），已为您刷新" % m["status"])


def _inv_dto(row: dict) -> InvoiceDTO:
    """发票 DTO：带销方名称与项目摘要，供「文本/抬头」维度做相似度比对。

    无票号票据的车牌号 / 上下车时间 / 票面文字存在 raw_json.traffic 中，回读供
    「业务特征碰撞」使用（不影响有票号的唯一键短路）。
    """
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        raw = {}
    traffic = raw.get("traffic") or {}
    return InvoiceDTO(
        invoice_code=row.get("invoice_code") or "",
        invoice_no=row.get("invoice_no") or "",
        invoice_date=row.get("invoice_date"),
        buyer_tax_no=row.get("buyer_tax_no") or "",
        seller_name=row.get("seller_name") or "",
        item_summary=row.get("item_summary") or "",
        total_amount=to_decimal(row.get("total_amount")),
        plate_no=traffic.get("plate_no") or "",
        ride_time=traffic.get("ride_time") or "",
        traffic_text=traffic.get("text") or "",
    )


def _reb_dto(row: dict) -> ReimburseDTO:
    """报销单 DTO：备注剥离系统写入的【不匹配】说明，只留用户原文。

    同时补齐事由 / 起止地点等业务特征（来自 extra_data 或备注），
    供无票号场景的文本碰撞打分。
    """
    dto = ReimburseDTO(
        reimburse_no=row.get("reimburse_no") or "",
        reimburse_date=row.get("reimburse_date"),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        amount=to_decimal(row.get("amount")),
        invoice_no=row.get("invoice_no") or "",
        remark=_strip_note(row.get("remark")),
        extra_data=row.get("extra_data") or {},
    )
    return with_reimburse_features(dto)


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
        # 第一轮：唯一键短路匹配（matcher.py 内建三段判断）
        #   报销单有发票号 -> 按号精确查找 available 池中的发票：
        #     情况 A（完美命中） / B（金额微差 ≤1 元）-> auto_matchable=True -> 直接 MATCHED
        #     情况 C（金额大差 >1 元）-> CONFLICT 留痕，但**不占用**发票和报销单（可能填了假号）
        #   注：短路已跳过安全锁/日期/文本等所有后续打分；无号场景直接交第三轮
        # ================================================================
        for reb_row in reimburses:
            summary["evaluated_reimburses"] += 1
            reb = _reb_dto(reb_row)
            reb_no = (reb.invoice_no or "").strip()
            if not reb_no:
                continue                      # 无号：交第三轮多维特征兜底
            found_same_no = False
            for inv_id in sorted(available):
                inv = inv_dtos[inv_id]
                if (inv.invoice_no or "").strip() != reb_no:
                    continue
                found_same_no = True
                ps = _engine.score_pair(inv, reb, company_tax_nos)

                if ps.auto_matchable:
                    # ----- 情况 A / B：唯一键自动匹配 -----
                    mid = _persist_pair(conn, inv_id, reb_row["id"], ps, "MATCHED", ps.match_type)
                    change_invoice_status(conn, inv_id, "MATCHED",
                                          f"第一轮唯一键直配（{ps.auto_match_reason}, match#{mid}）",
                                          operator)
                    available.discard(inv_id)
                    done.add(reb_row["id"])
                    summary["auto_matched"] += 1
                    if _detect_conflict(conn, reb_row["id"]):
                        summary["category_conflict"] += 1
                    _write_note(conn, reb_row, [])

                elif "amount" in ps.conflict_fields:
                    # ----- 情况 C：号对上但金额差大（>1 元）-----
                    # 留痕供人工排查，但不占用发票和报销单——让第三轮还能给它们配对
                    mid = _persist_pair(conn, inv_id, reb_row["id"], ps, "UNMATCHED", ps.match_type)
                    has_candidate.add(inv_id)
                    _write_note(conn, reb_row, list(ps.field_notes))

                # 其他情况（score_pair 走了第二阶段：号不一致或一方缺金额）—— 不处理，交第三轮
                break

            # 同号发票找不到（found_same_no=False）-> 可能填了不存在的号，交第三轮
            # 其他：情况 A/B/C 处理完，换下一个报销单（同号只有一张）

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
                # 「疑似发票号填错」的详细说明已由 score_pair 写入 field_notes，此处不再重复追加
                if ps.is_low_confidence:
                    if not ps.applicable.get("no"):
                        notes.append(
                            f"[低置信度推荐] 无发票号且金额误差 {ps.amount_error} 元，请重点核查金额")
                    else:
                        notes.append(
                            f"[低置信度推荐] 发票号一致但金额微差 {ps.amount_error} 元，请重点核查金额")
                if tie_note:
                    notes.append(tie_note)
                # 疑似号填错时 cap_reason 本身就是该结论，已在上方说明，避免同义重复
                if ps.capped and not ps.suspected_no_error:
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


def _message_of(ps: PairScore) -> str:
    """评分结果 -> 展示文案：绿色命中点（✅）在前，差异说明在后。"""
    points = "；".join(ps.match_points)
    notes = "；".join(ps.field_notes)
    if points and notes:
        return f"{points}；{notes}"
    if points:
        return points
    return notes or ("; ".join(ps.diff_fields) if ps.diff_fields else
                     (f"字段冲突: {ps.conflict_fields}" if ps.conflict_fields else ""))


def _persist_pair(conn, invoice_id: int, reimburse_id: Optional[int],
                  ps: PairScore, status: str, match_type: str) -> int:
    return match_repo.insert(conn, {
        "invoice_id": invoice_id,
        "reimburse_id": reimburse_id,
        "match_type": match_type,
        "match_score": ps.score,
        "status": status,
        "diff_fields": ps.diff_fields,
        "message": _message_of(ps),
        "match_points": ps.match_points,
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
def confirm(match_id: int, operator: str = None, note: str = "",
            expect_status: str = "", reason_option: str = "") -> dict:
    """人工确认 SUSPICIOUS/CONFLICT 匹配 -> CONFIRMED，发票 -> MATCHED。"""
    err = _validate_reason_option(reason_option, note)
    if err:
        return {"ok": False, "message": err}
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        _guard_status(m, expect_status)
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
        from app.service import audit_service
        inv = invoice_repo.get(conn, m["invoice_id"]) or {}
        reb = reimburse_repo.get(conn, reb_id) if reb_id else {}
        conf_detail = {
            "why": note or "人工确认匹配",
            "reason_option": reason_option or "",
            "invoice_id": m["invoice_id"],
            "invoice_no": inv.get("invoice_no") or "",
            "reimburse_id": reb_id,
            "reimburse_no": (reb or {}).get("reimburse_no") or "",
            "match_id": match_id,
            "changes": [{"field": "匹配状态",
                         "old": m["status"], "new": "CONFIRMED"}],
        }
        if reason_option:
            conf_detail["changes"].append(
                {"field": "确认依据", "old": "-", "new": reason_option})
        match_repo.update_detail(conn, match_id, conf_detail)
        audit_service.log(conn, "match_confirm", "match", match_id,
                          (f"发票:{inv.get('invoice_no') or '（无号）'} / "
                           f"报销单:{(reb or {}).get('reimburse_no') or '—'}"),
                          m["status"], "CONFIRMED",
                          operator, note or "人工确认匹配",
                          detail_json=conf_detail)
        return {"ok": True, "message": "已确认匹配"}


def reject(match_id: int, operator: str = None, note: str = "",
           expect_status: str = "", reason_option: str = "") -> dict:
    """人工拒绝待处理匹配 -> REJECTED（移出待处理列表）。"""
    err = _validate_reason_option(reason_option, note)
    if err:
        return {"ok": False, "message": err}
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        _guard_status(m, expect_status)
        if m["status"] not in ("SUSPICIOUS", "CONFLICT", "UNMATCHED"):
            return {"ok": False, "message": f"仅待处理记录可拒绝（当前: {m['status']}）"}
        match_repo.update_status(conn, match_id, "REJECTED", operator)
        if note:
            _append_message(conn, match_id, f"拒绝备注: {note}")
        from app.service import audit_service
        inv = invoice_repo.get(conn, m["invoice_id"]) or {}
        reb_id = m.get("reimburse_id")
        reb = reimburse_repo.get(conn, reb_id) if reb_id else {}
        rej_detail = {
            "why": note or "人工拒绝匹配",
            "reason_option": reason_option or "",
            "invoice_id": m["invoice_id"],
            "invoice_no": inv.get("invoice_no") or "",
            "reimburse_id": reb_id,
            "reimburse_no": (reb or {}).get("reimburse_no") or "",
            "match_id": match_id,
            "changes": [{"field": "匹配状态",
                         "old": m["status"], "new": "REJECTED"}],
        }
        if reason_option:
            rej_detail["changes"].append(
                {"field": "拒绝依据", "old": "-", "new": reason_option})
        match_repo.update_detail(conn, match_id, rej_detail)
        audit_service.log(conn, "match_reject", "match", match_id,
                          (f"发票:{inv.get('invoice_no') or '（无号）'} / "
                           f"报销单:{(reb or {}).get('reimburse_no') or '—'}"),
                          m["status"], "REJECTED",
                          operator, note or "人工拒绝匹配",
                          detail_json=rej_detail)
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
        mil = (f"发票:{inv_row.get('invoice_no') or '（无号）'} / "
               f"报销单:{reb_row.get('reimburse_no') or '—'}")
        inv_no_a = inv_row.get("invoice_no") or ""
        reb_no_a = reb_row.get("reimburse_no") or ""
        fix2_detail = {
            "why": trail, "reason_option": "",
            "invoice_id": m["invoice_id"], "invoice_no": inv_no_a,
            "reimburse_id": m["reimburse_id"], "reimburse_no": reb_no_a,
            "match_id": match_id,
            "changes": [{"field": "更正内容", "old": "-", "new": trail}],
        }
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
            from app.service import audit_service
            fix2_detail["changes"].append(
                {"field": "匹配状态", "old": m["status"], "new": "MATCHED"})
            match_repo.update_detail(conn, match_id, fix2_detail)
            audit_service.log(conn, "match_auto_correct", "match", match_id,
                              mil, m["status"], "MATCHED",
                              operator, f"一键更正并重新精确匹配：{trail}",
                              detail_json=fix2_detail)
            return {"ok": True, "message": f"一键更正成功：{trail}，重新精确匹配通过"}

        # 3) 仍不匹配（如日期超期间等）：更正结果保留，保持 UNMATCHED 并写明原因
        reason = "；".join(ps.field_notes) or (
            "; ".join(ps.diff_fields) if ps.diff_fields else "更正后评分仍不足")
        conn.execute(
            "UPDATE match_result SET status = 'UNMATCHED', match_score = ?, message = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (ps.score, f"一键更正：{trail}；更正后仍不匹配：{reason}", match_id))
        _write_note(conn, reb_row, [f"更正后仍不匹配：{reason}"])
        from app.service import audit_service
        fix2_detail["changes"].append(
            {"field": "匹配状态", "old": m["status"], "new": "UNMATCHED"})
        fix2_detail["changes"].append(
            {"field": "仍不匹配原因", "old": "-", "new": reason})
        match_repo.update_detail(conn, match_id, fix2_detail)
        audit_service.log(conn, "match_auto_correct", "match", match_id,
                          mil, m["status"], "UNMATCHED",
                          operator, f"一键更正后仍不匹配：{trail}；{reason}",
                          detail_json=fix2_detail)
        return {"ok": False, "message": f"已更正：{trail}；但仍不匹配：{reason}，请人工重新检查"}


def _amount_gap(inv_rows: List[dict], reb_amount) -> Optional[Decimal]:
    """汇总绑定金额差异：发票价税合计之和与报销金额之差的绝对值。"""
    reb_amt = to_decimal(reb_amount)
    if reb_amt is None:
        return None
    total = sum((to_decimal(i.get("total_amount")) or Decimal("0")) for i in inv_rows)
    return abs(total - reb_amt)


def manual_match(invoice_id: int, reimburse_id: int, operator: str = None,
                 reason_option: str = "", reason: str = "") -> dict:
    """人工指定匹配（manual 类型，直接生效；金额有差异时允许强绑并打标）。"""
    err = _validate_reason_option(reason_option, reason)
    if err:
        return {"ok": False, "message": err}
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

        gap = _amount_gap([inv], reb.get("amount"))
        inv_no = inv.get("invoice_no") or ""
        reb_no = reb.get("reimburse_no") or ""
        basis = reason_option or "人工指定"
        reason_text = reason or basis
        if gap:
            reason_text += f"（金额差异 ¥{gap:.2f}，已强制绑定）"
        message = f"人工绑定 [发票:{inv_no or '（无号）'}] 至 [报销单:{reb_no}]（依据: {basis}）"
        manual_detail = {
            "why": reason_text,
            "reason_option": reason_option or "",
            "invoice_id": invoice_id,
            "invoice_no": inv_no,
            "reimburse_id": reimburse_id,
            "reimburse_no": reb_no,
            "match_id": None,
            "changes": [
                {"field": "绑定依据", "old": "-", "new": basis},
                {"field": "汇总金额差异",
                 "old": "-",
                 "new": f"¥{gap:.2f}" if gap else "无"},
            ],
        }
        mid = match_repo.insert(conn, {
            "invoice_id": invoice_id, "reimburse_id": reimburse_id,
            "match_type": T_MANUAL, "match_score": 1.0, "status": "CONFIRMED",
            "diff_fields": ["amount"] if gap else [], "message": message,
            "detail": manual_detail,
            "confirmed_by": operator, "confirmed_at": None,
        })
        manual_detail["match_id"] = mid
        match_repo.update_detail(conn, mid, manual_detail)
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
        from app.service import audit_service
        audit_service.log(conn, "match_manual", "match", mid,
                          f"发票:{inv_no or '（无号）'} / 报销单:{reb_no or '—'}",
                          "UNMATCHED", "CONFIRMED", operator, reason_text,
                          detail_json=manual_detail)
        return {"ok": True,
                "message": "人工匹配成功" + (f"（已踢出 {kicked} 条其他候选）" if kicked else ""),
                "amount_diff": float(gap) if gap is not None else None}


def manual_batch_bind(invoice_ids: List[int], reimburse_id: int,
                      operator: str = None,
                      reason_option: str = "", reason: str = "") -> dict:
    """把多张未报销发票批量绑定到同一张报销单（人工汇总绑定）。

    复用与 many_to_one 相同的存法：每张发票一条 MATCHED 记录（match_type=manual，
    reimburse_id 为空）+ match_pair 指向同一报销单。金额存在差异时允许强绑并在
    差异说明打标（汇总比对：发票价税合计之和 vs 报销金额）。
    """
    err = _validate_reason_option(reason_option, reason)
    if err:
        return {"ok": False, "message": err}
    operator = operator or settings.default_operator
    ids = list(dict.fromkeys([int(x) for x in (invoice_ids or []) if x]))
    if not ids:
        return {"ok": False, "message": "未选择需要绑定的发票"}

    with db.tx() as conn:
        reb = reimburse_repo.get(conn, reimburse_id)
        if reb is None:
            return {"ok": False, "message": "报销单不存在"}
        if match_repo.active_for_reimburse(conn, reimburse_id):
            return {"ok": False, "message": "该报销单已存在生效匹配，请先解绑"}

        # 第一步：全部校验通过才写入（防抢占，避免部分成功）
        invs = []
        for iid in ids:
            inv = invoice_repo.get(conn, iid)
            if inv is None:
                return {"ok": False, "message": f"发票 #{iid} 不存在"}
            if inv["status"] != "NORMAL":
                return {"ok": False,
                        "message": f"发票 #{iid} 当前状态为 {inv['status']}，仅 NORMAL 可绑定"}
            if match_repo.active_for_invoice(conn, iid) is not None:
                return {"ok": False, "message": f"发票 #{iid} 已被占用，请先解绑"}
            invs.append(inv)

        gap = _amount_gap(invs, reb.get("amount"))
        reb_no = reb.get("reimburse_no") or ""
        basis = reason_option or "人工批量绑定"
        reason_text = reason or basis
        if gap:
            reason_text += f"（汇总金额差异 ¥{gap:.2f}，已强制绑定）"

        match_ids: List[int] = []
        for inv in invs:
            inv_no = inv.get("invoice_no") or ""
            msg_tail = f"（汇总金额差异 ¥{gap:.2f}）" if gap else ""
            note = (f"人工绑定 [发票:{inv_no or '（无号）'}] "
                    f"至 [报销单:{reb_no}]（依据: {basis}）{msg_tail}")
            batch_detail = {
                "why": reason_text,
                "reason_option": reason_option or "",
                "invoice_id": inv["id"],
                "invoice_no": inv_no,
                "reimburse_id": reimburse_id,
                "reimburse_no": reb_no,
                "match_id": None,
                "batch": True,
                "changes": [
                    {"field": "绑定依据", "old": "-", "new": basis},
                    {"field": "汇总金额差异", "old": "-",
                     "new": f"¥{gap:.2f}" if gap else "无"},
                ],
            }
            match_repo.delete_for_invoice(conn, inv["id"])
            mid = match_repo.insert(conn, {
                "invoice_id": inv["id"], "reimburse_id": None,
                "match_type": T_MANUAL, "match_score": 1.0, "status": "MATCHED",
                "diff_fields": ["amount"] if gap else [], "message": note,
                "detail": batch_detail, "confirmed_by": operator,
            })
            batch_detail["match_id"] = mid
            match_repo.update_detail(conn, mid, batch_detail)
            match_repo.insert_pair(conn, mid, inv["id"], reimburse_id)
            change_invoice_status(conn, inv["id"], "MATCHED",
                                  f"人工批量绑定 (match#{mid})", operator)
            _finalize_binding(conn, mid, inv["id"], reimburse_id, operator)
            match_ids.append(mid)

        _detect_conflict(conn, reimburse_id)
        reb_row = reimburse_repo.get(conn, reimburse_id)
        if reb_row:
            _write_note(conn, reb_row, [])

        inv_nos = [i.get("invoice_no") or "（无号）" for i in invs]
        from app.service import audit_service
        audit_service.log(conn, "match_manual_batch", "reimburse", reimburse_id,
                          f"批量绑定 -> 报销单:{reb_no}",
                          "UNMATCHED", "MATCHED", operator, reason_text,
                          detail_json={
                              "why": reason_text,
                              "reason_option": reason_option or "",
                              "reimburse_id": reimburse_id,
                              "reimburse_no": reb_no,
                              "invoice_ids": [i["id"] for i in invs],
                              "invoice_nos": inv_nos,
                              "match_ids": match_ids,
                              "changes": [
                                  {"field": "绑定依据", "old": "-", "new": basis},
                                  {"field": "绑定发票数", "old": "-",
                                   "new": f"{len(invs)} 张"},
                                  {"field": "汇总金额差异", "old": "-",
                                   "new": f"¥{gap:.2f}" if gap else "无"},
                              ],
                          })
        return {
            "ok": True,
            "message": f"已批量绑定 {len(ids)} 张发票" + (f"（{note}）" if gap else ""),
            "match_ids": match_ids,
            "amount_diff": float(gap) if gap is not None else None,
        }


def quick_fix(match_id: int = None, invoice_id: int = None,
              reimburse_id: int = None, inv_fields: dict = None,
              reb_fields: dict = None, category: str = "",
              force_bind: bool = False, operator: str = None,
              reason: str = "", expect_status: str = "",
              reason_option: str = "") -> dict:
    """快捷修复：人工干预后的单点重算（不触发全量匹配）。

    事务内四步（核心闭环）：
    1. 数据校验与防抢占：目标发票/报销单已被其他记录占用则立即阻断，本次不做任何写入；
    2. 保存修改：按白名单更新发票/报销单字段，并记录 原值 -> 新值；
    3. 局部重匹配：仅对当前这一条报销单重新调用匹配引擎评分；
    4. 状态跃迁：MATCHED（match_type=manual 强制绑定）/ SUSPICIOUS / CONFLICT，
       解绑通过 unmatch() 单独完成。全过程写审计日志。
    """
    err = _validate_reason_option(reason_option, reason)
    if err:
        return {"ok": False, "message": err}
    operator = operator or settings.default_operator
    inv_fields = inv_fields or {}
    reb_fields = reb_fields or {}
    with db.tx() as conn:
        m = match_repo.get(conn, match_id) if match_id else None
        if match_id and m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        if m:
            _guard_status(m, expect_status)
            invoice_id = invoice_id or m["invoice_id"]
            if reimburse_id is None:
                reimburse_id = m["reimburse_id"]
                if reimburse_id is None:
                    pairs = match_repo.pairs_of(conn, [match_id]).get(match_id, [])
                    reimburse_id = pairs[0]["reimburse_id"] if pairs else None
        if invoice_id is None or reimburse_id is None:
            return {"ok": False, "message": "缺少发票或报销单，无法执行快捷修复"}

        inv = invoice_repo.get(conn, invoice_id)
        reb = reimburse_repo.get(conn, reimburse_id)
        if inv is None or reb is None:
            return {"ok": False, "message": "发票或报销单不存在"}
        if inv["status"] not in ("NORMAL", "MATCHED"):
            return {"ok": False,
                    "message": f"发票当前状态为 {inv['status']}，请先修正后再匹配"}

        # ---------- 第一步：防抢占（必须在任何写入之前完成） ----------
        guard_id = match_id or -1
        act_inv = match_repo.active_for_invoice(conn, invoice_id)
        if act_inv is not None and act_inv["id"] != guard_id:
            return {"ok": False, "message": "该发票已被占用，请先解绑"}
        act_reb = match_repo.active_for_reimburse(conn, reimburse_id)
        if act_reb is not None and act_reb["id"] != guard_id:
            return {"ok": False, "message": "该报销单已被占用，请先解绑"}

        # ---------- 第二步：保存修改（先校验格式，再统一落库） ----------
        parsed_inv: Dict[str, str] = {}
        for key in ("invoice_no", "invoice_code", "invoice_date", "total_amount"):
            raw = inv_fields.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            if key == "total_amount":
                dec = to_decimal(raw)
                if dec is None:
                    return {"ok": False, "message": "发票价税合计格式不正确"}
                parsed_inv[key] = str(dec)
            else:
                parsed_inv[key] = str(raw).strip()

        parsed_reb: Dict[str, str] = {}
        for key in ("amount", "invoice_no", "invoice_date", "expense_type"):
            raw = reb_fields.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            if key == "amount":
                dec = to_decimal(raw)
                if dec is None:
                    return {"ok": False, "message": "报销金额格式不正确"}
                parsed_reb[key] = str(dec)
            else:
                parsed_reb[key] = str(raw).strip()

        # message 摘要用短标签（金额/票号），变更对比表用完整字段名（报销金额/发票号）
        msg_labels = {"invoice_no": "票号", "invoice_code": "发票代码",
                      "invoice_date": "日期", "total_amount": "价税合计",
                      "amount": "金额", "expense_type": "费用类型"}
        labels = {"invoice_no": "发票号", "invoice_code": "发票代码",
                  "invoice_date": "日期", "total_amount": "价税合计",
                  "amount": "报销金额", "expense_type": "费用类型"}
        trail: List[str] = []
        changes: List[dict] = []
        inv_no_before = inv.get("invoice_no") or ""
        reb_no_before = reb.get("reimburse_no") or ""
        if parsed_inv:
            old_inv = dict(inv)
            invoice_repo.update(conn, invoice_id, parsed_inv)
            for k, v in parsed_inv.items():
                old_v = str(old_inv.get(k) or "")
                if old_v != v:
                    trail.append(f"{msg_labels.get(k, k)}: {old_v or '空'} -> {v}")
                    changes.append({"field": labels.get(k, k),
                                    "old": old_v or "空", "new": v})
        if parsed_reb:
            sets = ", ".join(f"{k} = ?" for k in parsed_reb)
            conn.execute(f"UPDATE reimburse SET {sets} WHERE id = ?",
                         list(parsed_reb.values()) + [reimburse_id])
            for k, v in parsed_reb.items():
                old_v = str(reb.get(k) or "")
                if old_v != v:
                    trail.append(f"{msg_labels.get(k, k)}: {old_v or '空'} -> {v}")
                    changes.append({"field": labels.get(k, k),
                                    "old": old_v or "空", "new": v})

        if category:
            old_cat = (category_repo.get(conn, invoice_id) or {}).get("category") or ""
            if old_cat != category:
                category_repo.upsert(conn, invoice_id, category, "manual")
                trail.append(f"发票分类: {old_cat or '未分类'} -> {category}")
                changes.append({"field": "发票分类",
                                "old": old_cat or "未分类", "new": category})

        # 变更后重新读取，作为局部重匹配的事实依据
        inv = invoice_repo.get(conn, invoice_id)
        reb = reimburse_repo.get(conn, reimburse_id)
        old_status = m["status"] if m else "UNMATCHED"

        # ---------- 第三步：局部重匹配（仅当前这一条报销单） ----------
        if force_bind:
            new_status = "MATCHED"
            ps = PairScore(score=1.0, match_type=T_MANUAL,
                           field_notes=[f"人工强制绑定：{reason or '财务快捷修复'}"],
                           auto_matchable=True)
            match_type = T_MANUAL
        else:
            ps = _engine.score_pair(_inv_dto(inv), _reb_dto(reb),
                                    company_repo.tax_nos(conn))
            new_status = _engine.decide_status(ps)
            # 仍然存在差异时：CONFLICT 保留冲突态，其余待人工项统一 SUSPICIOUS
            if new_status == "UNMATCHED":
                new_status = "SUSPICIOUS" if ps.field_notes else "UNMATCHED"
            match_type = ps.match_type

        # 规范化差异说明文本：按任务要求展示业务单号 + 变更前后
        inv_no = inv.get("invoice_no") or ""
        reb_no = reb.get("reimburse_no") or ""
        basis = reason_option or "财务快捷修复"
        message = (
            f"快捷修复 [报销单:{reb_no}] "
            + (f"({trail[0]}) " if trail else "(无字段变更) ")
            + f"匹配 [发票:{inv_no}] "
            + (f"（强制绑定，依据: {basis}）" if force_bind else "")
        )
        # 只取第一项 trail 作为 message 里的括号摘要，完整 trail 存 audit detail

        # ---------- 第四步：状态跃迁 + 关联更新 ----------
        if m is None:
            mid = match_repo.insert(conn, {
                "invoice_id": invoice_id, "reimburse_id": reimburse_id,
                "match_type": match_type, "match_score": ps.score,
                "status": new_status, "diff_fields": ps.diff_fields,
                "message": message, "match_points": ps.match_points,
                "confirmed_by": operator if new_status == "MATCHED" else None,
            })
        else:
            mid = match_id
            conn.execute(
                "UPDATE match_result SET status = ?, match_type = ?, match_score = ?, "
                "diff_fields = ?, message = ?, match_points = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (new_status, match_type, ps.score,
                 json.dumps(ps.diff_fields, ensure_ascii=False), message,
                 json.dumps(ps.match_points, ensure_ascii=False), mid))

        if new_status == "MATCHED":
            change_invoice_status(conn, invoice_id, "MATCHED",
                                  f"快捷修复后匹配成功 (match#{mid})", operator)
            _detect_conflict(conn, reimburse_id)
            _write_note(conn, reb, [])
            _finalize_binding(conn, mid, invoice_id, reimburse_id, operator)
        else:
            # 原生效匹配被修正后不再成立 -> 发票回退 NORMAL
            if inv["status"] == "MATCHED":
                change_invoice_status(conn, invoice_id, "NORMAL",
                                      f"快捷修复后不再匹配 (match#{mid})", operator)
            _write_note(conn, reb, list(ps.field_notes))

        from app.service import audit_service
        fix_detail = {
            "why": reason or basis,
            "reason_option": reason_option or "",
            "invoice_id": invoice_id,
            "invoice_no": inv_no,
            "reimburse_id": reimburse_id,
            "reimburse_no": reb_no,
            "match_id": mid,
            "changes": changes,
            "force_bind": bool(force_bind),
        }
        # 匹配记录同步留存结构化依据（模块三.2：match_result.detail 与 audit_log.detail 同源）
        match_repo.update_detail(conn, mid, fix_detail)
        audit_service.log(
            conn, "match_quick_fix", "match", mid,
            f"发票:{inv_no or '（无号）'} / 报销单:{reb_no or '—'}",
            old_status, new_status, operator,
            "；".join(trail) if trail else "无字段变更",
            detail_json=fix_detail)

        return {"ok": True,
                "message": ("快捷修复成功，已匹配" if new_status == "MATCHED"
                            else f"已保存修改，仍需人工处理（{new_status}）"),
                "status": new_status, "match_id": mid,
                "invoice_id": invoice_id, "reimburse_id": reimburse_id,
                "changes": trail}


def unmatch(match_id: int, operator: str = None, reason: str = "",
            expect_status: str = "", reason_option: str = "") -> dict:
    """解除生效匹配（人工操作，记录原因）。

    解除后两边各自保留解除前的分类，恢复完全独立的孤岛状态（模块三.3）。
    """
    err = _validate_reason_option(reason_option, reason)
    if err:
        return {"ok": False, "message": err}
    operator = operator or settings.default_operator
    with db.tx() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return {"ok": False, "message": f"匹配记录 #{match_id} 不存在"}
        _guard_status(m, expect_status)
        if m["status"] not in ("MATCHED", "CONFIRMED"):
            return {"ok": False, "message": f"仅生效匹配可解除（当前: {m['status']}）"}

        reb_id = m["reimburse_id"]
        if reb_id is None:
            pairs = match_repo.pairs_of(conn, [match_id]).get(match_id, [])
            reb_id = pairs[0]["reimburse_id"] if pairs else None

        # 解除前快照双方分类（用于审计留痕，说明两边各自保留）
        from app.service import audit_service, category_service
        reb_no = ""
        reb_cat = ""
        inv_cat = ""
        if reb_id:
            reb = reimburse_repo.get(conn, reb_id) or {}
            reb_no = reb.get("reimburse_no") or ""
            reb_cat, _ = category_service.reimburse_explicit(conn, reb)
        inv = invoice_repo.get(conn, m["invoice_id"]) or {}
        inv_no = inv.get("invoice_no") or ""
        detail = "解除后双方分类各自保留"
        if reb_id:
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
        unm_detail = {
            "why": reason or "无备注",
            "reason_option": reason_option or "",
            "invoice_id": m["invoice_id"],
            "invoice_no": inv_no,
            "reimburse_id": reb_id,
            "reimburse_no": reb_no,
            "match_id": match_id,
            "changes": [
                {"field": "匹配状态", "old": "已匹配", "new": "已解除"},
                {"field": "解除后处理", "old": "-", "new": detail},
            ],
        }
        if reason_option:
            unm_detail["changes"].append(
                {"field": "解除依据", "old": "-", "new": reason_option})
        match_repo.update_detail(conn, match_id, unm_detail)
        audit_service.log(conn, "unmatch", "match", match_id,
                          f"发票:{inv_no or '（无号）'} / 报销单:{reb_no or '—'}",
                          "已匹配", "已解除", operator,
                          (reason or "无备注") + "；" + detail,
                          detail_json=unm_detail)
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
        inv_no = (invoice_repo.get(conn, invoice_id) or {}).get("invoice_no") or "（无号）"
        reb_no = (reimburse_repo.get(conn, reb_id) or {}).get("reimburse_no") or "—" if reb_id else "—"
        audit_service.log(conn, "match_arbitrate", "match", match_id,
                          f"发票:{inv_no} / 报销单:{reb_no}", "候选池 %d 条" % len(kicked),
                          "正式绑定", operator,
                          f"人工终裁踢出 {len(kicked)} 条候选记录")
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
