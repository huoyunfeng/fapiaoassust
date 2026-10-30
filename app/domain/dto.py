"""领域层数据传输对象（DTO）。

领域引擎层为纯函数式：输入 DTO、输出 DTO，无副作用（设计 §2）。
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


# ---------------------------------------------------------------- 校验问题
@dataclass
class Issue:
    """校验/规则产生的一条问题。severity: ERROR / WARN"""
    code: str
    message: str
    severity: str = "ERROR"  # ERROR / WARN

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


# ---------------------------------------------------------------- 发票
@dataclass
class InvoiceDTO:
    """发票标准化 DTO。金额为 Decimal（允许 None），日期为 ISO8601 字符串。"""
    invoice_code: str = ""
    invoice_no: str = ""
    invoice_date: Optional[str] = None          # YYYY-MM-DD
    buyer_name: str = ""
    buyer_tax_no: str = ""
    seller_name: str = ""
    seller_tax_no: str = ""
    amount: Optional[Decimal] = None            # 金额（不含税）
    tax_amount: Optional[Decimal] = None        # 税额
    total_amount: Optional[Decimal] = None      # 价税合计
    tax_rate: str = ""
    item_summary: str = ""
    is_red_flush: bool = False                  # 是否红冲（负数发票）
    # ---- 无票号发票（打车票/行程单）的票面特征 ----
    plate_no: str = ""                          # 车牌号（如 沪A12345）
    ride_time: str = ""                         # 上下车时间（原文或 ISO8601）
    traffic_text: str = ""                      # 票面关键文字（线路/上下车地点等）

    def filled_fields(self) -> list[str]:
        """返回已填充的必填字段名（用于解析引擎终止判断）。"""
        return [f for f in ("invoice_no", "invoice_date", "total_amount")
                if getattr(self, f)]


# ---------------------------------------------------------------- 报销单
@dataclass
class ReimburseDTO:
    """报销单 DTO（来自 Excel 导入或人工录入）。"""
    batch_id: str = ""
    reimburse_no: str = ""
    submitter: str = ""
    reimburse_date: Optional[str] = None        # YYYY-MM-DD
    period_start: Optional[str] = None          # YYYY-MM-DD
    period_end: Optional[str] = None            # YYYY-MM-DD
    expense_type: str = ""
    amount: Optional[Decimal] = None            # 报销金额
    invoice_no: str = ""                        # 报销单上登记的发票号
    invoice_date: Optional[str] = None
    remark: str = ""
    row_index: int = 0                          # 原始 Excel 行号，用于追溯
    # ---- 模板引擎新增字段 ----
    template_id: Optional[int] = None           # 来源模板
    extra_data: dict = None                     # 自定义字段 JSON
    source_row: Optional[int] = None            # 原始 Excel 行号（数据行，1-based）
    parent_id: Optional[int] = None             # 拆行的父行 id（one_to_many / many_to_one）
    split_type: str = "none"                    # none / one_to_many / many_to_one
    # ---- 行程明细类报销单的业务特征（无票号场景匹配用）----
    reason: str = ""                            # 事由
    origin: str = ""                            # 起始地点
    destination: str = ""                       # 到达地点
    plate_no: str = ""                          # 车牌号（行程单如已登记）

    def __post_init__(self):
        if self.extra_data is None:
            self.extra_data = {}

    @property
    def period_range(self) -> tuple[Optional[str], Optional[str]]:
        """用于日期匹配的有效期间：优先期间起止，退化到报销日期本身。"""
        if self.period_start or self.period_end:
            return self.period_start, self.period_end
        return self.reimburse_date, self.reimburse_date


# ---------------------------------------------------------------- 解析
@dataclass
class ParseTask:
    """解析任务：文件内容已由基础设施层读入内存，解析器保持纯函数。"""
    file_name: str
    file_type: str            # pdf / ofd / xml / jpg / jpeg / png
    content: bytes


@dataclass
class ParseResult:
    """解析结果。degrade_chain 记录降级链路（设计 §6.1）。"""
    success: bool
    dto: Optional[InvoiceDTO] = None
    engine: str = ""                       # xml / ofd / pdf_text / ocr / manual
    confidence: float = 0.0                # [0,1]
    degrade_chain: list = field(default_factory=list)   # [{engine, status, reason}]
    error: str = ""
    item_summary_raw: str = ""             # normalize 之前的完整项目名（带 *税收分类*、型号等）


# ---------------------------------------------------------------- 规则
@dataclass
class RuleDTO:
    """业务规则（来自 rule 表）。"""
    id: int = 0
    name: str = ""
    # buyer_tax_no / seller_blacklist / sensitive_words / period / amount
    # / cross_period / red_flush / dedup(旧) / custom(旧)
    rule_type: str = ""
    scope: str = "invoice"     # invoice = 针对单张发票；reimburse = 针对单笔报销单
    action: str = "block"      # block = 阻断（禁止匹配）；warn = 警告（放行并标黄）
    priority: int = 100
    stop_on_fail: bool = False
    config: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    is_core: bool = False
    enabled: bool = True
