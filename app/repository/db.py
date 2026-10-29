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
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from app.config import settings
from app.infra.logger import get_logger

logger = get_logger(__name__)

# 活跃连接注册表（sqlite3.Connection 不支持弱引用，用普通集合 + 关闭时移除）：
# 供 dispose() 在数据恢复前强制断连
_open_conns: "set[sqlite3.Connection]" = set()
_open_conns_lock = threading.Lock()

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

-- 报销内容自定义词典：用户手动改名记忆（原始词 -> 新词）
CREATE TABLE IF NOT EXISTS item_alias (
    raw_text   TEXT PRIMARY KEY,
    alias      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 人工费用分类决策：财务确认/修正后的发票分类（只记录人工决策，系统推测不落库）
-- source: manual = 财务在列表里确认或修改
CREATE TABLE IF NOT EXISTS invoice_category (
    invoice_id INTEGER PRIMARY KEY,
    category   TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(invoice_id) REFERENCES invoice(id) ON DELETE CASCADE
);

-- 分类纠正记忆：kind=submitter 按提交人、kind=item 按发票明细
-- wrong_value 记录纠正前的值（员工误填的费用类型或系统推测值），为空表示"当时没填"
CREATE TABLE IF NOT EXISTS category_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK(kind IN ('submitter','item')),
    key         TEXT NOT NULL,
    wrong_value TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_category_memory
    ON category_memory(kind, key);

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
    template_id    INTEGER,
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
    extra_data     TEXT NOT NULL DEFAULT '{}',
    source_row     INTEGER,
    parent_id      INTEGER,
    split_type     TEXT NOT NULL DEFAULT 'none',
    raw_row        TEXT NOT NULL DEFAULT '{}',
    row_index      INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_reimburse_batch_no
    ON reimburse(batch_id, reimburse_no, row_index);

CREATE INDEX IF NOT EXISTS idx_reimburse_no     ON reimburse(reimburse_no);
CREATE INDEX IF NOT EXISTS idx_reimburse_amount ON reimburse(amount);

-- 报销单模板表（设计 §4.2）
CREATE TABLE IF NOT EXISTS reimburse_template (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_id     INTEGER,
    user_id           INTEGER,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    is_default        INTEGER NOT NULL DEFAULT 0,
    is_system         INTEGER NOT NULL DEFAULT 0,
    fields_config     TEXT NOT NULL DEFAULT '{}',
    rules_config      TEXT NOT NULL DEFAULT '[]',
    header_fingerprint TEXT,
    usage_count       INTEGER NOT NULL DEFAULT 0,
    deleted_at        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_template_enterprise ON reimburse_template(enterprise_id);
CREATE INDEX IF NOT EXISTS idx_template_fingerprint ON reimburse_template(header_fingerprint);

-- 映射缓存表（设计 §4.6）：记住"表头 → 字段"的映射关系，下次自动套用
CREATE TABLE IF NOT EXISTS mapping_cache (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_id      INTEGER,
    user_id            INTEGER,
    header_fingerprint TEXT NOT NULL,
    template_id        INTEGER NOT NULL,
    headers            TEXT NOT NULL,
    mapping            TEXT NOT NULL DEFAULT '{}',
    last_used_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    use_count          INTEGER NOT NULL DEFAULT 1,
    UNIQUE(enterprise_id, header_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_mapping_fingerprint ON mapping_cache(header_fingerprint);

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
    match_points TEXT NOT NULL DEFAULT '[]',
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

-- 费用分类字典：分类只能禁用、不能物理删除（禁用后新建时隐藏，历史数据正常显示）
CREATE TABLE IF NOT EXISTS category_dict (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 报销单侧分类决策：与发票侧相互独立（未匹配时互不影响）
CREATE TABLE IF NOT EXISTS reimburse_category (
    reimburse_id INTEGER PRIMARY KEY,
    category     TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(reimburse_id) REFERENCES reimburse(id) ON DELETE CASCADE
);

-- 已匹配双方的分类冲突：resolution 为空 = 待处理，keep = 保留差异
CREATE TABLE IF NOT EXISTS category_conflict (
    reimburse_id       INTEGER PRIMARY KEY,
    invoice_category   TEXT NOT NULL DEFAULT '',
    reimburse_category TEXT NOT NULL DEFAULT '',
    detail             TEXT NOT NULL DEFAULT '',
    resolution         TEXT NOT NULL DEFAULT ''
                       CHECK(resolution IN ('','reimburse','invoice','keep')),
    detected_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_at        TEXT,
    FOREIGN KEY(reimburse_id) REFERENCES reimburse(id) ON DELETE CASCADE
);

-- 全量审计日志：字典维护、单条/批量修改、冲突仲裁、解除匹配
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    TEXT NOT NULL DEFAULT '',
    target_label TEXT NOT NULL DEFAULT '',
    old_value    TEXT NOT NULL DEFAULT '',
    new_value    TEXT NOT NULL DEFAULT '',
    operator     TEXT NOT NULL DEFAULT 'admin',
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

-- 轻量 KV：存自增序列等全局状态（reimburse_service 的 BX-* 自增等）
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect() -> sqlite3.Connection:
    """打开数据库连接（每次独立连接，外键开启，行工厂 Row）。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 同步级别默认 FULL（断电安全）；受限环境可经 FAPIAO_SYNC 降级（见 config.py 注释）
    if settings.db_synchronous in ("FULL", "NORMAL", "OFF"):
        conn.execute(f"PRAGMA synchronous = {settings.db_synchronous}")
    with _open_conns_lock:
        _open_conns.add(conn)
    return conn


def dispose() -> int:
    """强制关闭所有活跃数据库连接，返回关闭数量。

    数据恢复（覆盖 .db 文件）前必须调用：Windows 下打开的连接会锁定
    数据库文件，导致重命名/覆盖失败。本架构连接为按调用创建、用完即关，
    正常情况此处为 0；异常残留的连接在此被兜底强制释放。
    """
    with _open_conns_lock:
        conns = list(_open_conns)
    n = 0
    for conn in conns:
        try:
            conn.close()
            n += 1
        except Exception:
            pass
        finally:
            with _open_conns_lock:
                _open_conns.discard(conn)
    if n:
        logger.info("dispose: 强制关闭了 %s 个残留数据库连接", n)
    return n


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
        with _open_conns_lock:
            _open_conns.discard(conn)


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """只读连接上下文。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
        with _open_conns_lock:
            _open_conns.discard(conn)


def init_schema() -> None:
    """建表（幂等）。应用启动时调用。"""
    with tx() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
    logger.info("数据库 schema 初始化完成: %s", settings.db_path)


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：为旧库补充 reimburse 新列与内置经典模板。"""
    # 1. reimburse 表新增列（SQLite 不支持 ADD COLUMN IF NOT EXISTS，需手动检测）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reimburse)").fetchall()}
    for col, ddl in [
        ("template_id", "ALTER TABLE reimburse ADD COLUMN template_id INTEGER"),
        ("extra_data",  "ALTER TABLE reimburse ADD COLUMN extra_data TEXT NOT NULL DEFAULT '{}'"),
        ("source_row",  "ALTER TABLE reimburse ADD COLUMN source_row INTEGER"),
        ("parent_id",   "ALTER TABLE reimburse ADD COLUMN parent_id INTEGER"),
        ("split_type",  "ALTER TABLE reimburse ADD COLUMN split_type TEXT NOT NULL DEFAULT 'none'"),
    ]:
        if col not in cols:
            conn.execute(ddl)
            logger.info("迁移: reimburse 表新增列 %s", col)

    # 1a. match_result 表新增 match_points（无票号场景的 ✅ 命中点，供并排比对展示）
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(match_result)").fetchall()}
    if "match_points" not in mcols:
        conn.execute("ALTER TABLE match_result ADD COLUMN match_points TEXT NOT NULL DEFAULT '[]'")
        logger.info("迁移: match_result 表新增列 match_points")

    # 1b. invoice 表移除冗余列（items_detail 已合并为 item_summary）
    inv_cols = {r[1] for r in conn.execute("PRAGMA table_info(invoice)").fetchall()}
    for col, ddl in [
        ("items_detail", "ALTER TABLE invoice DROP COLUMN items_detail"),
    ]:
        if col in inv_cols:
            conn.execute(ddl)
            logger.info("迁移: invoice 表移除列 %s", col)

    # 2. 内置经典模板（个人版默认模板）
    from app.domain.field_registry import default_fields_config
    exists = conn.execute(
        "SELECT id FROM reimburse_template WHERE is_system = 1 AND name = ? LIMIT 1",
        ("经典模板",),
    ).fetchone()
    if not exists:
        conn.execute(
            """INSERT INTO reimburse_template
               (name, description, is_default, is_system, fields_config, rules_config)
               VALUES (?, ?, 1, 1, ?, '[]')""",
            ("经典模板", "系统内置经典模板，兼容原有导入格式",
             json.dumps(default_fields_config(), ensure_ascii=False)),
        )
        logger.info("迁移: 插入内置经典模板")

    # 3. 费用分类字典：首次启动写入内置六类（只初始化一次，之后由设置页维护）
    if conn.execute("SELECT COUNT(*) AS c FROM category_dict").fetchone()["c"] == 0:
        from app.domain.categorize import STANDARD_CATEGORIES
        for i, name in enumerate(STANDARD_CATEGORIES):
            conn.execute(
                "INSERT INTO category_dict (name, is_builtin, enabled, sort_order) VALUES (?, 1, 1, ?)",
                (name, i),
            )
        logger.info("迁移: 初始化费用分类字典 %s 项", len(STANDARD_CATEGORIES))
