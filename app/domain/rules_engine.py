"""规则引擎：可配置业务规则（rule 表），与内置校验（InvoiceValidator）叠加。

规则按 priority 升序执行（数值越小越先执行），stop_on_fail=1 时失败即短路。
两类作用域：
- invoice   针对单张发票（发票导入校验、匹配时逐张发票检查）
- reimburse 针对单笔报销单（匹配时对报销单检查）

支持类型：buyer_tax_no / seller_blacklist / sensitive_words / period /
amount / cross_period / red_flush；兼容旧数据中的 dedup / custom。
"""
from decimal import Decimal
from typing import List, Optional

from app.domain.dto import InvoiceDTO, Issue, ReimburseDTO, RuleDTO

# 各作用域允许的规则类型（保存时校验，防止配出永不适用的规则）
SCOPE_TYPES = {
    "invoice": {"buyer_tax_no", "seller_blacklist", "sensitive_words",
                "period", "amount", "cross_period", "red_flush", "dedup", "custom"},
    "reimburse": {"sensitive_words", "amount", "period", "custom"},
}

# 标签型规则 -> config 字段名
TAG_CONFIG_KEYS = {
    "buyer_tax_no": "tax_nos",
    "seller_blacklist": "sellers",
    "sensitive_words": "words",
}


def rule_severity(rule: RuleDTO) -> str:
    """规则命中后的严重级别：阻断 -> ERROR；警告 -> WARN。"""
    return "ERROR" if (rule.action or "block") == "block" else "WARN"


def _norm(text: str) -> str:
    """比对用归一化：去空白、转小写（敏感词/销方名称匹配不区分大小写与空格）。"""
    return "".join(str(text or "").split()).lower()


