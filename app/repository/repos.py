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

    @staticmethod
    def _filter_sql(status: str = None, keyword: str = None,
                    date_from: str = None, date_to: str = None,
                    exclude_status: tuple = None, since: str = None,
                    alias: str = "") -> tuple:
        """发票列表筛选条件片段（list/count/position_of 共用，保证口径一致）。

        since: 冷热分离的业务时间下界（开票日期，YYYY-MM-DD）。
               必须按业务发生时间过滤，绝不能按系统导入时间（否则补录历史发票会丢失）；
               无开票日期的行一律保留，避免解析缺失导致数据"消失"。
        alias: 表别名前缀（如 "i."），联表查询时避免与 match_result.status 等同名列冲突。
        """
        sql, params = "", []
        p = alias
        if status:
            sql += f" AND {p}status = ?"
            params.append(status)
        # 排除指定状态：仪表盘下钻需与统计口径一致（统计不计异常/重复发票）
        if exclude_status:
            marks = ", ".join("?" for _ in exclude_status)
            sql += f" AND {p}status NOT IN ({marks})"
            params += list(exclude_status)
        if keyword:
            sql += (f" AND ({p}invoice_no LIKE ? OR {p}invoice_code LIKE ?"
                    f" OR {p}buyer_name LIKE ? OR {p}seller_name LIKE ? OR {p}item_summary LIKE ?)")
            kw = f"%{keyword}%"
            params += [kw, kw, kw, kw, kw]
        # 开票日期范围（仪表盘下钻联动；口径与 aggregate_by_item_summary 一致）
        if date_from:
            sql += f" AND {p}invoice_date IS NOT NULL AND {p}invoice_date >= ?"
            params.append(date_from)
        if date_to:
            sql += f" AND {p}invoice_date IS NOT NULL AND {p}invoice_date <= ?"
            params.append(date_to)
        # 冷热分离：默认只取近 1 年（按开票日期）；无日期的行保留，防止补录/解析缺失数据丢失
        if since:
            sql += (f" AND ({p}invoice_date IS NULL OR {p}invoice_date = ''"
                    f" OR {p}invoice_date >= ?)")
            params.append(since)
        return sql, params

    # 费用归类所需的发票 + 报销单 + 人工决策联表（一次查询，避免逐票 N+1）
    _EXPENSE_SELECT = """
        SELECT i.id AS id, i.item_summary AS item_summary,
               i.total_amount AS total_amount, i.invoice_date AS invoice_date,
               i.status AS status,
               m.id AS match_id, m.status AS match_status, m.match_type AS match_type,
               r.id AS reimburse_id, r.reimburse_no AS reimburse_no,
               r.submitter AS submitter, r.expense_type AS expense_type,
               r.amount AS reimburse_amount,
               c.category AS manual_category, c.source AS manual_source,
               rc.category AS reimburse_manual_category,
               cf.invoice_category AS conflict_invoice_category,
               cf.reimburse_category AS conflict_reimburse_category,
               cf.resolution AS conflict_resolution
        FROM invoice i
        LEFT JOIN match_result m
               ON m.invoice_id = i.id AND m.status IN ('MATCHED','CONFIRMED')
        LEFT JOIN reimburse r
               ON r.id = COALESCE(m.reimburse_id,
                    (SELECT p.reimburse_id FROM match_pair p
                      WHERE p.match_id = m.id LIMIT 1))
        LEFT JOIN invoice_category c ON c.invoice_id = i.id
        LEFT JOIN reimburse_category rc ON rc.reimburse_id = r.id
        LEFT JOIN category_conflict cf ON cf.reimburse_id = r.id
        WHERE 1=1
    """

    def expense_rows(self, conn: sqlite3.Connection, date_from: str = None,
                     date_to: str = None, status: str = None, keyword: str = None,
                     ids: List[int] = None, exclude_status: tuple = None,
                     light: bool = False, since: str = None) -> List[dict]:
        """费用归类数据源：发票 + 生效匹配的报销单 + 人工分类决策。

        light=True 时只取归类必需的少量列（分类过滤用，10 万条量级省内存）。
        since: 冷热分离下界（按开票日期，无日期行保留）。
        """
        f_sql, params = self._filter_sql(status, keyword, date_from, date_to,
                                         exclude_status, since, alias="i.")
        if light:
            sql = ("SELECT i.id AS id, i.item_summary AS item_summary, "
                   "i.seller_name AS seller_name, "
                   "r.expense_type AS expense_type, r.submitter AS submitter, "
                   "r.id AS reimburse_id, "
                   "c.category AS manual_category, "
                   "rc.category AS reimburse_manual_category FROM invoice i "
                   "LEFT JOIN match_result m ON m.invoice_id = i.id AND m.status IN ('MATCHED','CONFIRMED') "
                   "LEFT JOIN reimburse r ON r.id = COALESCE(m.reimburse_id, "
                   "(SELECT p.reimburse_id FROM match_pair p WHERE p.match_id = m.id LIMIT 1)) "
                   "LEFT JOIN invoice_category c ON c.invoice_id = i.id "
                   "LEFT JOIN reimburse_category rc ON rc.reimburse_id = r.id WHERE 1=1" + f_sql)
        else:
            sql = self._EXPENSE_SELECT + f_sql
        if ids:
            marks = ", ".join("?" for _ in ids)
            sql += f" AND i.id IN ({marks})"
            params += list(ids)
        sql += " ORDER BY i.id ASC"
        return _rows_to_dicts(conn.execute(sql, params))

    def list(self, conn: sqlite3.Connection, status: str = None,
             keyword: str = None, limit: int = 500, offset: int = 0,
             ids: List[int] = None, date_from: str = None,
             date_to: str = None, exclude_status: tuple = None,
             since: str = None) -> List[dict]:
        sql, params = "SELECT * FROM invoice WHERE 1=1", []
        f_sql, f_params = self._filter_sql(status, keyword, date_from, date_to,
                                          exclude_status, since)
        sql += f_sql
        params += f_params
        if ids:
            marks = ", ".join("?" for _ in ids)
            sql += f" AND id IN ({marks})"
            params += ids
        sql += " ORDER BY id ASC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return _rows_to_dicts(conn.execute(sql, params))

    def list_id_summaries(self, conn: sqlite3.Connection, status: str = None,
                          keyword: str = None, date_from: str = None,
                          date_to: str = None,
                          exclude_status: tuple = None,
                          since: str = None) -> List[tuple]:
        """只取 [(id, item_summary)]（id 升序），供分类这类派生条件在内存过滤。

        分类 = 用户词典 + 智能归类，无法用 SQL 表达；只取两列可避免全字段全量加载。
        """
        sql, params = "SELECT id, item_summary FROM invoice WHERE 1=1", []
        f_sql, f_params = self._filter_sql(status, keyword, date_from, date_to,
                                          exclude_status, since)
        sql += f_sql + " ORDER BY id ASC"
        params += f_params
        return [(r["id"], r["item_summary"]) for r in conn.execute(sql, params)]

    def count(self, conn: sqlite3.Connection, status: str = None,
              keyword: str = None, date_from: str = None,
              date_to: str = None, exclude_status: tuple = None,
              since: str = None) -> int:
        """发票总数（与 list 同筛选口径，分页用）。"""
        sql, params = "SELECT COUNT(*) AS n FROM invoice WHERE 1=1", []
        f_sql, f_params = self._filter_sql(status, keyword, date_from, date_to,
                                          exclude_status, since)
        sql += f_sql
        params += f_params
        return conn.execute(sql, params).fetchone()["n"]

    def position_of(self, conn: sqlite3.Connection, invoice_id: int,
                    status: str = None, keyword: str = None,
                    date_from: str = None, date_to: str = None,
                    exclude_status: tuple = None, since: str = None) -> Optional[int]:
        """按 id ASC 排序时 invoice_id 之前的行数（用于计算 focus 行所在页码）。"""
        sql, params = "SELECT COUNT(*) AS n FROM invoice WHERE id < ?", [invoice_id]
        f_sql, f_params = self._filter_sql(status, keyword, date_from, date_to,
                                          exclude_status, since)
        sql += f_sql
        params += f_params
        return conn.execute(sql, params).fetchone()["n"]

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

    def rename_item_summary(self, conn: sqlite3.Connection, old: str, new: str) -> int:
        """把库里所有同名报销内容批量改名（用户词典学习时同步历史数据）。"""
        cur = conn.execute(
            "UPDATE invoice SET item_summary = ? WHERE item_summary = ?", (new, old))
        return cur.rowcount

    def count_by_status(self, conn: sqlite3.Connection) -> Dict[str, int]:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM invoice GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def aggregate_by_item_summary(
        self, conn: sqlite3.Connection,
        date_from: str = None, date_to: str = None,
    ) -> List[dict]:
        """按 item_summary 分组聚合价税合计/笔数，返回 [{'name','amount','count'}...]。

        过滤口径：
        - 排除 ABNORMAL / DUPLICATE 状态（异常/重复发票不计入费用统计）
        - 时间筛选基于 invoice_date（开票日期）
        - item_summary 为空或 NULL 统一归类为 "其他"
        """
        sql = (
            "SELECT COALESCE(NULLIF(TRIM(item_summary),''), '其他') AS name, "
            "SUM(CAST(total_amount AS REAL)) AS amount, COUNT(*) AS count "
            "FROM invoice WHERE status NOT IN ('ABNORMAL','DUPLICATE')"
        )
        params: list = []
        if date_from:
            sql += " AND invoice_date IS NOT NULL AND invoice_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND invoice_date IS NOT NULL AND invoice_date <= ?"
            params.append(date_to)
        sql += " GROUP BY name ORDER BY amount DESC"
        cur = conn.execute(sql, params)
        return _rows_to_dicts(cur)

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
        "split_type", "raw_row", "row_index", "dup_flag", "dup_source",
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
             keyword: str = None, limit: int = 500, offset: int = 0,
             since: str = None) -> List[dict]:
        """报销单列表。since: 冷热分离下界（按报销日期，无日期行保留）。"""
        sql, params = "SELECT * FROM reimburse WHERE 1=1", []
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        if keyword:
            sql += " AND (reimburse_no LIKE ? OR submitter LIKE ? OR expense_type LIKE ? OR remark LIKE ?)"
            kw = f"%{keyword}%"
            params += [kw, kw, kw, kw]
        if since:
            sql += (" AND (reimburse_date IS NULL OR reimburse_date = ''"
                    " OR reimburse_date >= ?)")
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._to_dict(r) for r in conn.execute(sql, params)]

    def batches(self, conn: sqlite3.Connection) -> List[dict]:
        """批次聚合：行数 + 未匹配数（未匹配 = 无生效匹配，口径与列表页 matched 一致）。"""
        return _rows_to_dicts(conn.execute(
            f"""SELECT r.batch_id, COUNT(*) AS row_count, MIN(r.created_at) AS created_at,
                      t.name AS template_name,
                      SUM(CASE WHEN ar.reimburse_id IS NULL THEN 1 ELSE 0 END) AS unmatched_count
               FROM reimburse r
               LEFT JOIN reimburse_template t ON t.id = r.template_id
               LEFT JOIN (
                   SELECT DISTINCT reimburse_id FROM (
                       SELECT reimburse_id FROM match_result
                        WHERE {ACTIVE_SQL} AND reimburse_id IS NOT NULL
                       UNION
                       SELECT p.reimburse_id FROM match_pair p
                           JOIN match_result m ON m.id = p.match_id AND m.{ACTIVE_SQL}
                   )
               ) ar ON ar.reimburse_id = r.id
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

    def aggregate_by_expense_type(
        self, conn: sqlite3.Connection,
        date_from: str = None, date_to: str = None,
    ) -> List[dict]:
        """按 expense_type 分组聚合金额/笔数，返回 [{'name','amount','count'}...]。

        过滤口径：reimburse_date BETWEEN date_from AND date_to（含边界，含当天）；
        当 date_from/date_to 任一传入时，reimburse_date 为 NULL 的记录不计入。
        """
        sql = (
            "SELECT COALESCE(NULLIF(expense_type,''), '未分类') AS name, "
            "SUM(CAST(amount AS REAL)) AS amount, COUNT(*) AS count "
            "FROM reimburse WHERE 1=1"
        )
        params: list = []
        if date_from:
            sql += " AND reimburse_date IS NOT NULL AND reimburse_date >= ?"
            params.append(date_from)
        if date_to:
            # 结束日期含当天全部：用户传 '2026-10-31' 则按前缀匹配 YYYY-MM-DD 或 datetime
            sql += " AND reimburse_date IS NOT NULL AND reimburse_date <= ?"
            params.append(date_to)
        sql += " GROUP BY name ORDER BY amount DESC"
        cur = conn.execute(sql, params)
        return _rows_to_dicts(cur)


# =====================================================================
# 匹配
# =====================================================================
class MatchRepo:
    def insert(self, conn: sqlite3.Connection, data: dict) -> int:
        cur = conn.execute(
            """INSERT INTO match_result
               (invoice_id, reimburse_id, match_type, match_score, status,
                diff_fields, match_points, message, warning_msg, detail,
                confirmed_by, confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["invoice_id"], data.get("reimburse_id"), data["match_type"],
                data.get("match_score", 0.0), data["status"],
                json.dumps(data.get("diff_fields", []), ensure_ascii=False),
                json.dumps(data.get("match_points", []), ensure_ascii=False),
                data.get("message", ""),
                data.get("warning_msg", ""),
                json.dumps(data.get("detail") or {}, ensure_ascii=False),
                data.get("confirmed_by"),
                data.get("confirmed_at"),
            ),
        )
        return cur.lastrowid

    def update_detail(self, conn: sqlite3.Connection, mid: int,
                      detail: dict, message: str = None) -> None:
        """写入人工干预/快捷修复的详细依据（detail JSON，与 audit_log.detail 同源）。"""
        if message is None:
            conn.execute(
                "UPDATE match_result SET detail = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (json.dumps(detail or {}, ensure_ascii=False), mid),
            )
        else:
            conn.execute(
                """UPDATE match_result SET detail = ?, message = ?,
                   updated_at = datetime('now','localtime') WHERE id = ?""",
                (json.dumps(detail or {}, ensure_ascii=False), message, mid),
            )

    def update_detail_by_invoice(self, conn: sqlite3.Connection, invoice_id: int,
                                 detail: dict, message: str = None) -> None:
        """按发票定位最新一条匹配记录并写入 detail。"""
        row = conn.execute(
            "SELECT id FROM match_result WHERE invoice_id = ? ORDER BY id DESC LIMIT 1",
            (invoice_id,)).fetchone()
        if row:
            self.update_detail(conn, row["id"], detail, message)

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
            d["match_points"] = json.loads(d.get("match_points") or "[]")
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

    def active_invoice_ids(self, conn: sqlite3.Connection) -> set:
        """所有已占用（生效匹配）的发票 id，含多对一汇总匹配（一次查询，替代逐票查询）。"""
        rows = conn.execute(
            f"""SELECT DISTINCT invoice_id FROM (
                    SELECT invoice_id FROM match_result WHERE {ACTIVE_SQL} AND invoice_id IS NOT NULL
                    UNION
                    SELECT p.invoice_id FROM match_pair p
                        JOIN match_result m ON m.id = p.match_id AND m.{ACTIVE_SQL}
                )"""
        )
        return {r["invoice_id"] for r in rows if r["invoice_id"] is not None}

    def invoice_summaries_for_reimburses(self, conn: sqlite3.Connection,
                                         reimburse_ids: List[int]) -> Dict[int, str]:
        """报销单 -> 已匹配发票的报销内容（费用类型为空时用于智能推测）。"""
        if not reimburse_ids:
            return {}
        marks = ", ".join("?" for _ in reimburse_ids)
        rows = conn.execute(
            f"""SELECT x.reimburse_id AS rid, i.item_summary AS item_summary
                FROM (
                    SELECT reimburse_id, invoice_id FROM match_result
                     WHERE {ACTIVE_SQL} AND reimburse_id IN ({marks})
                    UNION
                    SELECT p.reimburse_id AS reimburse_id, p.invoice_id AS invoice_id
                      FROM match_pair p JOIN match_result m ON m.id = p.match_id
                     WHERE m.{ACTIVE_SQL} AND p.reimburse_id IN ({marks})
                ) x
                JOIN invoice i ON i.id = x.invoice_id""",
            list(reimburse_ids) * 2)
        out: Dict[int, str] = {}
        for r in rows:
            out.setdefault(r["rid"], r["item_summary"] or "")
        return out

    def matched_invoice_ids(self, conn: sqlite3.Connection,
                            reimburse_ids: List[int]) -> Dict[int, List[int]]:
        """报销单 -> 已匹配发票 id 列表（费用类型与发票分类联动时使用）。"""
        if not reimburse_ids:
            return {}
        marks = ", ".join("?" for _ in reimburse_ids)
        rows = conn.execute(
            f"""SELECT DISTINCT reimburse_id, invoice_id FROM (
                    SELECT reimburse_id, invoice_id FROM match_result
                     WHERE {ACTIVE_SQL} AND reimburse_id IN ({marks})
                    UNION
                    SELECT p.reimburse_id AS reimburse_id, p.invoice_id AS invoice_id
                      FROM match_pair p JOIN match_result m ON m.id = p.match_id
                     WHERE m.{ACTIVE_SQL} AND p.reimburse_id IN ({marks})
                ) ORDER BY reimburse_id, invoice_id""",
            list(reimburse_ids) * 2)
        out: Dict[int, List[int]] = {}
        for r in rows:
            if r["reimburse_id"] is None or r["invoice_id"] is None:
                continue
            out.setdefault(r["reimburse_id"], []).append(r["invoice_id"])
        return out

    def list(self, conn: sqlite3.Connection, statuses: List[str] = None,
             limit: int = 500) -> List[dict]:
        sql = """SELECT m.*, i.invoice_no, i.invoice_date, i.total_amount,
                        i.buyer_name, i.seller_name, i.status AS invoice_status,
                        i.item_summary, i.raw_json,
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
            d["match_points"] = json.loads(d.get("match_points") or "[]")
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

    def delete_for_invoice(self, conn: sqlite3.Connection, invoice_id: int) -> int:
        """显式清理某发票的全部匹配记录（match_pair + match_result）。

        与表结构 ON DELETE CASCADE 双保险：即使外键未生效也不留悬空脏数据。
        返回删除的 match_result 行数。
        """
        conn.execute(
            "DELETE FROM match_pair WHERE match_id IN "
            "(SELECT id FROM match_result WHERE invoice_id = ?)",
            (invoice_id,),
        )
        conn.execute("DELETE FROM match_pair WHERE invoice_id = ?", (invoice_id,))
        cur = conn.execute("DELETE FROM match_result WHERE invoice_id = ?", (invoice_id,))
        return cur.rowcount

    def delete_for_reimburse(self, conn: sqlite3.Connection, reimburse_id: int) -> int:
        """显式清理某报销单的全部匹配记录（match_pair + match_result 直接引用）。"""
        conn.execute(
            "DELETE FROM match_pair WHERE match_id IN "
            "(SELECT id FROM match_result WHERE reimburse_id = ?)",
            (reimburse_id,),
        )
        conn.execute("DELETE FROM match_pair WHERE reimburse_id = ?", (reimburse_id,))
        cur = conn.execute("DELETE FROM match_result WHERE reimburse_id = ?", (reimburse_id,))
        return cur.rowcount

    def delete_unpaired_summary(self, conn: sqlite3.Connection) -> int:
        """清理孤儿的汇总匹配：match_pair 已删空、reimburse_id 为 NULL 的 match_result。"""
        cur = conn.execute(
            """DELETE FROM match_result
               WHERE reimburse_id IS NULL
                 AND match_type IN ('many_to_one','one_to_many')
                 AND NOT EXISTS (SELECT 1 FROM match_pair p WHERE p.match_id = match_result.id)"""
        )
        return cur.rowcount

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

    def delete_for_invoice(self, conn: sqlite3.Connection, invoice_id: int) -> int:
        """显式清理某发票的状态日志（外键级联的双保险）。"""
        cur = conn.execute("DELETE FROM invoice_status_log WHERE invoice_id = ?", (invoice_id,))
        return cur.rowcount


# =====================================================================
# 规则 / 公司抬头
# =====================================================================
class RuleRepo:
    def _decode(self, r: sqlite3.Row) -> dict:
        d = _row_to_dict(r)
        d["config"] = json.loads(d.get("config") or "{}")
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["stop_on_fail"] = bool(d.get("stop_on_fail"))
        d["enabled"] = bool(d.get("enabled"))
        d["is_core"] = bool(d.get("is_core"))
        return d

    def list_enabled(self, conn: sqlite3.Connection) -> List[dict]:
        rows = conn.execute(
            "SELECT * FROM rule WHERE enabled = 1 ORDER BY priority ASC, id ASC"
        )
        return [self._decode(r) for r in rows]

    def list_all(self, conn: sqlite3.Connection) -> List[dict]:
        rows = conn.execute("SELECT * FROM rule ORDER BY priority ASC, id ASC")
        return [self._decode(r) for r in rows]

    def get(self, conn: sqlite3.Connection, rid: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM rule WHERE id = ?", (rid,)).fetchone()
        return self._decode(row) if row else None

    def insert(self, conn: sqlite3.Connection, data: dict) -> int:
        cur = conn.execute(
            """INSERT INTO rule
               (name, rule_type, scope, action, priority, stop_on_fail,
                config, tags, is_core, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data["name"], data["rule_type"],
             data.get("scope") or "invoice", data.get("action") or "block",
             data.get("priority", 100),
             1 if data.get("stop_on_fail") else 0,
             json.dumps(data.get("config") or {}, ensure_ascii=False),
             json.dumps(data.get("tags") or [], ensure_ascii=False),
             1 if data.get("is_core") else 0,
             1 if data.get("enabled", True) else 0),
        )
        return cur.lastrowid

    def update(self, conn: sqlite3.Connection, rid: int, data: dict) -> None:
        conn.execute(
            """UPDATE rule SET name = ?, rule_type = ?, scope = ?, action = ?,
               priority = ?, stop_on_fail = ?, config = ?, tags = ?, is_core = ?,
               enabled = ? WHERE id = ?""",
            (data["name"], data["rule_type"],
             data.get("scope") or "invoice", data.get("action") or "block",
             data.get("priority", 100),
             1 if data.get("stop_on_fail") else 0,
             json.dumps(data.get("config") or {}, ensure_ascii=False),
             json.dumps(data.get("tags") or [], ensure_ascii=False),
             1 if data.get("is_core") else 0,
             1 if data.get("enabled", True) else 0, rid),
        )

    def next_priority(self, conn: sqlite3.Connection) -> int:
        """自定义规则的下一个优先级：按 10/20/30… 递增（核心规则 1~4 不参与）。"""
        row = conn.execute(
            "SELECT MAX(priority) AS p FROM rule WHERE is_core = 0").fetchone()
        return int((row["p"] or 0) + 10)

    def reorder(self, conn: sqlite3.Connection, ids: List[int]) -> None:
        """按传入顺序重排自定义规则优先级（10/20/30…）。"""
        for idx, rid in enumerate(ids):
            conn.execute("UPDATE rule SET priority = ? WHERE id = ? AND is_core = 0",
                         ((idx + 1) * 10, rid))

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


