"""状态流转编排（应用服务层专用）。

职责铁律：状态合法性判断（领域 states）+ 更新 + 强制日志（I7）。
所有发票状态变更必须经过这里，禁止直接改 status 字段。
"""
from app.domain.states import InvalidTransition, assert_invoice_transition
from app.infra.logger import get_logger
from app.infra.request_context import current_operator
from app.repository.repos import invoice_repo, log_repo

logger = get_logger(__name__)


def change_invoice_status(conn, invoice_id: int, target: str,
                          reason: str = "", operator: str = "system",
                          matched_amount=None) -> str:
    """校验并执行一次发票状态转移，写状态日志。返回原状态。

    闭环强约束：转为 MATCHED 时用条件更新（WHERE is_closed=0）做乐观锁，
    并发下只有一张单据能抢到该发票；落库同时记录已报销金额。
    """
    # 状态日志同样记录真实登录用户，避免被前端 operator 覆盖
    operator = current_operator() or operator
    inv = invoice_repo.get(conn, invoice_id)
    if inv is None:
        raise ValueError(f"发票 #{invoice_id} 不存在")
    current = inv["status"]
    if current == target:
        return current
    assert_invoice_transition(current, target)
    if target == "MATCHED":
        amt = matched_amount if matched_amount is not None else (inv.get("total_amount") or "0.00")
        try:
            amt = f"{float(amt):.2f}"
        except (TypeError, ValueError):
            amt = "0.00"
        cur = conn.execute(
            "UPDATE invoice SET status=?, is_closed=1, matched_amount=? "
            "WHERE id=? AND is_closed=0", (target, amt, invoice_id))
        if cur.rowcount == 0:
            raise InvalidTransition("该发票已被其他单据占用，系统严禁拆分报销。")
    elif current == "MATCHED":
        # 解除匹配 / 报销单删除：闭环标记同步回退，重新进入候选池
        conn.execute(
            "UPDATE invoice SET status=?, is_closed=0, matched_amount='0.00' WHERE id=?",
            (target, invoice_id))
    else:
        invoice_repo.update(conn, invoice_id, {"status": target})
    log_repo.log_invoice_status(conn, invoice_id, current, target, reason, operator)
    logger.info("发票 #%s 状态 %s -> %s (%s, by %s)", invoice_id, current, target, reason, operator)
    return current
