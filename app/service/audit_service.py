"""审计日志服务（模块六）。

所有分类相关写操作（字典新增/禁用/合并、单条修改、批量修改、
冲突仲裁、解除匹配、快捷修复、人工绑定）都必须调用本模块落一条审计记录。

detail 字段统一为 JSON 字符串，包含结构化元数据：
  {
    "why": "...原因/依据...",
    "reason_option": "业务人员已确认 | 员工补了凭证 | ...",
    "changes": [{"field":"报销金额","old":"29.30","new":"29.50"}],
    "invoice_no": "...",
    "reimburse_no": "...",
    "match_id": 123
  }
旧版调用传纯字符串 detail 依然兼容（存入时自动套成 {"legacy": "..."}）。
"""
import json
from typing import Optional

from app.config import settings
from app.repository import db
from app.repository.repos import audit_repo


def log(conn, action, target_type, target_id="", target_label="",
        old_value="", new_value="", operator=None, detail="",
        detail_json: Optional[dict] = None) -> int:
    """在调用方事务内写审计记录（不单独开事务，保证与业务操作同成败）。

    detail:     展示用的人类可读文本（搜索命中也会扫描）。
    detail_json: 结构化元数据（变更前后 / 原因选项 / 关联单号等），
                 会与 detail 合并进 detail 字段的 JSON 对象里。
                 若 detail 本身已是 JSON（以 '{' 开头），会与 detail_json 合并；
                 若 detail 是普通字符串，则存 {"legacy": str, **detail_json}。
    """
    final_detail = detail
    if detail_json:
        try:
            existing = json.loads(detail) if isinstance(detail, str) and detail.lstrip().startswith("{") else {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        merged = {**existing, **detail_json}
        if detail and not existing:
            merged["legacy"] = detail
        final_detail = json.dumps(merged, ensure_ascii=False)
    return audit_repo.log(
        conn, action, target_type, str(target_id), target_label,
        old_value, new_value, operator or settings.default_operator, final_detail)


def list_recent(limit: int = 100, target_type: str = None,
                action: str = None, keyword: str = None,
                date_from: str = None, date_to: str = None,
                page: int = 1, page_size: int = 20) -> dict:
    with db.read() as conn:
        return audit_repo.list(conn, limit=limit, target_type=target_type,
                               action=action, keyword=keyword,
                               date_from=date_from, date_to=date_to,
                               page=page, page_size=page_size)


def parse_detail(raw: str) -> dict:
    """安全地解析审计 detail 字段，兼容纯文本 / JSON 两种形态。"""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {"legacy": raw}
