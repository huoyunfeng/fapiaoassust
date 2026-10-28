"""匹配引擎（设计 §3.2 匹配状态机 + §5.3 match_result）。

评分维度（基础权重，按「有效维度」动态归一化）：
    金额 0.50 + 发票号 0.30 + 日期 0.10 + 文本/抬头 0.10

四条核心规则：
1. 失效维度剔除：某维度双方都没有信息时不进分母，剩余维度按权重比例重新分配，
   杜绝「缺数据反而拿中性分」造成的权重虚高。
2. 号码不一致不再直接淘汰（裁决2）：继续计算金额/日期，若金额完全一致且日期相近
   则判定「疑似发票号填错」推入 SUSPICIOUS 待人工核实；否则才淘汰。
3. 安全锁封顶（最核心）：发票号或日期任一缺失，总分强制封顶 0.94，
   永远不可能达到自动通过阈值 0.95，必须落入 SUSPICIOUS 待人工池。
4. 低置信度兜底（裁决4）：无发票号且金额误差 <= 1.00 元（且无日期全错/抬头全错
   等明确反证）的配对，即使总分不足阈值，也要捞回 SUSPICIOUS 供人工核查。

典型分值（安全锁生效前后）：
  号一致 + 金额一致 + 日期命中 + 文本命中        -> 1.00 自动通过
  号一致 + 金额一致 + 日期缺失                   -> 封顶 0.94 待人工
  无号 + 金额一致 + 日期命中                     -> 0.50/0.90 ≈ 0.56~0.94 待人工
  仅金额相同（无号、无日期）                     -> 0.50 低置信度推荐
  号不一致 + 金额一致 + 日期相近                 -> 0.94 疑似发票号填错
  号不一致 + 金额不同                            -> CONFLICT 淘汰
"""
import difflib
import itertools
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from app.config import settings
from app.domain.dto import InvoiceDTO, ReimburseDTO

# 基础权重（动态归一化前）
W_AMOUNT = 0.50
W_NO = 0.30
W_DATE = 0.10
W_TEXT = 0.10

# 安全锁：缺发票号或缺日期时，总分天花板
SAFE_CAP = 0.94

# 金额分档（绝对误差，单位元）
AMOUNT_EXACT = Decimal("0.01")     # <= 0.01        -> 1.0
AMOUNT_NEAR = Decimal("1.00")      # 0.02 ~ 1.00    -> 0.5；> 1.00 -> 0

# 文本相似度分档
TEXT_HIGH = 0.80                   # >= 0.80 -> 1.0
TEXT_MID = 0.40                    # 0.40 ~ 0.80 -> 0.5；< 0.40 -> 0

# 日期边界容差（天）
DATE_NEAR_DAYS = 7


@dataclass
class PairScore:
    """一次（发票, 报销单）配对的评分结果。"""
    score: float
    match_type: str                       # exact / composite / fuzzy / conflict
    conflict_fields: List[str] = field(default_factory=list)
    diff_fields: List[str] = field(default_factory=list)
    amount_diff: Decimal = Decimal("0")
    field_notes: List[str] = field(default_factory=list)   # 不一致项（写报销单备注/前端提示）
    no_ratio: float = 0.0                     # 发票号字符相似度（仅展示用）
    applicable: Dict[str, bool] = field(default_factory=dict)   # 各维度是否参与归一化
    raw_scores: Dict[str, float] = field(default_factory=dict)  # 各维度原始得分
    capped: bool = False                      # 是否被安全锁封顶
    cap_reason: str = ""                      # 封顶原因（发票号缺失 / 日期缺失 / 疑似填错）
    amount_error: Decimal = Decimal("0")      # 金额绝对误差（平局排序第一关键字）
    suspected_no_error: bool = False          # 号不一致，但金额一致且日期相近 -> 疑似号填错

    @property
    def is_conflict(self) -> bool:
        """存在号码冲突（用于展示/解释）。"""
        return bool(self.conflict_fields)

    @property
    def is_rejected(self) -> bool:
        """候选淘汰：号码冲突且不满足「疑似号填错」条件（裁决2）。"""
        return bool(self.conflict_fields) and not self.suspected_no_error

    @property
    def is_low_confidence(self) -> bool:
        """低置信度兜底（裁决4）：无发票号 + 金额误差 <= 1.00 元，捞回待人工池。

        只针对「无号 + 金额相近」这一种情况；日期完全不在期间内、购方抬头不属于
        本公司属于明确反证，仍按原规则淘汰，不入池。
        """
        if self.applicable.get("no"):
            return False                          # 双方都有号：走号码逻辑
        if not self.applicable.get("amount"):
            return False                          # 没有金额可比：谈不上金额兜底
        if self.amount_error > AMOUNT_NEAR:
            return False
        if self.applicable.get("date") and self.raw_scores.get("date", 0.0) == 0.0:
            return False                          # 日期全错 -> 淘汰
        if any("抬头" in n for n in self.field_notes):
            return False                          # 抬头全错 -> 淘汰
        return True


