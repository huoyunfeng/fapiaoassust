"""仓储层 - 数据库连接与 Schema（唯一写库入口的一部分）。

DDL 与设计文档 §5 一致，含两处已标注的最小调整：
1. uq_invoice_code_no 增加 `AND status != 'DUPLICATE'`：
   设计的状态机要求 DUPLICATE 状态可存在且可人工恢复（§3.1），
   若按原索引，重复(代码,号码)记录根本无法落库，状态机中的
   DUPLICATE 将永远不可达。排除 DUPLICATE 后，不变量 I2 的意图
   （同一代码+号码只有一条"有效"发票）仍然成立。
2. MATCHED -> NORMAL 转移（见 states.py 注释）：删除报销单级联删除
   生效匹配（I8）后，发票必须能回到 NORMAL，否则永久卡死。
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import settings
from app.infra.logger import get_logger

logger = get_logger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS invoice (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash        TEXT NOT NULL,
    source_path      TEXT,
    file_type        TEXT NOT NULL CHECK(file_type IN ('pdf','ofd','xml','jpg','jpeg','png')),
    invoice_code     TEXT NOT NULL DEFAULT '',
    invoice_no       TEXT NOT NULL DEFAULT '',
    invoice_date     TEXT,
    buyer_name       TEXT NOT NULL DEFAULT '',
    buyer_tax_no     TEXT NOT NULL DEFAULT '',
    seller_name      TEXT NOT NULL DEFAULT '',
    seller_tax_no    TEXT NOT NULL DEFAULT '',
    amount           TEXT,
    tax_amount       TEXT,
    total_amount     TEXT,
    tax_rate         TEXT NOT NULL DEFAULT '',
    item_summary     TEXT NOT NULL DEFAULT '',
    check_code       TEXT NOT NULL DEFAULT '',
    raw_json         TEXT NOT NULL DEFAULT '{}',
    parse_engine     TEXT NOT NULL CHECK(parse_engine IN ('xml','ofd','pdf_text','ocr','manual')),
    parse_confidence REAL NOT NULL DEFAULT 0.0 CHECK(parse_confidence BETWEEN 0 AND 1),
    status           TEXT NOT NULL DEFAULT 'PARSING'
                     CHECK(status IN ('PARSING','PARSED','NORMAL','ABNORMAL','DUPLICATE','MATCHED')),
    is_red_flush     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_file_hash ON invoice(file_hash);

-- 不变量 I2（部分唯一索引；含上述调整 1）
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_code_no
    ON invoice(invoice_code, invoice_no)
    WHERE invoice_code != '' AND invoice_no != '' AND status != 'DUPLICATE';

CREATE INDEX IF NOT EXISTS idx_invoice_no        ON invoice(invoice_no);
CREATE INDEX IF NOT EXISTS idx_invoice_date      ON invoice(invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoice_buyer_tax ON invoice(buyer_tax_no);
CREATE INDEX IF NOT EXISTS idx_invoice_status    ON invoice(status);

CREATE TABLE IF NOT EXISTS reimburse (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL,
    reimburse_no   TEXT NOT NULL,
    submitter      TEXT NOT NULL DEFAULT '',
    reimburse_date TEXT,
    period_start   TEXT,
    period_end     TEXT,
    expense_type   TEXT NOT NULL DEFAULT '',
    amount         TEXT NOT NULL,
    invoice_no     TEXT NOT NULL DEFAULT '',
    invoice_date   TEXT,
    remark         TEXT NOT NULL DEFAULT '',
    raw_row        TEXT NOT NULL DEFAULT '{}',
    row_index      INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_reimburse_batch_no
    ON reimburse(batch_id, reimburse_no, row_index);

CREATE INDEX IF NOT EXISTS idx_reimburse_no     ON reimburse(reimburse_no);
CREATE INDEX IF NOT EXISTS idx_reimburse_amount ON reimburse(amount);

CREATE TABLE IF NOT EXISTS match_result (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL,
    reimburse_id INTEGER,
    match_type   TEXT NOT NULL
                 CHECK(match_type IN ('exact','composite','fuzzy','many_to_one','one_to_many','manual')),
    match_score  REAL NOT NULL DEFAULT 0.0 CHECK(match_score BETWEEN 0 AND 1),
    status       TEXT NOT NULL
                 CHECK(status IN ('PENDING','MATCHED','SUSPICIOUS','CONFLICT','UNMATCHED','CONFIRMED','REJECTED')),
    diff_fields  TEXT NOT NULL DEFAULT '[]',
    message      TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(invoice_id) REFERENCES invoice(id) ON DELETE CASCADE,
    FOREIGN KEY(reimburse_id) REFERENCES reimburse(id) ON DELETE CASCADE
);

-- 不变量 I3: 一张发票最多一条生效匹配
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_invoice_active
    ON match_result(invoice_id)
    WHERE status IN ('MATCHED','CONFIRMED');

-- 不变量 I4: 一条报销单最多一条生效匹配（汇总匹配经 match_pair 关联，
-- 其 match_result.reimburse_id 为 NULL，天然不受此索引约束）
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_reimburse_active
    ON match_result(reimburse_id)
    WHERE status IN ('MATCHED','CONFIRMED') AND reimburse_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS match_pair (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id     INTEGER NOT NULL,
    invoice_id   INTEGER NOT NULL,
    reimburse_id INTEGER NOT NULL,
    FOREIGN KEY(match_id) REFERENCES match_result(id) ON DELETE CASCADE,
    FOREIGN KEY(invoice_id) REFERENCES invoice(id) ON DELETE CASCADE,
    FOREIGN KEY(reimburse_id) REFERENCES reimburse(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_match_pair ON match_pair(match_id, invoice_id, reimburse_id);

CREATE TABLE IF NOT EXISTS invoice_status_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    operator    TEXT NOT NULL DEFAULT 'system',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(invoice_id) REFERENCES invoice(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rule (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    rule_type    TEXT NOT NULL CHECK(rule_type IN ('buyer_tax_no','period','amount','dedup','custom')),
    priority     INTEGER NOT NULL DEFAULT 100,
    stop_on_fail INTEGER NOT NULL DEFAULT 0,
    config       TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_rule_priority ON rule(enabled, priority);

CREATE TABLE IF NOT EXISTS company_profile (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    tax_no     TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_company_tax_no ON company_profile(tax_no);
"""


def connect() -> sqlite3.Connection:
    """打开数据库连接（每次独立连接，外键开启，行工厂 Row）。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """事务上下文（不变量 I9: 所有写操作在事务内）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """只读连接上下文。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    """建表（幂等）。应用启动时调用。"""
    with tx() as conn:
        conn.executescript(SCHEMA_SQL)
    logger.info("数据库 schema 初始化完成: %s", settings.db_path)
