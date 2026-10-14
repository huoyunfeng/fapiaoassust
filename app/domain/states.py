"""状态机定义（设计 §3，系统严谨性来源）。

约束：
- 状态只能按图转移，禁止跳转；
- 每次状态变更必须写 invoice_status_log（由服务层通过仓储完成，I7）。
"""
from enum import Enum


class InvoiceStatus(str, Enum):
    PARSING = "PARSING"          # 解析中
    PARSED = "PARSED"            # 解析成功，待校验
    NORMAL = "NORMAL"            # 校验通过，无重复
    ABNORMAL = "ABNORMAL"        # 校验失败（可人工修正）
    DUPLICATE = "DUPLICATE"      # 重复发票（可人工确认非重复）
    MATCHED = "MATCHED"          # 已匹配报销单


class MatchStatus(str, Enum):
    PENDING = "PENDING"          # 匹配中
    MATCHED = "MATCHED"          # 自动通过 score >= 0.95
    SUSPICIOUS = "SUSPICIOUS"    # 0.7 <= score < 0.95，待人工
    CONFLICT = "CONFLICT"        # 字段冲突
    UNMATCHED = "UNMATCHED"      # 无候选
    CONFIRMED = "CONFIRMED"      # 人工确认
    REJECTED = "REJECTED"        # 人工拒绝


# 设计 §3.1 发票状态机转移表。
# 注：设计图中 MATCHED 为终态；但删除报销单会级联删除生效匹配（I8），
# 发票若永久停留 MATCHED 将与数据不一致。故补充 MATCHED -> NORMAL
# 一条转移，仅在"解除匹配/报销单删除"时发生，并强制记录日志与原因。
INVOICE_TRANSITIONS: dict[str, set[str]] = {
    "PARSING": {"PARSED", "ABNORMAL"},
    "PARSED": {"NORMAL", "ABNORMAL", "DUPLICATE"},
    "NORMAL": {"DUPLICATE", "MATCHED"},
    "ABNORMAL": {"NORMAL"},                 # 人工修正后
    "DUPLICATE": {"NORMAL"},                # 人工确认非重复
    "MATCHED": {"NORMAL"},                  # 解除匹配 / 报销单删除（见上注）
}

# 设计 §3.2 匹配状态机转移表。
MATCH_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"MATCHED", "SUSPICIOUS", "CONFLICT", "UNMATCHED"},
    "SUSPICIOUS": {"CONFIRMED", "REJECTED"},
    "CONFLICT": {"CONFIRMED", "REJECTED"},
    # MATCHED/CONFIRMED 为生效态；解除匹配时置 REJECTED（操作员动作，记录原因）
    "MATCHED": {"REJECTED"},
    "CONFIRMED": {"REJECTED"},
    "UNMATCHED": set(),
    "REJECTED": set(),
}

ACTIVE_MATCH_STATUSES = ("MATCHED", "CONFIRMED")


class InvalidTransition(Exception):
    """非法状态转移（禁止跳转）。"""


def assert_invoice_transition(current: str, target: str) -> None:
    if target not in INVOICE_TRANSITIONS.get(current, set()):
        raise InvalidTransition(
            f"发票状态非法转移: {current} -> {target}（设计 §3.1 禁止跳转）"
        )


def assert_match_transition(current: str, target: str) -> None:
    if target not in MATCH_TRANSITIONS.get(current, set()):
        raise InvalidTransition(
            f"匹配状态非法转移: {current} -> {target}（设计 §3.2 禁止跳转）"
        )
