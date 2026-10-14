"""规则引擎：可配置业务规则（rule 表），与内置校验（InvoiceValidator）叠加。

规则按 priority 升序执行（数值越小越先执行），stop_on_fail=1 时失败即短路。
支持类型：buyer_tax_no / period / amount / dedup / custom。
"""
from typing import List

from app.domain.dto import InvoiceDTO, Issue, RuleDTO


class RuleEngine:
    """纯函数：DTO + 规则列表 -> [Issue]。"""

    def evaluate(self, dto: InvoiceDTO, rules: List[RuleDTO]) -> List[Issue]:
        issues: List[Issue] = []
        for rule in sorted((r for r in rules if r.enabled), key=lambda r: r.priority):
            failed = False
            try:
                failed = self._apply(rule, dto, issues)
            except Exception as e:  # 规则配置错误不应中断校验流程
                issues.append(Issue("RULE_ERROR", f"规则 [{rule.name}] 执行异常: {e}", "WARN"))
                failed = False
            if failed and rule.stop_on_fail:
                break
        return issues

    def _apply(self, rule: RuleDTO, dto: InvoiceDTO, issues: List[Issue]) -> bool:
        cfg = rule.config or {}
        rt = rule.rule_type

        if rt == "buyer_tax_no":
            tax_nos = {str(t).strip().upper() for t in cfg.get("tax_nos", []) if str(t).strip()}
            if tax_nos and dto.buyer_tax_no and dto.buyer_tax_no not in tax_nos:
                issues.append(Issue("BUYER_TAX_NO_NOT_ALLOWED",
                                    f"购买方税号 {dto.buyer_tax_no} 不在允许清单内（规则：{rule.name}）",
                                    "ERROR"))
                return True
            return False

        if rt == "period":
            start = cfg.get("start") or None
            end = cfg.get("end") or None
            if dto.invoice_date and (start or end):
                if start and dto.invoice_date < start:
                    issues.append(Issue("PERIOD_BEFORE",
                                        f"开票日期 {dto.invoice_date} 早于允许期间 {start}（规则：{rule.name}）",
                                        "ERROR"))
                    return True
                if end and dto.invoice_date > end:
                    issues.append(Issue("PERIOD_AFTER",
                                        f"开票日期 {dto.invoice_date} 晚于允许期间 {end}（规则：{rule.name}）",
                                        "ERROR"))
                    return True
            return False

        if rt == "amount":
            lo = cfg.get("min")
            hi = cfg.get("max")
            if dto.total_amount is None:
                return False
            if lo is not None and dto.total_amount < _dec(lo):
                issues.append(Issue("AMOUNT_BELOW_MIN",
                                    f"价税合计 {dto.total_amount} 低于下限 {lo}（规则：{rule.name}）",
                                    "ERROR"))
                return True
            if hi is not None and dto.total_amount > _dec(hi):
                issues.append(Issue("AMOUNT_ABOVE_MAX",
                                    f"价税合计 {dto.total_amount} 超过上限 {hi}（规则：{rule.name}）",
                                    "ERROR"))
                return True
            return False

        if rt == "dedup":
            if cfg.get("disallow_red_flush") and dto.is_red_flush:
                issues.append(Issue("RED_FLUSH_NOT_ALLOWED",
                                    f"不允许红冲发票（规则：{rule.name}）", "ERROR"))
                return True
            return False

        # custom：预留类型，暂不产生问题
        return False


def _dec(v):
    from decimal import Decimal
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")