class RuleEngine:
    """纯函数：DTO + 规则列表 -> [Issue]。"""

    def validate(self, dto, rules: List[RuleDTO]) -> List[Issue]:
        """按 DTO 类型自动选取作用域（InvoiceDTO -> invoice，ReimburseDTO -> reimburse）。"""
        scope = "reimburse" if isinstance(dto, ReimburseDTO) else "invoice"
        return self._run(dto, scope, rules)

    def validate_pair(self, invoice: InvoiceDTO, reimburse: Optional[ReimburseDTO],
                      rules: List[RuleDTO]) -> List[Issue]:
        """匹配时的完整检查：发票侧规则 + 报销单侧规则 + 跨期规则。"""
        issues = self._run(invoice, "invoice", rules)
        if reimburse is not None:
            issues.extend(self._run(reimburse, "reimburse", rules))
        issues.extend(self._check_cross_period(invoice, reimburse, rules))
        return issues

    # ------------------------------------------------------------ 内部
    def _run(self, dto, scope: str, rules: List[RuleDTO]) -> List[Issue]:
        issues: List[Issue] = []
        ordered = sorted((r for r in rules
                          if r.enabled and (r.scope or "invoice") == scope),
                         key=lambda r: r.priority)
        for rule in ordered:
            failed = False
            try:
                failed = self._apply(rule, dto, issues)
            except Exception as e:  # 规则配置错误不应中断校验流程
                issues.append(Issue("RULE_ERROR", f"规则 [{rule.name}] 执行异常: {e}", "WARN"))
                failed = False
            if failed and rule.stop_on_fail:
                break
        return issues

    def _apply(self, rule: RuleDTO, dto, issues: List[Issue]) -> bool:
        cfg = rule.config or {}
        rt = rule.rule_type
        sev = rule_severity(rule)

        if rt == "buyer_tax_no":
            if not isinstance(dto, InvoiceDTO):
                return False
            tax_nos = {str(t).strip().upper() for t in cfg.get("tax_nos", []) if str(t).strip()}
            if tax_nos and dto.buyer_tax_no and dto.buyer_tax_no.upper() not in tax_nos:
                issues.append(Issue(
                    "BUYER_TAX_NO_NOT_ALLOWED",
                    f"该【单张发票】购买方税号（{dto.buyer_tax_no}）不在白名单内（规则：{rule.name}）",
                    sev))
                return True
            return False

        if rt == "seller_blacklist":
            if not isinstance(dto, InvoiceDTO):
                return False
            keywords = [str(s).strip() for s in cfg.get("sellers", []) if str(s).strip()]
            if not keywords:
                return False
            seller = _norm(dto.seller_name)
            tax = _norm(dto.seller_tax_no)
            for kw in keywords:
                n = _norm(kw)
                if not n:
                    continue
                if n in seller or n == tax:
                    # 阻断优先：命中销方黑名单时，即便购买方税号在白名单内也强制阻断
                    issues.append(Issue(
                        "SELLER_BLACKLISTED",
                        f"该【单张发票】销方（{dto.seller_name or dto.seller_tax_no}）在黑名单内（规则：{rule.name}）",
                        "ERROR"))
                    return True
            return False

        if rt == "sensitive_words":
            words = [str(w).strip() for w in cfg.get("words", []) if str(w).strip()]
            text, label = _sensitive_text(dto)
            if not words:
                return False
            if not text:
                # 缺失数据防误杀：摘要为空无法比对 -> 只告警，不阻断
                issues.append(Issue(
                    "SENSITIVE_TEXT_MISSING",
                    f"因摘要缺失，敏感词规则未生效（规则：{rule.name}）",
                    "WARN"))
                return False
            normalized = _norm(text)
            for w in words:
                if _norm(w) in normalized:
                    issues.append(Issue(
                        "SENSITIVE_WORD_HIT",
                        f"该【{label}】摘要/内容命中敏感词（{w}）（规则：{rule.name}）",
                        sev))
                    return True
            return False

        if rt == "period":
            date = getattr(dto, "invoice_date" if isinstance(dto, InvoiceDTO) else "reimburse_date", None)
            start = cfg.get("start") or None
            end = cfg.get("end") or None
            if date and (start or end):
                if start and date < start:
                    issues.append(Issue(
                        "PERIOD_BEFORE",
                        f"该【{_target_label(dto)}】日期（{date}）早于允许期间 {start}"
                        f"（规则：{rule.name}）",
                        sev))
                    return True
                if end and date > end:
                    issues.append(Issue(
                        "PERIOD_AFTER",
                        f"该【{_target_label(dto)}】日期（{date}）晚于允许期间 {end}"
                        f"（规则：{rule.name}）",
                        sev))
                    return True
            return False

        if rt == "amount":
            amount = dto.total_amount if isinstance(dto, InvoiceDTO) else dto.amount
            if amount is None:
                return False
            lo = cfg.get("min")
            hi = cfg.get("max")
            if lo not in (None, "") and amount < _dec(lo):
                issues.append(Issue(
                    "AMOUNT_BELOW_MIN",
                    f"该【{_target_label(dto)}】金额（{_fmt(amount)}）低于规则限制（{_fmt(lo)}）",
                    sev))
                return True
            if hi not in (None, "") and amount > _dec(hi):
                issues.append(Issue(
                    "AMOUNT_ABOVE_MAX",
                    f"该【{_target_label(dto)}】价税合计（{_fmt(amount)}）超过规则限制（{_fmt(hi)}）",
                    sev))
                return True
            return False

        if rt == "red_flush" or rt == "dedup":
            if not isinstance(dto, InvoiceDTO):
                return False
            enabled = cfg.get("disallow_red_flush")
            if rt == "red_flush" and enabled is None:
                enabled = True
            if enabled and dto.is_red_flush:
                issues.append(Issue(
                    "RED_FLUSH_NOT_ALLOWED",
                    f"该【单张发票】为红冲（负数）发票，禁止参与报销（规则：{rule.name}）",
                    sev))
                return True
            return False

        # cross_period 需要发票+报销单双方数据，在 validate_pair 中处理
        # custom：预留类型，暂不产生问题
        return False

    def _check_cross_period(self, invoice: InvoiceDTO, reimburse: Optional[ReimburseDTO],
                            rules: List[RuleDTO]) -> List[Issue]:
        if reimburse is None:
            return []
        start, end = reimburse.period_range
        if not invoice.invoice_date or not (start or end):
            return []
        issues: List[Issue] = []
        for rule in sorted((r for r in rules
                            if r.enabled and (r.scope or "invoice") == "invoice"
                            and r.rule_type == "cross_period"),
                           key=lambda r: r.priority):
            if start and invoice.invoice_date < start:
                issues.append(Issue(
                    "CROSS_PERIOD",
                    f"该【单张发票】开票日期（{invoice.invoice_date}）早于报销期间 {start} ~ {end or '未填'}",
                    rule_severity(rule)))
            elif end and invoice.invoice_date > end:
                issues.append(Issue(
                    "CROSS_PERIOD",
                    f"该【单张发票】开票日期（{invoice.invoice_date}）超出报销期间 {start or '未填'} ~ {end}",
                    rule_severity(rule)))
        return issues