# =====================================================================
# 报销内容自定义词典（用户改名记忆）
# =====================================================================
class AliasRepo:
    """item_alias 表：原始报销内容 -> 用户改名。"""

    def get(self, conn: sqlite3.Connection, raw_text: str) -> Optional[str]:
        row = conn.execute(
            "SELECT alias FROM item_alias WHERE raw_text = ?", (raw_text,)).fetchone()
        return row["alias"] if row else None

    def upsert(self, conn: sqlite3.Connection, raw_text: str, alias: str) -> None:
        conn.execute(
            """INSERT INTO item_alias (raw_text, alias) VALUES (?, ?)
               ON CONFLICT(raw_text) DO UPDATE SET alias = excluded.alias,
               updated_at = datetime('now','localtime')""",
            (raw_text, alias),
        )

    def all(self, conn: sqlite3.Connection) -> Dict[str, str]:
        rows = conn.execute("SELECT raw_text, alias FROM item_alias")
        return {r["raw_text"]: r["alias"] for r in rows}


# =====================================================================
# 费用分类决策 + 纠正记忆
# =====================================================================
class CategoryRepo:
    """invoice_category（人工决策）与 category_memory（纠正记忆）。"""

    def get(self, conn: sqlite3.Connection, invoice_id: int) -> Optional[dict]:
        return _row_to_dict(conn.execute(
            "SELECT * FROM invoice_category WHERE invoice_id = ?", (invoice_id,)).fetchone())

    def decisions(self, conn: sqlite3.Connection, invoice_ids: List[int] = None) -> Dict[int, dict]:
        """发票 -> 人工分类决策（可按 ids 过滤）。"""
        sql, params = "SELECT * FROM invoice_category", []
        if invoice_ids:
            marks = ", ".join("?" for _ in invoice_ids)
            sql += f" WHERE invoice_id IN ({marks})"
            params += list(invoice_ids)
        return {r["invoice_id"]: _row_to_dict(r) for r in conn.execute(sql, params)}

    def upsert(self, conn: sqlite3.Connection, invoice_id: int, category: str,
               source: str = "manual") -> None:
        conn.execute(
            """INSERT INTO invoice_category (invoice_id, category, source)
               VALUES (?, ?, ?)
               ON CONFLICT(invoice_id) DO UPDATE SET category = excluded.category,
               source = excluded.source, updated_at = datetime('now','localtime')""",
            (invoice_id, category, source),
        )

    def delete(self, conn: sqlite3.Connection, invoice_id: int) -> None:
        conn.execute("DELETE FROM invoice_category WHERE invoice_id = ?", (invoice_id,))

    def memory_all(self, conn: sqlite3.Connection) -> Dict[tuple, dict]:
        """纠正记忆：{(kind, key) -> {wrong_value, category}}。"""
        rows = conn.execute(
            "SELECT kind, key, wrong_value, category FROM category_memory")
        return {(r["kind"], r["key"]): _row_to_dict(r) for r in rows}

    def memory_upsert(self, conn: sqlite3.Connection, kind: str, key: str,
                      wrong_value: str, category: str) -> None:
        conn.execute(
            """INSERT INTO category_memory (kind, key, wrong_value, category)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET wrong_value = excluded.wrong_value,
               category = excluded.category, updated_at = datetime('now','localtime')""",
            (kind, key, wrong_value or "", category),
        )

    def rename_values(self, conn: sqlite3.Connection, old: str, new: str) -> int:
        """把历史数据里等于 old 的分类统一改为 new（字典「合并」用），返回影响行数。"""
        n = 0
        for sql in (
            "UPDATE invoice_category SET category = ? WHERE category = ?",
            "UPDATE reimburse_category SET category = ? WHERE category = ?",
        ):
            n += conn.execute(sql, (new, old)).rowcount
        n += conn.execute(
            "UPDATE category_memory SET category = ? WHERE category = ?", (new, old)).rowcount
        return n

    def usage_count(self, conn: sqlite3.Connection, name: str) -> int:
        """分类名被历史数据使用的次数（发票决策 + 报销单决策 + 纠正记忆）。"""
        n = 0
        for table in ("invoice_category", "reimburse_category", "category_memory"):
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE category = ?", (name,)).fetchone()
            n += int(row["c"] if row else 0)
        return n


