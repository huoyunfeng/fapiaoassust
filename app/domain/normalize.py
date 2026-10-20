"""标准化引擎（设计 §6.2）：纯函数，无副作用。

- 金额统一为 Decimal，量化到 0.01；
- 日期统一输出 ISO8601 字符串 YYYY-MM-DD；
- 税号去除空格/连字符并转大写。
"""
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.domain.dto import InvoiceDTO

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y%m%d", "%Y.%m.%d")

# Excel 日期序列号基准（1900 日期系统）
_EXCEL_EPOCH = datetime(1899, 12, 30)


def clean(v) -> str:
    """字符串清洗：None -> ""，其余 strip。"""
    if v is None:
        return ""
    return str(v).strip()


def to_decimal(v) -> Optional[Decimal]:
    """金额标准化：去除逗号/货币符号，量化到 0.01。失败返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v.quantize(Decimal("0.01"))
    if isinstance(v, (int, float)):
        return Decimal(str(v)).quantize(Decimal("0.01"))
    s = str(v).replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").strip()
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def parse_date(v) -> Optional[str]:
    """日期标准化：统一输出 ISO8601 字符串 YYYY-MM-DD。

    兼容：datetime 对象、Excel 日期序列号（单元格未被识别为日期时的原始数值）、
    带时间部分的字符串（如 "2026-08-20 00:00:00"）。
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):
        # Excel 序列号：合理业务日期区间 1954~2099 年
        n = float(v)
        if 20000 <= n <= 80000:
            return (_EXCEL_EPOCH + timedelta(days=int(n))).strftime("%Y-%m-%d")
        return None
    s = str(v).strip()
    if s.endswith(" 00:00:00"):
        s = s[:-9]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def clean_tax_no(v) -> str:
    """税号标准化：去空格/连字符，转大写。"""
    if not v:
        return ""
    return re.sub(r"[\s\-—－]", "", str(v)).upper()


class NormalizeEngine:
    """发票 DTO 标准化引擎（纯函数）。"""

    def normalize(self, dto: InvoiceDTO) -> InvoiceDTO:
        return InvoiceDTO(
            invoice_code=self._clean(dto.invoice_code),
            invoice_no=self._clean(dto.invoice_no),
            invoice_date=self._parse_date(dto.invoice_date),
            buyer_name=self._clean(dto.buyer_name),
            buyer_tax_no=self._clean_tax_no(dto.buyer_tax_no),
            seller_name=self._clean(dto.seller_name),
            seller_tax_no=self._clean_tax_no(dto.seller_tax_no),
            amount=self._to_decimal(dto.amount),
            tax_amount=self._to_decimal(dto.tax_amount),
            total_amount=self._to_decimal(dto.total_amount),
            tax_rate=self._clean(dto.tax_rate),
            item_summary=self._clean(dto.item_summary),
            is_red_flush=bool(dto.is_red_flush),
        )

    # 以下为实例方法别名，保持与设计伪代码一致的调用形态
    def _clean(self, v) -> str:
        return clean(v)

    def _to_decimal(self, v) -> Optional[Decimal]:
        return to_decimal(v)

    def _parse_date(self, v) -> Optional[str]:
        return parse_date(v)

    def _clean_tax_no(self, v) -> str:
        return clean_tax_no(v)
