"""审计日志服务（模块六）。

所有分类相关写操作（字典新增/禁用/合并、单条修改、批量修改、
冲突仲裁、解除匹配）都必须调用本模块落一条审计记录。
"""
from app.config import settings
from app.repository import db
from app.repository.repos import audit_repo


def log(conn, action: str, target_type: str, target_id="", target_label="",
        old_value="", new_value="", operator: str = None, detail: str = "") -> int:
    """在调用方事务内写审计记录（不单独开事务，保证与业务操作同成败）。"""
    return audit_repo.log(
        conn, action, target_type, target_id, target_label,
        old_value, new_value, operator or settings.default_operator, detail)


def list_recent(limit: int = 100, target_type: str = None) -> list:
    with db.read() as conn:
        return audit_repo.list(conn, limit=limit, target_type=target_type)
