"""匹配引擎（设计 §3.2 匹配状态机 + §5.3 match_result）。

评分维度（I10: score ∈ [0,1]，权重合计 1.0）：
  金额 0.50 + 发票号 0.30 + 日期 0.10 + 抬头 0.10

典型分值：
  发票号精确一致 + 金额一致 + 日期中性          -> 0.95 自动通过
  发票号精确一致 + 金额一致 + 期间/抬头命中      -> 1.00 自动通过
  发票号精确一致 + 金额不一致(超容差)           -> CONFLICT 字段冲突
  发票号模糊相似(>=0.85) + 金额一致             -> ~0.89 待人工
  仅金额一致(报销单未填发票号)                  -> ~0.70 待人工
  无有效信号                                   -> 不落库（发票记 UNMATCHED）

状态判定：
  字段冲突                      -> CONFLICT
  score >= 0.95                 -> MATCHED（自动通过）
  0.70 <= score < 0.95          -> SUSPICIOUS（待人工）
  score < 0.50（无候选）        -> UNMATCHED

匹配类型：exact / composite / fuzzy / many_to_one / manual
（one_to_many 因不变量 I3 一张发票最多一条生效匹配，引擎不生成该类型。）
"""
import difflib
import itertools
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Sequence

from app.config import settings
from app.domain.dto import InvoiceDTO, ReimburseDTO

# 权重
W_AMOUNT = 0.50
W_NO = 0.30
W_DATE = 0.10
W_TITLE = 0.10

# 金额模糊容差（相对/绝对取大者）
FUZZY_AMOUNT_REL = Decimal("0.05")
FUZZY_AMOUNT_ABS = Decimal("1")


@dataclass
class PairScore:
    """一次（发票, 报销单）配对的评分结果。"""
    score: float
    match_type: str                       # exact / composite / fuzzy
    conflict_fields: List[str] = field(default_factory=list)
    diff_fields: List[str] = field(default_factory=list)
    amount_diff: Decimal = Decimal("0")
    field_notes: List[str] = field(default_factory=list)   # 双方同类型字段的不一致项（写报销单备注）
    no_ratio: float = 0.0                     # 发票号原始相似度（总分相同时优先选错位更少的候选）


