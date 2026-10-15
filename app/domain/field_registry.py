"""字段注册表（设计 §4.1 / §6.2）。

系统字段是内置标准字段，不可删除，只能被 Excel 表头映射。
锚点字段（amount / invoice_no）是匹配引擎必须依赖的字段。

FIELD_ALIASES 用于自动映射：基于表头名称的模糊包含匹配。
"""
import hashlib
from typing import Dict, List, Optional


# ------------------------------------------------------------------
# 系统字段注册表（不可删除，只能映射）
# ------------------------------------------------------------------
SYSTEM_FIELDS: List[dict] = [
    {"key": "reimburse_no",   "label": "报销单号", "type": "string",  "anchor": False, "required": True},
    {"key": "submitter",      "label": "提交人",   "type": "string",  "anchor": False, "required": False},
    {"key": "reimburse_date", "label": "报销日期", "type": "date",    "anchor": False, "required": False},
    {"key": "period_start",   "label": "期间起",   "type": "date",    "anchor": False, "required": False},
    {"key": "period_end",     "label": "期间止",   "type": "date",    "anchor": False, "required": False},
    {"key": "expense_type",   "label": "费用类型", "type": "string",  "anchor": False, "required": False},
    {"key": "amount",         "label": "金额",     "type": "decimal", "anchor": True,  "required": True},
    {"key": "invoice_no",     "label": "发票号",   "type": "string",  "anchor": True,  "required": False},
    {"key": "invoice_date",   "label": "发票日期", "type": "date",    "anchor": False, "required": False},
    {"key": "remark",         "label": "备注",     "type": "string",  "anchor": False, "required": False},
]

ANCHOR_KEYS = {"amount", "invoice_no"}
SYSTEM_KEYS = {f["key"] for f in SYSTEM_FIELDS}


def get_field(key: str) -> Optional[dict]:
    for f in SYSTEM_FIELDS:
        if f["key"] == key:
            return f
    return None


def is_system_key(key: str) -> bool:
    return key in SYSTEM_KEYS


def is_anchor(key: str) -> bool:
    return key in ANCHOR_KEYS


# ------------------------------------------------------------------
# 表头别名（用于自动映射）
# ------------------------------------------------------------------
FIELD_ALIASES: Dict[str, List[str]] = {
    "reimburse_no":   ["报销单号", "单号", "报销编号", "申请单号", "单据号"],
    "submitter":      ["提交人", "报销人", "申请人", "员工姓名", "姓名"],
    "reimburse_date": ["报销日期", "申请日期", "提交日期", "单据日期",
                       "用餐日期", "发生日期", "费用日期", "实际日期"],
    "period_start":   ["期间起", "开始日期", "费用起始", "起始日期"],
    "period_end":     ["期间止", "结束日期", "费用截止", "截止日期"],
    "expense_type":   ["费用类型", "费用类别", "科目", "费用科目", "项目名称", "费用归属"],
    "amount":         ["金额", "报销金额", "费用合计", "合计金额", "总金额", "价税合计"],
    "invoice_no":     ["发票号", "发票号码", "票据号", "票号", "发票编号"],
    "invoice_date":   ["发票日期", "开票日期"],
    "remark":         ["备注", "说明", "事由", "摘要"],
}


def auto_map(headers: List[str]) -> Dict[str, str]:
    """根据表头自动推断 -> 系统字段映射。

    返回 {excel_header: system_key}。
    规则：表头清洗后若包含任一别名则命中；别名优先级按 SYSTEM_FIELDS 顺序。
    """
    mapping: Dict[str, str] = {}
    for h in headers:
        h_clean = (h or "").strip()
        if not h_clean:
            continue
        h_lower = h_clean.lower()
        hit = None
        for sys_key, aliases in FIELD_ALIASES.items():
            if hit:
                break
            for alias in aliases:
                if alias in h_clean or h_clean in alias:
                    # 避免同一系统字段被多个表头占用（取第一个命中）
                    if sys_key not in mapping.values():
                        hit = sys_key
                    break
        if hit:
            mapping[h_clean] = hit
    return mapping


# ------------------------------------------------------------------
# 表头指纹（设计 §4.7）
# ------------------------------------------------------------------
def header_fingerprint(headers: List[str]) -> str:
    """表头指纹：对排序、清洗后的表头取 sha256 前 16 位。

    优点：即使列顺序改变，只要表头内容一致，仍能命中缓存。
    """
    normalized = sorted(h.strip().lower() for h in headers if h and h.strip())
    raw = "|".join(normalized)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------
# 经典模板（系统内置，开箱即用）
# ------------------------------------------------------------------
CLASSIC_TEMPLATE_FIELDS: List[dict] = [
    {"excel_header": "报销单号", "system_key": "reimburse_no",   "required": True,  "type": "string"},
    {"excel_header": "提交人",   "system_key": "submitter",      "required": False, "type": "string"},
    {"excel_header": "报销日期", "system_key": "reimburse_date", "required": False, "type": "date"},
    {"excel_header": "期间起",   "system_key": "period_start",   "required": False, "type": "date"},
    {"excel_header": "期间止",   "system_key": "period_end",     "required": False, "type": "date"},
    {"excel_header": "费用类型", "system_key": "expense_type",   "required": False, "type": "string"},
    {"excel_header": "金额",     "system_key": "amount",         "required": True,  "type": "decimal"},
    {"excel_header": "发票号",   "system_key": "invoice_no",     "required": False, "type": "string"},
    {"excel_header": "发票日期", "system_key": "invoice_date",   "required": False, "type": "date"},
    {"excel_header": "备注",     "system_key": "remark",         "required": False, "type": "string"},
]

CLASSIC_OPTIONS: dict = {
    "header_row": 1,
    "data_start_row": 2,
    "multi_invoice_separator": ",",
    "invoice_mode": "one_to_one",
}


def default_fields_config() -> dict:
    return {"fields": list(CLASSIC_TEMPLATE_FIELDS), "options": dict(CLASSIC_OPTIONS)}
