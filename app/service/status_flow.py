"""状态流转编排（应用服务层专用）。

职责铁律：状态合法性判断（领域 states）+ 更新 + 强制日志（I7）。
所有发票状态变更必须经过这里，禁止直接改 status 字段。
"""
from app.domain.states import assert_invoice_transition
from app.infra.logger import get_logger
from app.repository.repos import invoice_repo, log_repo

logger = get_logger(__name__)


def change_invoice_status(conn, invoice_id: int, target: str,
                          reason: str = "", operator: str = "system") -> str:
    """校验并执行一次发票状态转移，写状态日志。返回原状态。"""
    inv = invoice_repo.get(conn, invoice_id)
    if inv is None:
        raise ValueError(f"发票 #{invoice_id} 不存在")
    current = inv["status"]
    if current == target:
        return current
    assert_invoice_transition(current, target)
    invoice_repo.update(conn, invoice_id, {"status": target})
    log_repo.log_invoice_status(conn, invoice_id, current, target, reason, operator)
    logger.info("发票 #%s 状态 %s -> %s (%s, by %s)", invoice_id, current, target, reason, operator)
    return current