class CategoryDictRepo:
    """费用分类字典：只允许禁用/启用，禁止物理删除。"""

    def list(self, conn: sqlite3.Connection, enabled_only: bool = False) -> List[dict]:
        sql = "SELECT * FROM category_dict"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY sort_order, id"
        return _rows_to_dicts(conn.execute(sql))

    def names(self, conn: sqlite3.Connection, enabled_only: bool = True) -> List[str]:
        sql = "SELECT name FROM category_dict"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY sort_order, id"
        return [r["name"] for r in conn.execute(sql)]

    def get(self, conn: sqlite3.Connection, cid: int) -> Optional[dict]:
        return _row_to_dict(conn.execute(
            "SELECT * FROM category_dict WHERE id = ?", (cid,)).fetchone())

    def get_by_name(self, conn: sqlite3.Connection, name: str) -> Optional[dict]:
        return _row_to_dict(conn.execute(
            "SELECT * FROM category_dict WHERE name = ?", (name,)).fetchone())

    def insert(self, conn: sqlite3.Connection, name: str,
               is_builtin: int = 0, sort_order: int = 0) -> int:
        cur = conn.execute(
            "INSERT INTO category_dict (name, is_builtin, sort_order) VALUES (?, ?, ?)",
            (name, is_builtin, sort_order))
        return cur.lastrowid

    def set_enabled(self, conn: sqlite3.Connection, cid: int, enabled: bool) -> None:
        conn.execute(
            "UPDATE category_dict SET enabled = ?, updated_at = datetime('now','localtime') "
            "WHERE id = ?", (1 if enabled else 0, cid))

    def rename(self, conn: sqlite3.Connection, cid: int, new_name: str) -> None:
        conn.execute(
            "UPDATE category_dict SET name = ?, updated_at = datetime('now','localtime') "
            "WHERE id = ?", (new_name, cid))

    def max_sort(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM category_dict").fetchone()
        return int(row["m"])


class ReimburseCategoryRepo:
    """报销单侧分类决策（与发票侧隔离存储）。"""

    def get(self, conn: sqlite3.Connection, reimburse_id: int) -> Optional[dict]:
        return _row_to_dict(conn.execute(
            "SELECT * FROM reimburse_category WHERE reimburse_id = ?",
            (reimburse_id,)).fetchone())

    def decisions(self, conn: sqlite3.Connection,
                  reimburse_ids: List[int] = None) -> Dict[int, dict]:
        sql, params = "SELECT * FROM reimburse_category", []
        if reimburse_ids:
            marks = ", ".join("?" for _ in reimburse_ids)
            sql += f" WHERE reimburse_id IN ({marks})"
            params += list(reimburse_ids)
        return {r["reimburse_id"]: _row_to_dict(r) for r in conn.execute(sql, params)}

    def upsert(self, conn: sqlite3.Connection, reimburse_id: int, category: str,
               source: str = "manual") -> None:
        conn.execute(
            """INSERT INTO reimburse_category (reimburse_id, category, source)
               VALUES (?, ?, ?)
               ON CONFLICT(reimburse_id) DO UPDATE SET category = excluded.category,
               source = excluded.source, updated_at = datetime('now','localtime')""",
            (reimburse_id, category, source))

    def delete(self, conn: sqlite3.Connection, reimburse_id: int) -> None:
        conn.execute("DELETE FROM reimburse_category WHERE reimburse_id = ?",
                     (reimburse_id,))


class ConflictRepo:
    """已匹配报销单的分类冲突（resolution 为空 = 待处理）。"""

    def get(self, conn: sqlite3.Connection, reimburse_id: int) -> Optional[dict]:
        return _row_to_dict(conn.execute(
            "SELECT * FROM category_conflict WHERE reimburse_id = ?",
            (reimburse_id,)).fetchone())

    def all(self, conn: sqlite3.Connection) -> Dict[int, dict]:
        rows = conn.execute("SELECT * FROM category_conflict")
        return {r["reimburse_id"]: _row_to_dict(r) for r in rows}

    def upsert(self, conn: sqlite3.Connection, reimburse_id: int,
               invoice_category: str, reimburse_category: str,
               detail: str = "") -> None:
        conn.execute(
            """INSERT INTO category_conflict
                   (reimburse_id, invoice_category, reimburse_category, detail)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(reimburse_id) DO UPDATE SET
                   invoice_category = excluded.invoice_category,
                   reimburse_category = excluded.reimburse_category,
                   detail = excluded.detail,
                   resolution = '', resolved_at = NULL,
                   detected_at = datetime('now','localtime')""",
            (reimburse_id, invoice_category, reimburse_category, detail))

    def set_resolution(self, conn: sqlite3.Connection, reimburse_id: int,
                       resolution: str) -> None:
        conn.execute(
            "UPDATE category_conflict SET resolution = ?, "
            "resolved_at = datetime('now','localtime') WHERE reimburse_id = ?",
            (resolution, reimburse_id))

    def delete(self, conn: sqlite3.Connection, reimburse_id: int) -> None:
        conn.execute("DELETE FROM category_conflict WHERE reimburse_id = ?",
                     (reimburse_id,))

    def count_unresolved(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM category_conflict WHERE resolution = ''").fetchone()
        return int(row["c"])


class AuditRepo:
    """全量审计日志：字典维护、分类修改、批量修改、仲裁、解除匹配。"""

    def log(self, conn: sqlite3.Connection, action: str, target_type: str,
            target_id: str = "", target_label: str = "", old_value: str = "",
            new_value: str = "", operator: str = "admin", detail: str = "") -> int:
        cur = conn.execute(
            """INSERT INTO audit_log
                   (action, target_type, target_id, target_label,
                    old_value, new_value, operator, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (action, target_type, str(target_id), target_label,
             old_value or "", new_value or "", operator or "admin", detail or ""))
        return cur.lastrowid

    def list(self, conn: sqlite3.Connection, limit: int = 100,
             target_type: str = None, action: str = None,
             keyword: str = None, date_from: str = None, date_to: str = None,
             page: int = 1, page_size: int = 20) -> dict:
        """分页 + 多维度筛选，返回 {items, total, page, page_size}。

        兼容旧签名：limit 仅作为无 page 参数时的简易上限，
        有 page 时以 page_size 为准。
        """
        conds: list = []
        params: list = []
        if target_type:
            conds.append("target_type = ?")
            params.append(target_type)
        if action:
            conds.append("action = ?")
            params.append(action)
        if keyword:
            conds.append("(operator LIKE ? OR target_label LIKE ? OR detail LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        if date_from:
            conds.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conds.append("created_at <= ?")
            params.append(date_to + " 23:59:59")

        where = (" WHERE " + " AND ".join(conds)) if conds else ""

        total_sql = f"SELECT COUNT(*) AS c FROM audit_log{where}"
        total = conn.execute(total_sql, params).fetchone()["c"]

        use_page = page and page >= 1
        if use_page:
            ps = max(1, min(500, page_size or 20))
            offset = (page - 1) * ps
            sql = f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
            rows = _rows_to_dicts(conn.execute(sql, params + [ps, offset]))
            return {"items": rows, "total": total, "page": page,
                    "page_size": ps, "pages": max(1, (total + ps - 1) // ps)}
        else:
            sql = f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?"
            rows = _rows_to_dicts(conn.execute(sql, params + [limit]))
            return {"items": rows, "total": total, "page": 1,
                    "page_size": limit, "pages": 1}


invoice_repo = InvoiceRepo()
reimburse_repo = ReimburseRepo()
match_repo = MatchRepo()
log_repo = LogRepo()
rule_repo = RuleRepo()
company_repo = CompanyRepo()
alias_repo = AliasRepo()
category_repo = CategoryRepo()
category_dict_repo = CategoryDictRepo()
reimburse_category_repo = ReimburseCategoryRepo()
conflict_repo = ConflictRepo()
audit_repo = AuditRepo()


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
