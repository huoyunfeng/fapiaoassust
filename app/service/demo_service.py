"""内置示例数据（Demo）：一键生成样例发票 + 报销单，触发匹配，产出「问题报告」。

设计目标：装完是空库时，点「看示例」即可在 2 分钟内看到一份「发现 ¥4,860 问题」的报告。

要点：
- 全部样例数据带 ``__demo__`` 标记（invoice.source_path / reimburse.source_file），
  可一键物理清除，不影响用户真实数据。
- 发票复用系统原生的校验 / 去重 / 风控流程：缺日期 → ABNORMAL、重复 → DUPLICATE、
  连号写入 risk_note，与真实用户导入的效果完全一致。
- 「超标」为演示概念：系统原生无费用限额标准，此处内置一套 Demo 限额，
  仅在示例报告中展示，不写入正式库、不影响正式匹配。
"""
import json
import time
import uuid
from decimal import Decimal
from typing import Dict, List, Tuple

from app.config import settings
from app.domain.normalize import to_decimal
from app.repository import db
from app.repository.repos import invoice_repo, reimburse_repo
from app.service import invoice_service, match_service
from app.service.risk_service import decode_note
from app.service.status_flow import change_invoice_status

DEMO_MARK = "__demo_sample__"          # 样例数据标记（invoice.source_path / reimburse.source_file）
DEMO_BATCH = "DEMO-BATCH-001"           # 样例报销单批次
DEMO_REIMBURSE_NO = "BX-2024-09-001"    # 样例报销单号
DEMO_SUBMITTER = "张三"
DEMO_BUYER = "演示科技有限公司"
DEMO_BUYER_TAX = "91110000MA01DEMO01"
DEMO_SELLER_TAX = "91110000MA01SELL01"

# 各类问题金额合计必须等于此目标值
TARGET_TOTAL = Decimal("4860.00")

# Demo 内置限额标准（仅示例报告用，不写入正式系统）：明细关键词 -> 单笔限额
DEMO_LIMITS: List[Tuple[str, Decimal]] = [
    ("住宿", Decimal("2200.00")),
    ("餐饮", Decimal("500.00")),
    ("交通", Decimal("1000.00")),
]


# ---------------------------------------------------------------- 样例数据定义
def _inv(no: str, total: str, date: str, seller: str, summary: str, tag: str) -> dict:
    """构造一张样例发票。amount=total、tax=0 以满足 C2 价税勾稽（0 税率发票合法）。"""
    return {"invoice_code": "", "invoice_no": no, "invoice_date": date,
            "seller_name": seller, "item_summary": summary,
            "amount": total, "tax_amount": "0.00", "total_amount": total,
            "buyer_name": DEMO_BUYER, "buyer_tax_no": DEMO_BUYER_TAX,
            "seller_tax_no": DEMO_SELLER_TAX, "tax_rate": "0%", "tag": tag}


# 20 张样例发票。tag 仅用于生成逻辑，不落业务字段：
#   diff      差异匹配票（报销额≠票面额）
#   dup_normal 重复对首张（→ NORMAL）
#   dup        重复对次张（→ DUPLICATE，I2 同号判重）
#   seq        连号票（同 seller 同 date 号码连续 → risk_note 连号）
#   over       超标票（住宿 3000 超限额 2200）
#   missing_date 缺日期票（→ ABNORMAL）
#   normal     正常匹配票
SAMPLE_INVOICES: List[dict] = [
    _inv("24000000000000000101", "620.00",  "2024-09-01", "北京商务酒店",       "住宿费", "diff"),
    _inv("24000000000000000102", "1050.00", "2024-09-02", "上海餐饮管理公司",   "餐饮费", "diff"),
    _inv("24000000000000000103", "1150.00", "2024-09-03", "深圳交通运输公司",   "交通费", "diff"),
    _inv("24000000000000000201", "580.00",  "2024-09-04", "广州酒店有限公司",   "住宿费", "dup_normal"),
    _inv("24000000000000000201", "580.00",  "2024-09-04", "广州酒店有限公司",   "住宿费", "dup"),
    _inv("24000000000000000301", "400.00",  "2024-09-05", "中石化朝阳加油站",   "加油费", "seq"),
    _inv("24000000000000000302", "400.00",  "2024-09-05", "中石化朝阳加油站",   "加油费", "seq"),
    _inv("24000000000000000303", "400.00",  "2024-09-05", "中石化朝阳加油站",   "加油费", "seq"),
    _inv("24000000000000000401", "3000.00", "2024-09-06", "上海豪华酒店",       "住宿费", "over"),
    _inv("24000000000000000501", "480.00",  "",           "某某商贸公司",       "办公用品", "missing_date"),
    _inv("24000000000000000601", "200.00",  "2024-09-08", "如家酒店",           "住宿费", "normal"),
    _inv("24000000000000000602", "250.00",  "2024-09-08", "肯德基",             "餐饮费", "normal"),
    _inv("24000000000000000603", "300.00",  "2024-09-09", "滴滴出行",           "交通费", "normal"),
    _inv("24000000000000000604", "350.00",  "2024-09-09", "汉庭酒店",           "住宿费", "normal"),
    _inv("24000000000000000605", "450.00",  "2024-09-10", "真功夫",             "餐饮费", "normal"),
    _inv("24000000000000000606", "500.00",  "2024-09-10", "神州租车",           "交通费", "normal"),
    _inv("24000000000000000607", "550.00",  "2024-09-11", "全季酒店",           "住宿费", "normal"),
    _inv("24000000000000000608", "700.00",  "2024-09-11", "海底捞",             "餐饮费", "normal"),
    _inv("24000000000000000609", "900.00",  "2024-09-12", "南方航空",           "交通费", "normal"),
    _inv("24000000000000000610", "1000.00", "2024-09-12", "万豪酒店",           "住宿费", "normal"),
]
assert len(SAMPLE_INVOICES) == 20, "样例发票必须为 20 张"


