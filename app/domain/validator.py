"""校验引擎（设计 §6.3）：规则明确，含边界。

C1 必填字段 / C2 价税合计(±0.01) / C3 税号格式(15/18/20 位)
C4 日期合理性(不晚于今天、不早于 10 年前) / C5 金额符号一致性(红冲)
"""
import re
from datetime import date, datetime
from decimal import Decimal
from typing import List

from app.domain.dto import InvoiceDTO, Issue

_TAX_NO_RE = re.compile(r"^[0-9A-Z]{15,20}$")


class InvoiceValidator:
    """发票内置校验规则（纯函数：DTO -> [Issue]）。"""

    def validate(self, dto: InvoiceDTO) -> List[Issue]:
        issues: List[Issue] = []

        # C1: 必填字段（Decimal('0.00') 合法，0 元发票不算缺失）
        for f in ("invoice_no", "invoice_date", "total_amount"):
            v = getattr(dto, f)
            if v is None or (isinstance(v, str) and not v.strip()):
                issues.append(Issue("MISSING_FIELD", f"缺失必填字段: {f}", "ERROR"))

        # C2: 价税合计 = 金额 + 税额（允许 ±0.01，不变量 I5）
        if dto.amount is not None and dto.tax_amount is not None and dto.total_amount is not None:
            expected = dto.amount + dto.tax_amount
            if abs(expected - dto.total_amount) > Decimal("0.01"):
                issues.append(Issue(
                    "AMOUNT_MISMATCH",
                    f"价税合计 ({dto.total_amount}) != 金额 ({dto.amount}) + 税额 ({dto.tax_amount})",
                    "ERROR",
                ))

        # C3: 税号格式（15/18/20 位大写字母数字）
        for label, tax in (("购买方", dto.buyer_tax_no), ("销售方", dto.seller_tax_no)):
            if tax and not _TAX_NO_RE.fullmatch(tax):
                issues.append(Issue("TAX_NO_FORMAT", f"{label}税号格式异常: {tax}", "WARN"))

        # C4: 日期合理性（不晚于今天 + 不早于 10 年前）
        if dto.invoice_date:
            try:
                d = datetime.strptime(dto.invoice_date, "%Y-%m-%d").date()
            except ValueError:
                issues.append(Issue("DATE_FORMAT", f"开票日期格式异常: {dto.invoice_date}", "ERROR"))
            else:
                if d > date.today():
                    issues.append(Issue("FUTURE_DATE", f"开票日期 {d} 晚于今天", "ERROR"))
                if d < date.today().replace(year=date.today().year - 10):
                    issues.append(Issue("TOO_OLD", f"开票日期 {d} 过旧", "WARN"))

        # C5: 金额符号一致性（红冲发票）
        if dto.total_amount is not None:
            if dto.total_amount < 0:
                dto.is_red_flush = True
            if dto.amount is not None and dto.amount * dto.total_amount < 0:
                issues.append(Issue("SIGN_MISMATCH", "金额与价税合计符号不一致", "WARN"))

        return issues