def find_conflicts(rules: List[RuleDTO], candidate: RuleDTO) -> List[str]:
    """保存前的逻辑死锁防御：同一作用域内的规则区间互相重叠时拒绝保存。

    典型冲突：同作用域同时存在「金额 ≤ 5000」与「金额 ≤ 10000」两条区间规则，
    命中顺序与判定结果不稳定，视为规则逻辑冲突。
    """
    conflicts: List[str] = []
    if candidate.rule_type != "amount":
        return conflicts
    lo, hi = _range_of(candidate)
    for r in rules:
        if not r.enabled or r.rule_type != "amount":
            continue
        if (r.scope or "invoice") != (candidate.scope or "invoice"):
            continue
        if r.id and r.id == candidate.id:
            continue
        olo, ohi = _range_of(r)
        if _overlap(lo, hi, olo, ohi):
            conflicts.append(
                f"「{r.name}」的金额区间（{_fmt(olo)} ~ {_fmt(ohi)}）与"
                f"「{candidate.name}」的区间（{_fmt(lo)} ~ {_fmt(hi)}）重叠")
    return conflicts


# ---------------------------------------------------------------- 辅助
def _sensitive_text(dto) -> tuple:
    """取用于敏感词比对的文本：发票取项目摘要；报销单取备注/事由/费用类型。"""
    if isinstance(dto, InvoiceDTO):
        return (dto.item_summary or ""), "单张发票"
    parts = [dto.remark, dto.reason, dto.expense_type]
    return "".join(str(p or "") for p in parts), "单笔报销单"


def _target_label(dto) -> str:
    return "单张发票" if isinstance(dto, InvoiceDTO) else "单笔报销单"


def _range_of(rule: RuleDTO) -> tuple:
    cfg = rule.config or {}
    lo = cfg.get("min")
    hi = cfg.get("max")
    return (None if lo in (None, "") else _dec(lo),
            None if hi in (None, "") else _dec(hi))


def _overlap(a_lo, a_hi, b_lo, b_hi) -> bool:
    """两个区间是否重叠（None 表示无界）。"""
    if a_lo is not None and a_hi is not None and b_lo is not None and b_hi is not None:
        return a_lo <= b_hi and b_lo <= a_hi
    if a_hi is not None and b_lo is not None and b_lo > a_hi:
        return False
    if b_hi is not None and a_lo is not None and a_lo > b_hi:
        return False
    return True


def _fmt(v) -> str:
    """金额展示：去掉多余小数位。"""
    if v is None:
        return "不限"
    try:
        d = Decimal(str(v))
    except Exception:
        return str(v)
    s = format(d.normalize(), "f")
    return s


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")
