"""仓储层 - 数据访问对象（唯一写库入口，机械执行 SQL，不做业务判断）。

所有方法接收 conn（由应用服务层通过 db.tx() 开启事务并传入）。
"""
import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from app.repository import db

ACTIVE_SQL = "status IN ('MATCHED','CONFIRMED')"


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return None if row is None else {k: row[k] for k in row.keys()}


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[dict]:
    return [_row_to_dict(r) for r in rows]


# =====================================================================
# 发票
# =====================================================================
class InvoiceRepo:
    INSERT_FIELDS = [
        "file_hash", "source_path", "file_type", "invoice_code", "invoice_no",
        "invoice_date", "buyer_name", "buyer_tax_no", "seller_name", "seller_tax_no",
        "amount", "tax_amount", "total_amount", "tax_rate", "item_summary",
        "check_code", "raw_json", "parse_engine", "parse_confidence", "status",
        "is_red_flush",
    ]

    def insert(self, conn: sqlite3.Connection, data: dict) -> int:
        fields = [f for f in self.INSERT_FIELDS if f in data]
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO invoice ({cols}) VALUES ({marks})",
            [self._serialize(data.get(f)) for f in fields],
        )
        return cur.lastrowid

    def update(self, conn: sqlite3.Connection, invoice_id: int, data: dict) -> None:
        fields = [f for f in self.INSERT_FIELDS if f in data]
        if not fields:
            return
        sets = ", ".join(f"{f} = ?" for f in fields)
        params = [self._serialize(data.get(f)) for f in fields]
        params.append(invoice_id)
        conn.execute(
            f"UPDATE invoice SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
            params,
        )

    def get(self, conn: sqlite3.Connection, invoice_id: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM invoice WHERE id = ?", (invoice_id,)).fetchone()
        return _row_to_dict(row)

    def get_by_hash(self, conn: sqlite3.Connection, file_hash: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM invoice WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return _row_to_dict(row)

    def list(self, conn: sqlite3.Connection, status: str = None,
             keyword: str = None, limit: int = 500, offset: int = 0) -> List[dict]:
        sql, params = "SELECT * FROM invoice WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if keyword:
            sql += " AND (invoice_no LIKE ? OR invoice_code LIKE ? OR buyer_name LIKE ? OR seller_name LIKE ? OR item_summary LIKE ?)"
            kw = f"%{keyword}%"
            params += [kw, kw, kw, kw, kw]
        sql += " ORDER BY id ASC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return _rows_to_dicts(conn.execute(sql, params))

    def fingerprints(self, conn: sqlite3.Connection, exclude_id: int = None) -> List[tuple]:
        """用于去重引擎的指纹列表 [(id, file_hash, invoice_code, invoice_no)]。"""
        sql = "SELECT id, file_hash, invoice_code, invoice_no FROM invoice WHERE status != 'DUPLICATE'"
        params: list = []
        if exclude_id:
            sql += " AND id != ?"
            params.append(exclude_id)
        return [tuple(r) for r in conn.execute(sql, params)]

    def delete(self, conn: sqlite3.Connection, invoice_id: int) -> None:
        conn.execute("DELETE FROM invoice WHERE id = ?", (invoice_id,))

    def count_by_status(self, conn: sqlite3.Connection) -> Dict[str, int]:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM invoice GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _serialize(v):
        if isinstance(v, bool):
            return 1 if v else 0
        return v


# =====================================================================
# 报销单
# =====================================================================
class ReimburseRepo:
    INSERT_FIELDS = [
        "batch_id", "template_id", "reimburse_no", "submitter", "reimburse_date",
        "period_start", "period_end", "expense_type", "amount", "invoice_no",
        "invoice_date", "remark", "extra_data", "source_row", "parent_id",
        "split_type", "raw_row", "row_index",
    ]

    def insert(self, conn: sqlite3.Connection, data: dict) -> int:
        fields = [f for f in self.INSERT_FIELDS if f in data]
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO reimburse ({cols}) VALUES ({marks})",
            [self._serialize(data.get(f), f) for f in fields],
        )
        return cur.lastrowid

    def get(self, conn: sqlite3.Connection, rid: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM reimburse WHERE id = ?", (rid,)).fetchone()
        return self._to_dict(row)

    def list(self, conn: sqlite3.Connection, batch_id: str = None,
             keyword: str = None, limit: int = 500, offset: int = 0) -> List[dict]:
        sql, params = "SELECT * FROM reimburse WHERE 1=1", []
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        if keyword:
            sql += " AND (reimburse_no LIKE ? OR submitter LIKE ? OR expense_type LIKE ? OR remark LIKE ?)"
            kw = f"%{keyword}%"
            params += [kw, kw, kw, kw]
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._to_dict(r) for r in conn.execute(sql, params)]

    def batches(self, conn: sqlite3.Connection) -> List[dict]:
        return _rows_to_dicts(conn.execute(
            """SELECT r.batch_id, COUNT(*) AS row_count, MIN(r.created_at) AS created_at,
                      t.name AS template_name
               FROM reimburse r
               LEFT JOIN reimburse_template t ON t.id = r.template_id
               GROUP BY r.batch_id ORDER BY created_at DESC"""
        ))

    def update_remark(self, conn: sqlite3.Connection, rid: int, remark: str) -> None:
        """仅更新报销单备注（匹配说明写入用，不触及其它业务字段）。"""
        conn.execute("UPDATE reimburse SET remark = ? WHERE id = ?", (remark, rid))

    def delete(self, conn: sqlite3.Connection, rid: int) -> None:
        conn.execute("DELETE FROM reimburse WHERE id = ?", (rid,))

    def delete_batch(self, conn: sqlite3.Connection, batch_id: str) -> int:
        cur = conn.execute("DELETE FROM reimburse WHERE batch_id = ?", (batch_id,))
        return cur.rowcount

    def count(self, conn: sqlite3.Connection) -> int:
        return conn.execute("SELECT COUNT(*) AS n FROM reimburse").fetchone()["n"]

    @staticmethod
    def _serialize(v, field: str = ""):
        if field in ("extra_data", "raw_row"):
            return json.dumps(v or {}, ensure_ascii=False)
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    @staticmethod
    def _to_dict(row):
        d = _row_to_dict(row)
        if d:
            try:
                d["extra_data"] = json.loads(d.get("extra_data") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["extra_data"] = {}
        return d


# =====================================================================
# 匹配
# =====================================================================
class MatchRepo:
    def insert(self, conn: sqlite3.Connection, data: dict) -> int:
        cur = conn.execute(
            """INSERT INTO match_result
               (invoice_id, reimburse_id, match_type, match_score, status,
                diff_fields, message, confirmed_by, confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["invoice_id"], data.get("reimburse_id"), data["match_type"],
                data.get("match_score", 0.0), data["status"],
                json.dumps(data.get("diff_fields", []), ensure_ascii=False),
                data.get("message", ""), data.get("confirmed_by"),
                data.get("confirmed_at"),
            ),
        )
        return cur.lastrowid

    def insert_pair(self, conn: sqlite3.Connection, match_id: int,
                    invoice_id: int, reimburse_id: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO match_pair (match_id, invoice_id, reimburse_id) VALUES (?, ?, ?)",
            (match_id, invoice_id, reimburse_id),
        )

    def get(self, conn: sqlite3.Connection, mid: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM match_result WHERE id = ?", (mid,)).fetchone()
        d = _row_to_dict(row)
        if d:
            d["diff_fields"] = json.loads(d.get("diff_fields") or "[]")
        return d

    def update_status(self, conn: sqlite3.Connection, mid: int, status: str,
                      confirmed_by: str = None) -> None:
        conn.execute(
            """UPDATE match_result
               SET status = ?, confirmed_by = COALESCE(?, confirmed_by),
                   confirmed_at = CASE WHEN ? IN ('CONFIRMED','REJECTED')
                       THEN datetime('now','localtime') ELSE confirmed_at END,
                   updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (status, confirmed_by, status, mid),
        )

    def active_for_invoice(self, conn: sqlite3.Connection, invoice_id: int) -> Optional[dict]:
        row = conn.execute(
            f"SELECT * FROM match_result WHERE invoice_id = ? AND {ACTIVE_SQL}",
            (invoice_id,),
        ).fetchone()
        return _row_to_dict(row)

    def active_for_reimburse(self, conn: sqlite3.Connection, reimburse_id: int) -> Optional[dict]:
        """报销单的生效匹配（含经 match_pair 关联的汇总匹配）。"""
        row = conn.execute(
            """SELECT m.* FROM match_result m
               WHERE (m.reimburse_id = ?
                      AND m.status IN ('MATCHED','CONFIRMED'))
                  OR (EXISTS (SELECT 1 FROM match_pair p
                              JOIN match_result m2 ON m2.id = p.match_id
                              WHERE p.reimburse_id = ? AND p.invoice_id = m.invoice_id
                                AND m2.status IN ('MATCHED','CONFIRMED')))
               LIMIT 1""",
            (reimburse_id, reimburse_id),
        ).fetchone()
        return _row_to_dict(row)

    def active_reimburse_ids(self, conn: sqlite3.Connection) -> set:
        """所有已占用（生效匹配）的报销单 id，含汇总匹配。"""
        rows = conn.execute(
            f"""SELECT DISTINCT reimburse_id FROM (
                    SELECT reimburse_id FROM match_result WHERE {ACTIVE_SQL} AND reimburse_id IS NOT NULL
                    UNION
                    SELECT p.reimburse_id FROM match_pair p
                        JOIN match_result m ON m.id = p.match_id AND m.{ACTIVE_SQL}
                )"""
        )
        return {r["reimburse_id"] for r in rows if r["reimburse_id"] is not None}

    def list(self, conn: sqlite3.Connection, statuses: List[str] = None,
             limit: int = 500) -> List[dict]:
        sql = """SELECT m.*, i.invoice_no, i.invoice_date, i.total_amount,
                        i.buyer_name, i.seller_name, i.status AS invoice_status,
                        r.reimburse_no, r.amount AS reimburse_amount, r.submitter,
                        r.expense_type, r.reimburse_date, r.period_start, r.period_end
                 FROM match_result m
                 JOIN invoice i ON i.id = m.invoice_id
                 LEFT JOIN reimburse r ON r.id = m.reimburse_id"""
        params: list = []
        if statuses:
            marks = ", ".join("?" for _ in statuses)
            sql += f" WHERE m.status IN ({marks})"
            params += statuses
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        out = _rows_to_dicts(conn.execute(sql, params))
        for d in out:
            d["diff_fields"] = json.loads(d.get("diff_fields") or "[]")
        return out

    def pairs_of(self, conn: sqlite3.Connection, match_ids: List[int]) -> Dict[int, List[dict]]:
        """match_id -> [{invoice_id, reimburse_id}]"""
        if not match_ids:
            return {}
        marks = ", ".join("?" for _ in match_ids)
        out: Dict[int, List[dict]] = {}
        for r in conn.execute(
            f"SELECT * FROM match_pair WHERE match_id IN ({marks})", match_ids
        ):
            out.setdefault(r["match_id"], []).append(_row_to_dict(r))
        return out

    def delete_non_active(self, conn: sqlite3.Connection) -> int:
        """清理上一轮的评估痕迹（非生效态记录），生效记录保留。"""
        cur = conn.execute(
            "DELETE FROM match_result WHERE status NOT IN ('MATCHED','CONFIRMED','REJECTED')"
        )
        return cur.rowcount

    def count_by_status(self, conn: sqlite3.Connection) -> Dict[str, int]:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM match_result GROUP BY status")
        return {r["status"]: r["n"] for r in rows}


# =====================================================================
# 状态日志（不变量 I7）
# =====================================================================
class LogRepo:
    def log_invoice_status(self, conn: sqlite3.Connection, invoice_id: int,
                           from_status: str, to_status: str,
                           reason: str = "", operator: str = "system") -> None:
        conn.execute(
            """INSERT INTO invoice_status_log (invoice_id, from_status, to_status, reason, operator)
               VALUES (?, ?, ?, ?, ?)""",
            (invoice_id, from_status, to_status, reason, operator),
        )

    def list_for_invoice(self, conn: sqlite3.Connection, invoice_id: int) -> List[dict]:
        return _rows_to_dicts(conn.execute(
            "SELECT * FROM invoice_status_log WHERE invoice_id = ? ORDER BY id DESC",
            (invoice_id,),
        ))


# =====================================================================
# 规则 / 公司抬头
# =====================================================================
class RuleRepo:
    def list_enabled(self, conn: sqlite3.Connection) -> List[dict]:
        rows = conn.execute(
            "SELECT * FROM rule WHERE enabled = 1 ORDER BY priority ASC"
        )
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["config"] = json.loads(d.get("config") or "{}")
            out.append(d)
        return out

    def list_all(self, conn: sqlite3.Connection) -> List[dict]:
        rows = conn.execute("SELECT * FROM rule ORDER BY priority ASC, id ASC")
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["config"] = json.loads(d.get("config") or "{}")
            out.append(d)
        return out

    def get(self, conn: sqlite3.Connection, rid: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM rule WHERE id = ?", (rid,)).fetchone()
        d = _row_to_dict(row)
        if d:
            d["config"] = json.loads(d.get("config") or "{}")
        return d

    def insert(self, conn: sqlite3.Connection, data: dict) -> int:
        cur = conn.execute(
            "INSERT INTO rule (name, rule_type, priority, stop_on_fail, config, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (data["name"], data["rule_type"], data.get("priority", 100),
             1 if data.get("stop_on_fail") else 0,
             json.dumps(data.get("config") or {}, ensure_ascii=False),
             1 if data.get("enabled", True) else 0),
        )
        return cur.lastrowid

    def update(self, conn: sqlite3.Connection, rid: int, data: dict) -> None:
        conn.execute(
            "UPDATE rule SET name = ?, rule_type = ?, priority = ?, stop_on_fail = ?, config = ?, enabled = ? WHERE id = ?",
            (data["name"], data["rule_type"], data.get("priority", 100),
             1 if data.get("stop_on_fail") else 0,
             json.dumps(data.get("config") or {}, ensure_ascii=False),
             1 if data.get("enabled", True) else 0, rid),
        )

    def delete(self, conn: sqlite3.Connection, rid: int) -> None:
        conn.execute("DELETE FROM rule WHERE id = ?", (rid,))


class CompanyRepo:
    def list(self, conn: sqlite3.Connection) -> List[dict]:
        return _rows_to_dicts(conn.execute(
            "SELECT * FROM company_profile ORDER BY is_default DESC, id ASC"
        ))

    def tax_nos(self, conn: sqlite3.Connection) -> set:
        rows = conn.execute("SELECT tax_no FROM company_profile")
        return {r["tax_no"].upper() for r in rows if r["tax_no"]}

    def insert(self, conn: sqlite3.Connection, name: str, tax_no: str,
               is_default: bool = False) -> int:
        if is_default:
            conn.execute("UPDATE company_profile SET is_default = 0")
        cur = conn.execute(
            "INSERT INTO company_profile (name, tax_no, is_default) VALUES (?, ?, ?)",
            (name, tax_no, 1 if is_default else 0),
        )
        return cur.lastrowid

    def delete(self, conn: sqlite3.Connection, cid: int) -> None:
        conn.execute("DELETE FROM company_profile WHERE id = ?", (cid,))


invoice_repo = InvoiceRepo()
reimburse_repo = ReimburseRepo()
match_repo = MatchRepo()
log_repo = LogRepo()
rule_repo = RuleRepo()
company_repo = CompanyRepo()


# =====================================================================
# 报销单模板（设计 §4.2 / §5）
# =====================================================================
class TemplateRepo:
    INSERT_FIELDS = [
        "enterprise_id", "user_id", "name", "description", "is_default",
        "is_system", "fields_config", "rules_config", "header_fingerprint",
    ]

    def insert(self, conn, data: dict) -> int:
        fields = [f for f in self.INSERT_FIELDS if f in data]
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO reimburse_template ({cols}) VALUES ({marks})",
            [self._serialize(data.get(f), f) for f in fields],
        )
        return cur.lastrowid

    def update(self, conn, template_id: int, data: dict) -> None:
        fields = [f for f in self.INSERT_FIELDS if f in data]
        if not fields:
            return
        sets = ", ".join(f"{f} = ?" for f in fields)
        params = [self._serialize(data.get(f), f) for f in fields]
        params.append(template_id)
        conn.execute(
            f"UPDATE reimburse_template SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
            params,
        )

    def get(self, conn, template_id: int) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM reimburse_template WHERE id = ? AND deleted_at IS NULL",
            (template_id,),
        ).fetchone()
        return self._to_dict(row)

    def get_default(self, conn, enterprise_id: int = None) -> Optional[dict]:
        row = conn.execute(
            """SELECT * FROM reimburse_template
               WHERE deleted_at IS NULL AND is_default = 1
                 AND (enterprise_id IS ? OR enterprise_id = ?)
               ORDER BY is_system DESC LIMIT 1""",
            (enterprise_id, enterprise_id),
        ).fetchone()
        return self._to_dict(row)

    def list(self, conn, enterprise_id: int = None) -> List[dict]:
        sql = "SELECT * FROM reimburse_template WHERE deleted_at IS NULL"
        params: list = []
        if enterprise_id is not None:
            sql += " AND (enterprise_id IS NULL OR enterprise_id = ?)"
            params.append(enterprise_id)
        sql += " ORDER BY is_system DESC, is_default DESC, id ASC"
        return [self._to_dict(r) for r in conn.execute(sql, params)]

    def soft_delete(self, conn, template_id: int) -> None:
        conn.execute(
            "UPDATE reimburse_template SET deleted_at = datetime('now','localtime') WHERE id = ?",
            (template_id,),
        )

    def clear_default(self, conn, enterprise_id: int = None) -> None:
        if enterprise_id is None:
            conn.execute("UPDATE reimburse_template SET is_default = 0 WHERE enterprise_id IS NULL")
        else:
            conn.execute("UPDATE reimburse_template SET is_default = 0 WHERE enterprise_id = ?", (enterprise_id,))

    def set_default(self, conn, template_id: int) -> None:
        conn.execute("UPDATE reimburse_template SET is_default = 1 WHERE id = ?", (template_id,))

    def bump_usage(self, conn, template_id: int) -> None:
        conn.execute(
            "UPDATE reimburse_template SET usage_count = usage_count + 1 WHERE id = ?",
            (template_id,),
        )

    @staticmethod
    def _serialize(v, field: str = ""):
        if field in ("fields_config", "rules_config"):
            return json.dumps(v or {}, ensure_ascii=False) if field == "fields_config" else json.dumps(v or [], ensure_ascii=False)
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    @staticmethod
    def _to_dict(row):
        d = _row_to_dict(row)
        if d:
            try:
                d["fields_config"] = json.loads(d.get("fields_config") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["fields_config"] = {}
            try:
                d["rules_config"] = json.loads(d.get("rules_config") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["rules_config"] = []
        return d


# =====================================================================
# 映射缓存（设计 §4.6）
# =====================================================================
class MappingCacheRepo:
    def get(self, conn, fingerprint: str, enterprise_id: int = None) -> Optional[dict]:
        row = conn.execute(
            """SELECT * FROM mapping_cache
               WHERE header_fingerprint = ? AND (enterprise_id IS ? OR enterprise_id = ?)
               LIMIT 1""",
            (fingerprint, enterprise_id, enterprise_id),
        ).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row)
        d["headers"] = json.loads(d.get("headers") or "[]")
        d["mapping"] = json.loads(d.get("mapping") or "{}")
        return d

    def upsert(self, conn, fingerprint: str, template_id: int, headers: list,
               mapping: dict, enterprise_id: int = None, user_id: int = None) -> None:
        conn.execute(
            """INSERT INTO mapping_cache
               (enterprise_id, user_id, header_fingerprint, template_id, headers, mapping)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(enterprise_id, header_fingerprint) DO UPDATE SET
                 template_id = excluded.template_id,
                 headers = excluded.headers,
                 mapping = excluded.mapping,
                 last_used_at = datetime('now','localtime'),
                 use_count = use_count + 1""",
            (enterprise_id, user_id, fingerprint, template_id,
             json.dumps(headers, ensure_ascii=False), json.dumps(mapping, ensure_ascii=False)),
        )

    def touch(self, conn, fingerprint: str, enterprise_id: int = None) -> None:
        conn.execute(
            """UPDATE mapping_cache
               SET last_used_at = datetime('now','localtime'), use_count = use_count + 1
               WHERE header_fingerprint = ? AND (enterprise_id IS ? OR enterprise_id = ?)""",
            (fingerprint, enterprise_id, enterprise_id),
        )


template_repo = TemplateRepo()
mapping_cache_repo = MappingCacheRepo()
