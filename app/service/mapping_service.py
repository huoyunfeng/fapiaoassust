"""智能映射服务（设计 §6）。

职责：
1. 读取 Excel 表头并计算指纹；
2. 命中 mapping_cache 则直接返回缓存映射；
3. 未命中则调用 auto_map 自动推断，供映射向导展示；
4. 导入完成后回写映射缓存（可记忆）。
"""
from typing import Dict, List, Optional, Tuple

from app.domain.field_registry import auto_map, header_fingerprint
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import mapping_cache_repo, template_repo

logger = get_logger(__name__)


class MappingService:

    def detect(self, headers: List[str], enterprise_id: int = None,
               header_row: int = 1) -> dict:
        """根据表头返回映射方案（命中缓存 / 自动推断）。

        返回：
          {
            "fingerprint": str,
            "headers": [...],
            "header_row": int,
            "mapping": {excel_header: system_key},   # 命中缓存或自动推断
            "from_cache": bool,
            "template_id": int | None,
          }
        """
        fp = header_fingerprint(headers)
        with db.read() as conn:
            cached = mapping_cache_repo.get(conn, fp, enterprise_id)
        if cached:
            with db.tx() as conn:
                mapping_cache_repo.touch(conn, fp, enterprise_id)
            return {
                "fingerprint": fp,
                "headers": headers,
                "header_row": header_row,
                "mapping": cached["mapping"],
                "from_cache": True,
                "template_id": cached["template_id"],
            }
        return {
            "fingerprint": fp,
            "headers": headers,
            "header_row": header_row,
            "mapping": auto_map(headers),
            "from_cache": False,
            "template_id": None,
        }

    def remember(self, fingerprint: str, template_id: int, headers: List[str],
                 mapping: Dict[str, str], enterprise_id: int = None,
                 user_id: int = None) -> None:
        """导入成功后回写映射缓存。"""
        with db.tx() as conn:
            mapping_cache_repo.upsert(conn, fingerprint, template_id, headers,
                                      mapping, enterprise_id, user_id)
            template_repo.bump_usage(conn, template_id)
        logger.debug("映射缓存已更新: fingerprint=%s template=%s", fingerprint[:8], template_id)

    def build_fields_config(self, headers: List[str],
                            mapping: Dict[str, str],
                            header_row: int = 1) -> dict:
        """由表头 + 用户映射生成 fields_config（用于动态创建模板）。

        未映射或选择「自定义字段」的列统一生成唯一 key（custom_<列名>），
        避免多列共用 "custom" 而在 extra_data 中互相覆盖。
        """
        from app.domain.field_registry import is_system_key
        fields = []
        for h in headers:
            name = (h or "").strip()
            if not name:
                continue
            sys_key = (mapping.get(name) or "").strip()
            is_sys = bool(sys_key) and is_system_key(sys_key) and not sys_key.startswith("custom")
            if is_sys:
                fields.append({
                    "excel_header": name,
                    "system_key": sys_key,
                    "required": sys_key in ("reimburse_no", "amount"),
                    "type": "decimal" if sys_key == "amount" else "string",
                    "is_custom": False,
                    "display_name": name,
                })
            else:
                # 自定义字段：key 唯一且稳定，落库到 extra_data 时用列名做键
                fields.append({
                    "excel_header": name,
                    "system_key": f"custom_{name}",
                    "required": False,
                    "type": "string",
                    "is_custom": True,
                    "display_name": name,
                })
        return {
            "fields": fields,
            "options": {
                "header_row": int(header_row or 1),
                "data_start_row": int(header_row or 1) + 1,
                "multi_invoice_separator": ",",
                "invoice_mode": "one_to_one",
            },
        }


mapping_service = MappingService()
