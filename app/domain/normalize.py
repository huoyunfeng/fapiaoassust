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
            item_summary=self._clean_summary(dto.item_summary),
            is_red_flush=bool(dto.is_red_flush),
        )

    # 以下为实例方法别名，保持与设计伪代码一致的调用形态
    def _clean(self, v) -> str:
        return clean(v)

    # 规格型号 token：同时含字母和数字的组合（如 F50、20A3Pro、F50-20A3Pro）
    _MODEL_TOKEN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-./]*$")
    # 纯数字/符号 token（数量、单价、税率等噪音；仅在存在其它 token 时剔除）
    _NUMERIC_TOKEN = re.compile(r"^[\d.,\-/:%¥￥*]+$")
    _PAREN_CHUNK = re.compile(r"[（(][^（）()]*[）)]")
    _SUMMARY_NOISE_KW = ("电子发票", "普通发票", "增值税", "发票代码", "发票号码",
                         "开票日期", "价税合计", "统一社会信用代码", "纳税人识别号")
    # 表头/字段名停用词（整词匹配）：OCR 把表格表头并入商品行时剔除，如 "餐饮服务 金额"
    _HEADER_STOPWORDS = frozenset({
        "项目名称", "规格型号", "单位", "数量", "单价", "金额",
        "税率", "税额", "价税合计", "合计", "税率/征收率", "税率征收率",
    })

    def _clean_summary(self, v) -> str:
        """报销内容统一提取规则（商业化口径）：

        1) 优先提取第二类明细：`*税务大类*具体明细` -> 取第二个星号后面的内容
        2) 剔除杂质：去首尾空格；去括号内容；去规格型号 token（字母+数字组合，
           如 F50-20A3Pro），只保留核心商品名
        3) 兜底：没有星号或提取后为空 -> 直接使用原文本去掉空格
        4) 禁止只展示税务大类：除非发票真的只有大类没有明细（星号后为空）才用大类
        """
        s = clean(v)
        if not s:
            return ""

        # 多商品逗号分隔：取第一条做摘要（与历史行为一致）
        first = s.split(",")[0].strip()

        m = re.match(r"^\*([^*\n]+)\*(.*)$", first)
        if m:
            category, detail = m.group(1).strip(), m.group(2).strip()
            s = detail or category           # 规则1 + 规则4的例外（只有大类时用大类）
        else:
            s = first                        # 规则3 兜底

        # 规则2：剔除括号内容与规格型号/纯数字噪音 token，以及混入商品行的表头词
        s = self._PAREN_CHUNK.sub(" ", s)
        tokens = [t for t in s.split()
                  if not self._MODEL_TOKEN.match(t)
                  and not re.fullmatch(r"[A-Za-z]{1,2}", t)  # 孤立字母碎片（型号折行残留，如 "F"）
                  and t not in self._HEADER_STOPWORDS]
        if len(tokens) > 1:
            tokens = [t for t in tokens if not self._NUMERIC_TOKEN.match(t)]
        # 剔除双栏折行交叠产生的重复碎片：互为子串的 token 只保留最长者
        # （如 [美的电, 美的电热水器, 热水器] -> [美的电热水器]）
        tokens = [t for i, t in enumerate(tokens)
                  if not any(i != j and t in u and (len(u) > len(t) or j < i)
                             for j, u in enumerate(tokens))]
        s = " ".join(tokens)
        s = re.sub(r"\s{2,}", " ", s).strip(" ,，;；")

        # 防御性过滤：PDF 标题/表头被误识别为商品名 -> 返回空
        if any(kw in s for kw in self._SUMMARY_NOISE_KW):
            return ""
        return s[:30]

    def _to_decimal(self, v) -> Optional[Decimal]:
        return to_decimal(v)

    def _parse_date(self, v) -> Optional[str]:
        return parse_date(v)

    def _clean_tax_no(self, v) -> str:
        return clean_tax_no(v)