def _reb(amount: str, invoice_no: str, date: str, etype: str, remark: str, tag: str) -> dict:
    return {"amount": amount, "invoice_no": invoice_no, "reimburse_date": date,
            "expense_type": etype, "remark": remark, "tag": tag}


# 1 份样例报销单（14 行）：3 处金额差异 + 1 笔无票 + 10 行正常匹配
SAMPLE_REIMBURSES: List[dict] = [
    _reb("1020.00", "24000000000000000101", "2024-09-01", "差旅费",     "北京出差住宿（报销额与票面差 400）", "diff"),
    _reb("1500.00", "24000000000000000102", "2024-09-02", "业务招待费", "客户招待（报销额与票面差 450）",     "diff"),
    _reb("800.00",  "24000000000000000103", "2024-09-03", "差旅费",     "市内交通（报销额与票面差 350）",     "diff"),
    _reb("600.00",  "",                     "2024-09-07", "办公费",     "零星采购，无对应发票",               "no_invoice"),
    _reb("200.00",  "24000000000000000601", "2024-09-08", "差旅费",     "住宿", "normal"),
    _reb("250.00",  "24000000000000000602", "2024-09-08", "业务招待费", "餐饮", "normal"),
    _reb("300.00",  "24000000000000000603", "2024-09-09", "差旅费",     "交通", "normal"),
    _reb("350.00",  "24000000000000000604", "2024-09-09", "差旅费",     "住宿", "normal"),
    _reb("450.00",  "24000000000000000605", "2024-09-10", "业务招待费", "餐饮", "normal"),
    _reb("500.00",  "24000000000000000606", "2024-09-10", "差旅费",     "租车", "normal"),
    _reb("550.00",  "24000000000000000607", "2024-09-11", "差旅费",     "住宿", "normal"),
    _reb("700.00",  "24000000000000000608", "2024-09-11", "业务招待费", "餐饮", "normal"),
    _reb("900.00",  "24000000000000000609", "2024-09-12", "差旅费",     "机票", "normal"),
    _reb("1000.00", "24000000000000000610", "2024-09-12", "差旅费",     "住宿", "normal"),
]


# ---------------------------------------------------------------- 生成 / 清理
def _operator() -> str:
    return settings.default_operator


def clear_demo() -> dict:
    """物理清除全部样例数据（FK CASCADE 自动清理匹配记录与状态日志）。"""
    with db.tx() as conn:
        n_inv = conn.execute("DELETE FROM invoice WHERE source_path = ?",
                             (DEMO_MARK,)).rowcount
        n_reb = conn.execute("DELETE FROM reimburse WHERE source_file = ?",
                             (DEMO_MARK,)).rowcount
    return {"ok": True, "invoices": n_inv, "reimburses": n_reb}


