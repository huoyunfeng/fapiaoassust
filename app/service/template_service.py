"""模板管理服务（设计 §5）。

职责：模板的创建/编辑/复制/软删除/设默认/列表。
锚点字段（amount 必选、invoice_no 可选但推荐）必须存在。
"""
from typing import List, Optional

from app.domain.field_registry import (SYSTEM_FIELDS, is_anchor, is_system_key)
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import template_repo

logger = get_logger(__name__)


class TemplateError(Exception):
    pass


class TemplateService:

    # ------------------------------------------------------------------
    def create(self, name: str, fields_config: dict, rules_config: list = None,
               description: str = "", enterprise_id: int = None,
               user_id: int = None) -> dict:
        """创建模板。校验锚点字段后落库。"""
        self._validate_anchors(fields_config)
        rules_config = rules_config or []
        with db.tx() as conn:
            tid = template_repo.insert(conn, {
                "name": name.strip(),
                "description": description,
                "is_system": 0,
                "fields_config": fields_config,
                "rules_config": rules_config,
                "enterprise_id": enterprise_id,
                "user_id": user_id,
            })
        logger.info("模板 #%s 创建: %s", tid, name)
        return {"ok": True, "template_id": tid}

    # ------------------------------------------------------------------
    def update(self, template_id: int, name: str = None, description: str = None,
               fields_config: dict = None, rules_config: list = None) -> dict:
        """编辑模板。系统内置模板不可编辑。"""
        with db.read() as conn:
            tpl = template_repo.get(conn, template_id)
        if tpl is None:
            return {"ok": False, "message": "模板不存在"}
        if tpl["is_system"]:
            return {"ok": False, "message": "系统内置模板不可编辑，请复制后修改"}
        data: dict = {}
        if name is not None:
            data["name"] = name.strip()
        if description is not None:
            data["description"] = description
        if fields_config is not None:
            self._validate_anchors(fields_config)
            data["fields_config"] = fields_config
        if rules_config is not None:
            data["rules_config"] = rules_config
        with db.tx() as conn:
            template_repo.update(conn, template_id, data)
        return {"ok": True, "template_id": template_id}

    # ------------------------------------------------------------------
    def copy(self, template_id: int, new_name: str,
             enterprise_id: int = None, user_id: int = None) -> dict:
        """基于现有模板创建新模板。"""
        with db.read() as conn:
            src = template_repo.get(conn, template_id)
        if src is None:
            return {"ok": False, "message": "模板不存在"}
        return self.create(
            name=new_name,
            fields_config=src["fields_config"],
            rules_config=src["rules_config"],
            description=f"复制自 {src['name']}",
            enterprise_id=enterprise_id,
            user_id=user_id,
        )

    # ------------------------------------------------------------------
    def delete(self, template_id: int) -> dict:
        """软删除。已使用的模板不受影响。"""
        with db.read() as conn:
            tpl = template_repo.get(conn, template_id)
        if tpl is None:
            return {"ok": False, "message": "模板不存在"}
        if tpl["is_system"]:
            return {"ok": False, "message": "系统内置模板不可删除"}
        with db.tx() as conn:
            template_repo.soft_delete(conn, template_id)
        return {"ok": True, "message": "模板已删除（软删除，历史数据不受影响）"}

    # ------------------------------------------------------------------
    def set_default(self, template_id: int, enterprise_id: int = None) -> dict:
        with db.read() as conn:
            tpl = template_repo.get(conn, template_id)
        if tpl is None:
            return {"ok": False, "message": "模板不存在"}
        with db.tx() as conn:
            template_repo.clear_default(conn, enterprise_id)
            template_repo.set_default(conn, template_id)
        return {"ok": True, "message": "已设为默认模板"}

    # ------------------------------------------------------------------
    def list(self, enterprise_id: int = None) -> List[dict]:
        with db.read() as conn:
            return template_repo.list(conn, enterprise_id)

    def get(self, template_id: int) -> Optional[dict]:
        with db.read() as conn:
            return template_repo.get(conn, template_id)

    def get_default(self, enterprise_id: int = None) -> Optional[dict]:
        with db.read() as conn:
            return template_repo.get_default(conn, enterprise_id)

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_anchors(fields_config: dict) -> None:
        """必须包含 amount 锚点；invoice_no 可选但推荐。"""
        if not fields_config or "fields" not in fields_config:
            raise TemplateError("fields_config 缺少 fields 数组")
        keys = {f.get("system_key") for f in fields_config["fields"]}
        if "amount" not in keys:
            raise TemplateError("必须映射「金额」字段（匹配锚点）")
        # invoice_no 允许缺失，但会限制匹配能力


def system_fields_meta() -> List[dict]:
    """返回系统字段元数据（供前端映射向导下拉选择）。"""
    return [
        {"key": f["key"], "label": f["label"], "type": f["type"],
         "anchor": f["anchor"], "required": f["required"]}
        for f in SYSTEM_FIELDS
    ]


template_service = TemplateService()