def _parse_d(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _norm_text(s: Optional[str]) -> str:
    """文本标准化：去空白、转小写，便于相似度比较。"""
    return "".join(str(s or "").split()).lower()


def text_similarity(a: Optional[str], b: Optional[str]) -> float:
    """两段文本的相似度（0~1）。任一方为空返回 0。"""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


class MatchEngine:
    """纯函数匹配评分引擎。"""

    # ------------------------------------------------------------------
    def score_pair(self, inv: InvoiceDTO, reb: ReimburseDTO,
                   company_tax_nos: Optional[set] = None) -> PairScore:
        """对一对（发票, 报销单）打分。维度权重按有效维度动态归一化。"""
        conflict_fields: List[str] = []
        diff_fields: List[str] = []
        notes: List[str] = []
        applicable: Dict[str, bool] = {}
        raw: Dict[str, float] = {}
        match_type = "fuzzy"

        # ---------- 维度 1：发票号（CONFLICT 绝对优先）----------
        inv_no = (inv.invoice_no or "").strip()
        reb_no = (reb.invoice_no or "").strip()
        no_ratio = 0.0
        if inv_no and reb_no:
            applicable["no"] = True
            if inv_no == reb_no:
                raw["no"] = 1.0
                no_ratio = 1.0
                # 报销单号与发票号一致且发票带代码 -> 组合匹配
                match_type = "composite" if (inv.invoice_code or "").strip() else "exact"
            else:
                # 皆有号但不一致：不能仅凭号码就淘汰（裁决2）。
                # 继续计算金额/日期等维度，末尾再判定「疑似号填错」还是淘汰。
                no_ratio = difflib.SequenceMatcher(None, inv_no, reb_no).ratio()
                conflict_fields.append("invoice_no")
                diff_fields.append(f"发票号: 报销单 {reb_no} vs 发票 {inv_no}")
                raw["no"] = 0.0
                match_type = "fuzzy"
        else:
            applicable["no"] = False

        # ---------- 维度 2：金额（绝对误差分档）----------
        amount_error = Decimal("0")
        if inv.total_amount is not None and reb.amount is not None:
            applicable["amount"] = True
            amount_error = abs(inv.total_amount - reb.amount)
            if amount_error <= AMOUNT_EXACT:
                raw["amount"] = 1.0
            elif amount_error <= AMOUNT_NEAR:
                raw["amount"] = 0.5
            else:
                raw["amount"] = 0.0
            if raw["amount"] < 1.0:
                diff_fields.append(
                    f"金额: 报销单 {reb.amount} vs 发票价税合计 {inv.total_amount}"
                    f"（差 {amount_error}）")
                notes.append(
                    f"金额不匹配（报销单 {reb.amount} vs 发票价税合计 "
                    f"{inv.total_amount}，差 {amount_error}）")
        else:
            applicable["amount"] = False

        # ---------- 维度 3：日期（期间内 / 边界±7天 / 超出）----------
        start, end = reb.period_range
        start, end = (str(start)[:10] if start else None), (str(end)[:10] if end else None)
        if inv.invoice_date and (start or end):
            applicable["date"] = True
            d, s, e = _parse_d(inv.invoice_date), _parse_d(start), _parse_d(end)
            if d and s and e:
                if s <= d <= e:
                    raw["date"] = 1.0
                elif abs((d - s).days) <= DATE_NEAR_DAYS or abs((d - e).days) <= DATE_NEAR_DAYS:
                    raw["date"] = 0.5
                else:
                    raw["date"] = 0.0
            elif d and s:            # 只有起始边界
                raw["date"] = 1.0 if d >= s else (0.5 if abs((d - s).days) <= DATE_NEAR_DAYS else 0.0)
            elif d and e:            # 只有结束边界
                raw["date"] = 1.0 if d <= e else (0.5 if abs((d - e).days) <= DATE_NEAR_DAYS else 0.0)
            else:
                applicable["date"] = False
            if applicable.get("date") and raw.get("date", 0.0) == 0.0:
                diff_fields.append(
                    f"日期: 开票 {inv.invoice_date} 不在报销期间 [{start or ''} ~ {end or ''}]")
                notes.append(
                    f"日期不匹配（开票 {inv.invoice_date} 不在报销期间 "
                    f"[{start or ''} ~ {end or ''}]）")
        else:
            # 任一方无日期 -> 剔除该维度（不扣分也不给中性分）
            applicable["date"] = False

        # ---------- 维度 4：文本 / 抬头 ----------
        inv_text = f"{inv.seller_name or ''} {inv.item_summary or ''}"
        reb_text = reb.remark or ""
        text_app = bool(_norm_text(reb_text) and _norm_text(inv_text))
        title_app = bool(company_tax_nos)
        if text_app or title_app:
            applicable["text"] = True
            # 抬头不符 -> 负向否决（0 分）
            if title_app and (inv.buyer_tax_no or "") not in company_tax_nos:
                raw["text"] = 0.0
                notes.append("购方抬头不属于本公司")
            elif text_app:
                ratio = text_similarity(reb_text, inv_text)
                if ratio >= TEXT_HIGH:
                    raw["text"] = 1.0
                elif ratio >= TEXT_MID:
                    raw["text"] = 0.5
                else:
                    raw["text"] = 0.0
            else:
                # 仅抬头可用且命中
                raw["text"] = 1.0
        else:
            applicable["text"] = False

        # ---------- 动态归一化 ----------
        weights = {"amount": W_AMOUNT, "no": W_NO, "date": W_DATE, "text": W_TEXT}
        active = [k for k in weights if applicable.get(k)]
        total_w = sum(weights[k] for k in active)
        if total_w > 0:
            score = sum(weights[k] * raw.get(k, 0.0) for k in active) / total_w
        else:
            score = 0.0

        # ---------- 裁决2：号码不一致的两种走向 ----------
        suspected_no_error = False
        if conflict_fields:
            # 日期维度失效（双方都没日期）不算反证；只有「日期明确不在期间」才否决
            date_ok = (not applicable.get("date")) or raw.get("date", 0.0) >= 0.5
            if applicable.get("amount") and amount_error <= AMOUNT_EXACT and date_ok:
                # 金额完全一致 + 日期相近 -> 疑似发票号填错，推入待人工核实
                suspected_no_error = True
                diff_fields.append("发票号疑似填错")
                notes.append(
                    f"⚠️ 疑似发票号填错（报销单 {reb_no} vs 发票 {inv_no}，"
                    f"但金额一致且日期相近，请核实修正）")
            else:
                notes.append(f"发票号不一致（报销单 {reb_no} vs 发票 {inv_no}），候选淘汰")

        # ---------- 安全锁：无号 / 缺日期 / 疑似号填错 一律封顶 ----------
        missing = []
        if not applicable.get("no"):
            missing.append("发票号")
        if not applicable.get("date"):
            missing.append("日期")
        capped, cap_reason = False, ""
        if suspected_no_error:
            capped = True
            cap_reason = "发票号疑似填错"
            score = min(score, SAFE_CAP)
        elif missing:
            capped = True
            cap_reason = "、".join(missing) + "缺失"
            score = min(score, SAFE_CAP)

        score = min(1.0, round(score, 4))
        return PairScore(
            score=score, match_type=match_type,
            conflict_fields=conflict_fields, diff_fields=diff_fields,
            amount_diff=amount_error, field_notes=notes, no_ratio=no_ratio,
            applicable=applicable, raw_scores={k: round(v, 4) for k, v in raw.items()},
            capped=capped, cap_reason=cap_reason, amount_error=amount_error,
            suspected_no_error=suspected_no_error,
        )

    # ------------------------------------------------------------------
    def decide_status(self, ps: PairScore) -> str:
        """由评分结果判定匹配状态。

        安全锁优先于阈值：被封顶的配对即使因归一化算到高分，也绝不能自动通过。
        """
        if ps.is_rejected:
            return "CONFLICT"
        if ps.suspected_no_error:
            return "SUSPICIOUS"          # 疑似号填错：必须人工核实
        if ps.is_low_confidence:
            return "SUSPICIOUS"          # 低置信度兜底：分数不足也要交人工
        if ps.capped:
            # 封顶 0.94 必然 < 0.95，此处显式短路，避免阈值调整后出现漏洞
            return "SUSPICIOUS" if ps.score >= settings.suspicious_threshold else "UNMATCHED"
        if ps.score >= settings.auto_match_threshold:
            return "MATCHED"
        if ps.score >= settings.suspicious_threshold:
            return "SUSPICIOUS"
        return "UNMATCHED"

    # ------------------------------------------------------------------
    def find_many_to_one(self, invoices: Sequence[InvoiceDTO], reb: ReimburseDTO,
                         max_invoices: int = None) -> Optional[List[InvoiceDTO]]:
        """汇总匹配（多对一）：多张发票价税合计 == 报销单金额（±0.01）。

        发票必须落在报销期间内；组合张数 2~max_invoices；
        池按金额降序截断，避免组合爆炸。
        """
        target = reb.amount
        if target is None or target <= 0:
            return None
        tol = settings.amount_tolerance
        max_n = max_invoices or settings.many_to_one_max_invoices

        start, end = reb.period_range
        s, e = _parse_d(start), _parse_d(end)
        pool = []
        for inv in invoices:
            if inv.total_amount is None or inv.total_amount <= 0 or inv.total_amount >= target:
                continue
            if (s or e) and not inv.invoice_date:
                continue                      # 期间存在但发票无日期 -> 不参与
            if s or e:
                d = _parse_d(inv.invoice_date)
                if d and not ((s and d >= s) if not e else (e and d <= e) if not s else (s <= d <= e)):
                    continue
            pool.append(inv)
        pool.sort(key=lambda x: x.total_amount or 0, reverse=True)
        pool = pool[: settings.many_to_one_pool_size]

        for n in range(2, max_n + 1):
            for combo in itertools.combinations(pool, n):
                total = sum((c.total_amount for c in combo), Decimal("0"))
                if abs(total - target) <= tol:
                    return list(combo)
        return None