def seed_demo() -> dict:
    """幂等生成样例数据：先清旧示例 → 插入发票（走原生校验/去重/风控）→ 插入报销单 → 触发匹配。"""
    clear_demo()
    operator = _operator()
    inserted: List[Tuple[str, int]] = []  # (tag, invoice_id)
    reimburse_ids: List[int] = []

    with db.tx() as conn:
        # 1) 发票：逐张走 校验+去重（缺日期→ABNORMAL，重复→DUPLICATE，其余→NORMAL）
        for spec in SAMPLE_INVOICES:
            tag = spec["tag"]
            fields = {k: v for k, v in spec.items() if k != "tag"}
            inv_id = invoice_repo.insert(conn, {
                "file_hash": uuid.uuid4().hex,   # 每张唯一；重复靠 I2(invoice_no) 判定
                "source_path": DEMO_MARK,
                "file_type": "pdf",
                "raw_json": json.dumps({"demo": True, "tag": tag, "issues": []},
                                       ensure_ascii=False),
                "parse_engine": "manual",
                "parse_confidence": 1.0,
                "status": "PARSING",
                "risk_note": "",
                **fields,
            })
            change_invoice_status(conn, inv_id, "PARSED", "示例数据", operator)
            invoice_service._validate_and_dedup(conn, inv_id, operator)  # noqa: SLF001（复用原生流程）
            inserted.append((tag, inv_id))

        # 2) 风控扫描（连号需同 seller+date 的票均已在库，故全部入库后逐张扫）
        for _tag, inv_id in inserted:
            invoice_service._scan_risk(conn, inv_id)  # noqa: SLF001

        # 3) 报销单
        for idx, spec in enumerate(SAMPLE_REIMBURSES):
            reb_id = reimburse_repo.insert(conn, {
                "batch_id": DEMO_BATCH,
                "source_file": DEMO_MARK,
                "reimburse_no": DEMO_REIMBURSE_NO,
                "submitter": DEMO_SUBMITTER,
                "reimburse_date": spec.get("reimburse_date"),
                "expense_type": spec.get("expense_type", ""),
                "amount": spec.get("amount"),
                "invoice_no": spec.get("invoice_no", ""),
                "remark": spec.get("remark", ""),
                "row_index": idx,
                "raw_row": json.dumps({"demo": True, "tag": spec.get("tag")},
                                      ensure_ascii=False),
            })
            reimburse_ids.append(reb_id)

    # 4) 触发匹配（事务外；仅限示例数据作用域，避免影响正式业务；全局串行锁，失败重试）
    summary: dict = {}
    inv_ids = {i for _tag, i in inserted}
    for _ in range(5):
        try:
            summary = match_service.run_matching(
                operator, wait_seconds=30,
                scope_invoice_ids=inv_ids, scope_reimburse_ids=set(reimburse_ids))
            break
        except match_service.MatchBusy:
            time.sleep(1.0)

    return {"ok": True, "invoices": len(SAMPLE_INVOICES),
            "reimburses": len(SAMPLE_REIMBURSES), "match": summary}


# ---------------------------------------------------------------- 示例报告汇总
def _q(s: str) -> Decimal:
    """安全转 Decimal，空值返回 0。"""
    v = to_decimal(s)
    return v if v is not None else Decimal("0")