def _parse_d(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


class MatchEngine:
    """纯函数匹配评分引擎。"""

    def score_pair(self, inv: InvoiceDTO, reb: ReimburseDTO,
                   company_tax_nos: Optional[set] = None) -> PairScore:
        conflict_fields: List[str] = []
        diff_fields: List[str] = []
        # 仅记录"双方都有且不一致"的字段，用于回写报销单备注
        notes: List[str] = []

        # ---------- 发票号维度 ----------
        no_score = 0.0
        no_ratio = 0.0
        match_type = "fuzzy"
        inv_no, reb_no = inv.invoice_no, reb.invoice_no
        if inv_no and reb_no:
            if inv_no == reb_no:
                no_score = 1.0
                no_ratio = 1.0
                match_type = "exact"
            else:
                ratio = difflib.SequenceMatcher(None, inv_no, reb_no).ratio()
                no_ratio = ratio
                if ratio >= 0.85:
                    no_score = 0.8          # 高度相似（OCR/笔误），待人工确认而非冲突
                    notes.append(
                        f"发票号不匹配（报销单 {reb_no} vs 发票 {inv_no}，相似度 {ratio:.0%}）"
                    )
                else:
                    no_score = 0.0
                    diff_fields.append(f"发票号: 报销单 {reb_no} vs 发票 {inv_no}")
                    conflict_fields.append("invoice_no")  # 两边均有号但显著不同 -> 字段冲突
                    notes.append(f"发票号不匹配（报销单 {reb_no} vs 发票 {inv_no}）")
        # 复合维度：报销单号 == 发票号 且 代码一致（composite）
        if inv_no and reb_no and inv_no == reb_no and inv.invoice_code:
            match_type = "composite"

        # ---------- 金额维度 ----------
        amount_diff = Decimal("0")
        amount_score = 0.0
        if inv.total_amount is not None and reb.amount is not None:
            amount_diff = abs(inv.total_amount - reb.amount)
            tol = settings.amount_tolerance
            if amount_diff <= tol:
                amount_score = 1.0
            else:
                rel = amount_diff / abs(reb.amount) if reb.amount else Decimal("1")
                if rel <= Decimal("0.01"):
                    amount_score = 0.7
                elif rel <= FUZZY_AMOUNT_REL and amount_diff <= FUZZY_AMOUNT_ABS * 100:
                    amount_score = 0.4
                diff_fields.append(
                    f"金额: 报销单 {reb.amount} vs 发票价税合计 {inv.total_amount}（差 {amount_diff}）"
                )
                notes.append(
                    f"金额不匹配（报销单 {reb.amount} vs 发票价税合计 "
                    f"{inv.total_amount}，差 {amount_diff}）"
                )
                # 发票号精确一致但金额差异显著 -> 字段冲突
                if no_score == 1.0 and amount_diff > max(FUZZY_AMOUNT_ABS, abs(reb.amount) * FUZZY_AMOUNT_REL):
                    conflict_fields.append("amount")

        # ---------- 日期维度 ----------
        date_score = 0.0
        start, end = reb.period_range
        if inv.invoice_date and (start or end):
            d = _parse_d(inv.invoice_date)
            s, e = _parse_d(start), _parse_d(end)
            if d and s and e and s <= d <= e:
                date_score = 1.0
            elif d and s and e and (abs((d - s).days) <= 7 or abs((d - e).days) <= 7):
                date_score = 0.5
            else:
                diff_fields.append(f"日期: 开票 {inv.invoice_date} 不在报销期间 [{start or ''} ~ {end or ''}]")
                notes.append(
                    f"日期不匹配（开票 {inv.invoice_date} 不在报销期间 "
                    f"[{start or ''} ~ {end or ''}]）"
                )
        elif inv.invoice_date and reb.reimburse_date and not (start or end):
            # 无期间信息时以报销日期为基准（±7 天容差）
            gap = abs((_parse_d(inv.invoice_date) or date.min) - (_parse_d(reb.reimburse_date) or date.min)).days
            date_score = 1.0 if gap == 0 else (0.5 if gap <= 7 else 0.0)
            if date_score == 0.0:
                notes.append(
                    f"日期不匹配（开票 {inv.invoice_date} vs 报销日期 "
                    f"{reb.reimburse_date}，相差 {gap} 天）"
                )
        else:
            date_score = 0.5  # 报销单无日期信息 -> 中性

        # ---------- 抬头维度 ----------
        if company_tax_nos:
            title_score = 1.0 if inv.buyer_tax_no in company_tax_nos else 0.0
        else:
            title_score = 1.0  # 未配置公司抬头 -> 中性，不惩罚

        score = min(1.0, round(W_AMOUNT * amount_score + W_NO * no_score
                               + W_DATE * date_score + W_TITLE * title_score, 4))
        return PairScore(score=score, match_type=match_type,
                         conflict_fields=conflict_fields, diff_fields=diff_fields,
                         amount_diff=amount_diff, field_notes=notes, no_ratio=no_ratio)

    # ------------------------------------------------------------------
    def decide_status(self, ps: PairScore) -> str:
        """由评分结果判定匹配状态（设计 §3.2）。"""
        if ps.conflict_fields:
            return "CONFLICT"
        if ps.score >= settings.auto_match_threshold:
            return "MATCHED"
        if ps.score >= settings.suspicious_threshold:
            return "SUSPICIOUS"
        return "UNMATCHED"

    # ------------------------------------------------------------------
    def find_many_to_one(self, invoices: Sequence[InvoiceDTO],
                         reb: ReimburseDTO) -> Optional[List[InvoiceDTO]]:
        """汇总匹配（多对一）：多张发票价税合计 == 报销单金额（±0.01）。

        组合搜索上限由 settings 控制，避免组合爆炸；命中即返回。
        """
        target = reb.amount
        if target is None or target <= 0:
            return None
        tol = settings.amount_tolerance

        start, end = reb.period_range
        pool = []
        for inv in invoices:
            if inv.total_amount is None or inv.total_amount <= 0 or inv.total_amount >= target:
                continue
            if (start or end) and inv.invoice_date:
                d = _parse_d(inv.invoice_date)
                s, e = _parse_d(start), _parse_d(end)
                if d and s and e and not (s <= d <= e):
                    continue
            pool.append(inv)
        pool.sort(key=lambda x: x.total_amount or 0, reverse=True)
        pool = pool[: settings.many_to_one_pool_size]

        max_n = settings.many_to_one_max_invoices
        for n in range(2, max_n + 1):
            for combo in itertools.combinations(pool, n):
                total = sum((c.total_amount for c in combo), Decimal("0"))
                if abs(total - target) <= tol:
                    return list(combo)
        return None
