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
from app.domain.features import (InvoiceFeatures, ReimburseFeatures, feature_match,
                                 invoice_features, reimburse_features)

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
    match_points: List[str] = field(default_factory=list)  # ✅/✗ 命中点（前端并排比对展示）
    # ---- 唯一键短路匹配（第一阶段）----
    auto_matchable: bool = False              # 唯一键命中：发票号精确匹配 + 金额在容差内 -> 跳过安全锁直接 MATCHED
    auto_match_reason: str = ""               # 唯一键命中说明（展示用）

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
        """低置信度兜底（裁决4 + 修复）：

        两种情况强制捞回 SUSPICIOUS：
        a) 无发票号 + 金额误差 <= 1.00 元（原裁决4）
        b) 有号且号一致 + 金额误差 <= 1.00 元（实际最常见的"号对得上但金额微差"场景）

        日期明确不在期间内、抬头不属于本公司属于明确反证，仍按原规则淘汰。
        """
        if not self.applicable.get("amount"):
            return False
        if self.amount_error > AMOUNT_NEAR:
            return False
        # 明确反证否决
        if self.applicable.get("date") and self.raw_scores.get("date", 0.0) == 0.0:
            return False
        if any("抬头" in n for n in self.field_notes):
            return False
        no_app = self.applicable.get("no")
        if not no_app:
            # 情况 a：无号 + 金额相近
            return True
        # 情况 b：有号且号一致 + 金额相近（原来漏掉的最常见场景）
        if self.raw_scores.get("no", 0.0) >= 0.9 and self.amount_error > AMOUNT_EXACT:
            return True
        return False


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

        # ================================================================
        # 第一阶段：唯一键短路匹配（一票通过，跳过后续所有打分）
        # 设计原则：发票号是发票的唯一标识，对上了就是同一张票。
        #   - 完美命中（金额≤0.01）→ 直接 MATCHED，跳过安全锁、日期、文本等所有后续逻辑
        #   - 金额微差（0.02~1.00 元）→ 大概率浮点/四舍五入问题，自动通过并加备注提示
        #   - 金额大差（>1.00 元）→ 唯一键对不上，说明报销单填了假号，返回 CONFLICT
        # ================================================================
        inv_no = (inv.invoice_no or "").strip()
        reb_no = (reb.invoice_no or "").strip()

        # ================================================================
        # 无票号专用：业务特征碰撞（金额 0.4 / 日期 0.3 / 文本 0.3）
        # 绝对不使用自增 ID 或 Excel 序号——只认金额、日期、事由、起止地点、
        # 车牌号/票面文字。双方都没有发票号时才走此分支；有号仍走唯一键短路。
        # ================================================================
        if not inv_no and not reb_no:
            rfeat = reimburse_features(reb)
            ifeat = invoice_features(inv)
            if rfeat.has_text or ifeat.has_text:
                return self._score_feature_pair(inv, reb, rfeat, ifeat)

        if inv_no and reb_no and inv_no == reb_no:
            # 唯一键精确命中 → 三段判断
            if inv.total_amount is not None and reb.amount is not None:
                amt_err = abs(inv.total_amount - reb.amount)
                if amt_err <= AMOUNT_EXACT:
                    # ----- 情况 A：完美命中 -----
                    return PairScore(
                        score=1.0, match_type="exact",
                        conflict_fields=[], diff_fields=[],
                        amount_diff=amt_err,
                        field_notes=["唯一键命中：发票号精确匹配 + 金额一致，自动通过"],
                        applicable={"no": True, "amount": True},
                        raw_scores={"no": 1.0, "amount": 1.0},
                        capped=False, cap_reason="",
                        amount_error=amt_err,
                        no_ratio=1.0,
                        auto_matchable=True,
                        auto_match_reason="唯一键命中：发票号精确匹配 + 金额一致",
                    )
                elif amt_err <= AMOUNT_NEAR:
                    # ----- 情况 B：金额微差（≤1 元）-----
                    # 微差大概率是 Excel/导入时的浮点精度问题，唯一键已锁定，自动通过但加备注
                    return PairScore(
                        score=1.0, match_type="exact",
                        conflict_fields=[],
                        diff_fields=[
                            f"金额微差：报销单 {reb.amount} vs 发票 {inv.total_amount}（差 {amt_err} 元）"
                        ],
                        amount_diff=amt_err,
                        field_notes=[
                            f"唯一键命中但金额微差 {amt_err} 元（≤1 元容差），已自动通过请留意"
                        ],
                        applicable={"no": True, "amount": True},
                        raw_scores={"no": 1.0, "amount": 0.5},
                        capped=False, cap_reason="",
                        amount_error=amt_err,
                        no_ratio=1.0,
                        auto_matchable=True,
                        auto_match_reason=f"唯一键命中：发票号精确匹配 + 金额微差 {amt_err} 元（≤1 元容差）",
                    )
                else:
                    # ----- 情况 C：金额大差（>1 元）-----
                    # 唯一键对不上：报销单填了一个真实存在但不属于此报销的发票号
                    return PairScore(
                        score=0.0, match_type="exact",
                        conflict_fields=["amount"],
                        diff_fields=[
                            f"金额冲突：报销单 {reb.amount} vs 发票 {inv.total_amount}（差 {amt_err} 元）"
                        ],
                        amount_diff=amt_err,
                        field_notes=[
                            f"⚠️ 发票号一致但金额差距 {amt_err} 元（>1 元容差），"
                            f"可能填错发票号，请核实修正"
                        ],
                        applicable={"no": True, "amount": True},
                        raw_scores={"no": 1.0, "amount": 0.0},
                        capped=False, cap_reason="",
                        amount_error=amt_err,
                        no_ratio=1.0,
                        auto_matchable=False,
                    )
            else:
                # 号一致但一方缺金额信息 → 不短路，进入第二阶段多维打分（后续金额维度会不适用）
                pass

        # ================================================================
        # 第二阶段：多维特征打分兜底（仅当唯一键未命中或命中但无法判定金额时启用）
        # ================================================================
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

        # ---------- 维度 3：日期 ----------
        # 优先用报销单期间检查；报销单缺期间时降级用「报销日期 vs 发票日期 ±90 天」做近似检查
        start, end = reb.period_range
        start, end = (str(start)[:10] if start else None), (str(end)[:10] if end else None)
        inv_d = _parse_d(inv.invoice_date)
        reb_d = _parse_d(getattr(reb, "reimburse_date", None))

        if inv_d and (start or end):
            # 完整期间检查
            applicable["date"] = True
            s, e = _parse_d(start), _parse_d(end)
            if s and e:
                if s <= inv_d <= e: raw["date"] = 1.0
                elif abs((inv_d - s).days) <= DATE_NEAR_DAYS or abs((inv_d - e).days) <= DATE_NEAR_DAYS: raw["date"] = 0.5
                else: raw["date"] = 0.0
            elif s: raw["date"] = 1.0 if inv_d >= s else (0.5 if abs((inv_d - s).days) <= DATE_NEAR_DAYS else 0.0)
            elif e: raw["date"] = 1.0 if inv_d <= e else (0.5 if abs((inv_d - e).days) <= DATE_NEAR_DAYS else 0.0)
            if raw.get("date", 0.0) == 0.0:
                diff_fields.append(f"日期: 开票 {inv.invoice_date} 不在报销期间 [{start or ''} ~ {end or ''}]")
                notes.append(f"日期不匹配（开票 {inv.invoice_date} 不在报销期间 [{start or ''} ~ {end or ''}]）")

        elif inv_d and reb_d:
            # 报销单缺期间，但有报销日期 → 近似检查（±90 天给 0.5 分，±30 天给 1.0 分）
            applicable["date"] = True
            gap = abs((inv_d - reb_d).days)
            if gap <= 30: raw["date"] = 1.0
            elif gap <= 90: raw["date"] = 0.5
            else: raw["date"] = 0.0
            if raw["date"] == 0.0:
                diff_fields.append(f"日期: 开票 {inv.invoice_date} 与报销日期 {reb.reimburse_date} 相差 {gap} 天")
                notes.append(f"开票日期 {inv.invoice_date} 与报销日期 {reb.reimburse_date} 相差 {gap} 天，超出合理范围")

        elif inv_d and not (start or end):
            # 发票有日期但报销单完全没有任何日期信息 → 仍标记日期维度不适用（安全锁仅在双方都无日期时封顶）
            applicable["date"] = False
            notes.append("报销单未填写日期信息，日期维度不参与打分（发票开票日期可作参考）")
        else:
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

        # ---------- 安全锁：仅当"发票号缺失"或"双方都没日期"或"疑似号填错"才封顶 ----------
        # 说明：以前是报销单缺期间就封顶，但报销单没填期间≠发票无日期——发票开票日是真实可信的，
        # 不应因此完全废掉日期维度。只有双方都没有日期信息时才真正"无法校验时序"。
        missing = []
        if not applicable.get("no"):
            missing.append("发票号")
        # 双方都没有日期信息才视为"日期缺失"；发票有开票日或报销有报销日期都算有参考
        inv_has_date = bool(inv.invoice_date)
        reb_has_date = bool((reb.period_range[0] or reb.period_range[1] or getattr(reb, "reimburse_date", None)))
        if not inv_has_date and not reb_has_date:
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
    def _score_feature_pair(self, inv: InvoiceDTO, reb: ReimburseDTO,
                            rfeat: ReimburseFeatures,
                            ifeat: InvoiceFeatures) -> PairScore:
        """无票号专用打分：金额 0.4（硬门槛）+ 日期 0.3 + 文本特征 0.3。

        业务红线：
        - 金额必须绝对一致（±0.01），不一致直接 0 分淘汰；
        - 日期同日满分、相差 1~2 天半分、超过 2 天 0 分；
        - 文本用「事由/起止地点/车牌号」与「销方+票面文字」做模糊碰撞；
        - 因为无发票号，总分强制封顶 0.94，永远只能进人工确认池。
        """
        points: List[str] = []
        notes: List[str] = []
        diff_fields: List[str] = []
        applicable: Dict[str, bool] = {}
        raw: Dict[str, float] = {}

        # ---------- 金额：绝对一致硬门槛 ----------
        amount_error = Decimal("0")
        if inv.total_amount is not None and reb.amount is not None:
            amount_error = abs(inv.total_amount - reb.amount)
            if amount_error > AMOUNT_EXACT:
                # 不一致直接 0 分：不做任何其它维度的"补救"
                return PairScore(
                    score=0.0, match_type="fuzzy",
                    conflict_fields=["amount"],
                    diff_fields=[f"金额: 报销单 {reb.amount} vs 发票 {inv.total_amount}"
                                 f"（差 {amount_error}）"],
                    field_notes=[f"金额不一致（无发票号场景要求金额完全一致，差 {amount_error} 元）"],
                    match_points=[f"✗ 金额不一致（差 {amount_error} 元）"],
                    applicable={"amount": True}, raw_scores={"amount": 0.0},
                    amount_error=amount_error,
                    capped=True, cap_reason="无发票号：需人工确认",
                )
            applicable["amount"] = True
            raw["amount"] = 1.0
            points.append(f"✅ 金额一致（¥{inv.total_amount:.2f}）")

        # ---------- 日期：同日满分；相差 1~2 天半分 ----------
        inv_d = _parse_d(inv.invoice_date) or _parse_d(inv.ride_time)
        reb_d = (_parse_d(reb.reimburse_date) or _parse_d(reb.period_start)
                 or _parse_d(reb.period_end))
        if inv_d and reb_d:
            gap = abs((inv_d - reb_d).days)
            applicable["date"] = True
            if gap == 0:
                raw["date"] = 1.0
                points.append(f"✅ 日期一致（{reb_d.isoformat()}）")
            elif gap <= 2:
                raw["date"] = 0.5
                points.append(f"✅ 日期相差 {gap} 天（容差内半匹配）")
            else:
                raw["date"] = 0.0
                points.append(f"✗ 日期相差 {gap} 天（超出 2 天容差）")
                diff_fields.append(
                    f"日期: 报销 {reb_d.isoformat()} vs 发票 {inv_d.isoformat()}（相差 {gap} 天）")
                notes.append(f"日期相差 {gap} 天，超出无票号场景 2 天容差")

        # ---------- 文本特征：事由/起止地点 vs 销方+票面文字 ----------
        if rfeat.has_text:
            applicable["text"] = True
            if rfeat.origin or rfeat.destination or rfeat.plate_no:
                # 有起止地点/车牌：按词条碰撞（「盈创动力E座」命中「盈创」得高分）
                text_score, hits = feature_match(rfeat, ifeat)
                for h in hits:
                    if h.startswith("车牌号"):
                        points.append(f"✅ {h}")
                    else:
                        points.append(f"✅ 文本包含“{h}”")
            else:
                # 只有「事由」时沿用整体相似度分档，避免短词包含导致权重虚高
                ratio = text_similarity("".join(rfeat.phrases), ifeat.text)
                text_score = (1.0 if ratio >= TEXT_HIGH
                              else 0.5 if ratio >= TEXT_MID else 0.0)
                if text_score > 0:
                    points.append(f"✅ 事由文本相似（{ratio:.0%}）")
            raw["text"] = text_score
            if text_score <= 0:
                points.append("✗ 票面文字未命中事由/起止地点")
                diff_fields.append("文本: 事由/起止地点未在票面文字中命中")
                notes.append("票面文字未命中报销单的事由/起止地点，请人工核对")

        # ---------- 动态归一化（缺维度剔除，避免缺数据反而占权重）----------
        weights = {"amount": 0.4, "date": 0.3, "text": 0.3}
        active = [k for k in weights if applicable.get(k)]
        total_w = sum(weights[k] for k in active)
        score = (sum(weights[k] * raw.get(k, 0.0) for k in active) / total_w
                 if total_w > 0 else 0.0)
        score = min(SAFE_CAP, round(score, 4))   # 无号 -> 永不可自动通过

        return PairScore(
            score=score, match_type="fuzzy",
            conflict_fields=[], diff_fields=diff_fields,
            amount_diff=amount_error,
            field_notes=notes, match_points=points,
            applicable=applicable,
            raw_scores={k: round(v, 4) for k, v in raw.items()},
            amount_error=amount_error,
            capped=True, cap_reason="无发票号：需人工确认（业务特征碰撞）",
        )

    # ------------------------------------------------------------------
    def decide_status(self, ps: PairScore) -> str:
        """由评分结果判定匹配状态。

        判定优先级（从高到低）：
        1. 唯一键短路命中 → 直接 MATCHED（跳过安全锁、阈值等所有后续判断）
        2. 唯一键金额冲突 → 直接 CONFLICT（报销单填了假号）
        3. 裁决2疑似号填错 → SUSPICIOUS
        4. 裁决4低置信度兜底 → SUSPICIOUS
        5. 安全锁封顶 → SUSPICIOUS（或 UNMATCHED）
        6. 阈值判断 → MATCHED / SUSPICIOUS / UNMATCHED
        """
        # ---------- 优先级 1：唯一键短路命中 ----------
        if ps.auto_matchable:
            return "MATCHED"

        # ---------- 优先级 2：唯一键金额冲突（号对得上但金额差大）----------
        # 这种场景 conflict_fields=["amount"]，属于 is_conflict 但不是 is_rejected
        # 必须显式返回 CONFLICT，不能让它落到后面的阈值判断
        if "amount" in ps.conflict_fields and "invoice_no" not in ps.conflict_fields:
            return "CONFLICT"

        # ---------- 原有裁决链路 ----------
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