def demo_report() -> dict:
    """汇总样例数据的 6 类问题金额，目标合计 ¥4,860.00。"""
    with db.read() as conn:
        invs = conn.execute(
            "SELECT * FROM invoice WHERE source_path = ? AND deleted_at IS NULL",
            (DEMO_MARK,)).fetchall()
        rebs = conn.execute(
            "SELECT * FROM reimburse WHERE source_file = ? AND deleted_at IS NULL",
            (DEMO_MARK,)).fetchall()
        # 与样例数据相关的匹配记录（用于金额差异 / 无票判定）
        matches = conn.execute(
            "SELECT m.id, m.status, m.invoice_id, m.reimburse_id, "
            "       i.invoice_no AS inv_no, i.total_amount AS inv_total, "
            "       r.amount AS reb_amount, r.id AS rid "
            "FROM match_result m "
            "LEFT JOIN invoice i ON i.id = m.invoice_id "
            "LEFT JOIN reimburse r ON r.id = m.reimburse_id "
            "WHERE i.source_path = ? OR r.source_file = ?",
            (DEMO_MARK, DEMO_MARK)).fetchall()

    # 1) 重复发票：DUPLICATE 状态的票面额
    dup_invs = [i for i in invs if i["status"] == "DUPLICATE"]
    dup_amt = sum((_q(i["total_amount"]) for i in dup_invs), Decimal("0"))

    # 2) 连号发票：risk_note 含「连号」标签的票面额
    seq_amt, seq_count = Decimal("0"), 0
    for i in invs:
        tags, _ = decode_note(i["risk_note"] or "")
        if "连号" in tags:
            seq_amt += _q(i["total_amount"])
            seq_count += 1

    # 3) 超标费用：仅样例中标记为 over 的发票（系统原生无限额，Demo 内置标准演示），
    #    避免正常票 / 差异票被重复计入问题金额。
    over_amt, over_count = Decimal("0"), 0
    over_items: List[dict] = []
    for i in invs:
        try:
            tag = (json.loads(i["raw_json"] or "{}") or {}).get("tag")
        except (json.JSONDecodeError, TypeError):
            tag = None
        if tag != "over":
            continue
        summary = i["item_summary"] or ""
        for kw, limit in DEMO_LIMITS:
            if kw in summary:
                excess = _q(i["total_amount"]) - limit
                if excess > Decimal("0"):
                    over_amt += excess
                    over_count += 1
                    over_items.append({
                        "invoice_no": i["invoice_no"], "summary": summary,
                        "total": str(i["total_amount"]), "limit": str(limit),
                        "excess": str(excess.quantize(Decimal("0.01")))})
                break

    # 4) 缺开票日期：ABNORMAL 状态的票面额
    miss_invs = [i for i in invs if i["status"] == "ABNORMAL"]
    miss_amt = sum((_q(i["total_amount"]) for i in miss_invs), Decimal("0"))

    # 5) 金额差异：有匹配记录且 |票面额 - 报销额| > 0.01
    diff_amt, diff_count = Decimal("0"), 0
    diff_items: List[dict] = []
    for m in matches:
        inv_t, reb_a = _q(m["inv_total"]), _q(m["reb_amount"])
        if m["inv_total"] is None or m["reb_amount"] is None:
            continue
        d = abs(inv_t - reb_a)
        if d > Decimal("0.01"):
            diff_amt += d
            diff_count += 1
            diff_items.append({
                "match_id": m["id"], "status": m["status"],
                "invoice_no": m["inv_no"] or "", "amount_diff": str(d.quantize(Decimal("0.01")))})

    # 6) 无票报销：样例报销单中没有任何匹配记录的
    matched_rid = {m["rid"] for m in matches if m["rid"] is not None}
    no_inv_amt, no_inv_count = Decimal("0"), 0
    for r in rebs:
        if r["id"] not in matched_rid:
            no_inv_amt += _q(r["amount"])
            no_inv_count += 1

    total = (dup_amt + seq_amt + over_amt + miss_amt + diff_amt + no_inv_amt
             ).quantize(Decimal("0.01"))
    return {
        "target_total": str(TARGET_TOTAL),
        "actual_total": str(total),
        "matched": abs(total - TARGET_TOTAL) <= Decimal("0.01"),
        "invoice_count": len(invs),
        "reimburse_count": len(rebs),
        "categories": [
            {"key": "duplicate",    "name": "重复发票", "amount": str(dup_amt.quantize(Decimal("0.01"))),
             "count": len(dup_invs),  "desc": "相同发票号码重复报销", "color": "#f59e0b"},
            {"key": "sequence",     "name": "连号发票", "amount": str(seq_amt.quantize(Decimal("0.01"))),
             "count": seq_count,     "desc": "同商户连号开具，存在虚开风险", "color": "#8b5cf6"},
            {"key": "over_limit",   "name": "超标费用", "amount": str(over_amt.quantize(Decimal("0.01"))),
             "count": over_count,    "desc": "超出公司费用标准的部分", "color": "#ef4444"},
            {"key": "missing_date", "name": "缺开票日期", "amount": str(miss_amt.quantize(Decimal("0.01"))),
             "count": len(miss_invs), "desc": "票面缺失开票日期，不合规", "color": "#f97316"},
            {"key": "amount_diff",  "name": "金额差异", "amount": str(diff_amt.quantize(Decimal("0.01"))),
             "count": diff_count,    "desc": "报销金额与发票金额不一致", "color": "#3b82f6"},
            {"key": "no_invoice",   "name": "无票报销", "amount": str(no_inv_amt.quantize(Decimal("0.01"))),
             "count": no_inv_count,  "desc": "报销单无对应发票", "color": "#ec4899"},
        ],
        "over_items": over_items,
        "diff_items": diff_items,
    }
