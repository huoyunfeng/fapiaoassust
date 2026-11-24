"""轻量风控预警：连号/跳号 + 敏感词扫描（独立于匹配算分，不阻断业务）。

设计要点：
- 默认关闭：两项开关都关闭时，导入流程直接跳过，零额外开销。
- 只写提示，不做拦截：结果写入 invoice.risk_note（JSON），由前端极简展示。
- 扫描在导入事务内执行，只读同商户同日期发票，异常不影响导入。
"""
import json
import re
from typing import List, Optional

from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import invoice_repo, kv_repo

logger = get_logger(__name__)

KV_SEQ_ON = "risk_seq_on"
KV_WORD_ON = "risk_word_on"
KV_WORDS = "risk_words"
DEFAULT_WORDS = ["烟", "酒", "礼品", "洗浴", "娱乐"]


# ---------------------------------------------------------------- 设置读写
def get_settings() -> dict:
    with db.read() as conn:
        # 连号/跳号预警默认开启：同销售方同日期的连号票应被提示（仅提示，不阻断匹配）；
        # 用户在设置页主动关闭后，kv 落盘 "0"，此处读出 "0" 即尊重用户选择。
        seq = kv_repo.get(conn, KV_SEQ_ON, "1") == "1"
        word = kv_repo.get(conn, KV_WORD_ON, "0") == "1"
        raw = kv_repo.get(conn, KV_WORDS, "") or ""
    try:
        words = [w for w in json.loads(raw) if isinstance(w, str) and w.strip()]
    except Exception:
        words = []
    if not words:
        words = list(DEFAULT_WORDS)
    return {"seq": seq, "word": word, "words": words}


def save_settings(seq: bool, word: bool, words: List[str]) -> dict:
    clean = []
    for w in words or []:
        w = (w or "").strip()
        if w and w not in clean:
            clean.append(w)
    with db.tx() as conn:
        kv_repo.set(conn, KV_SEQ_ON, "1" if seq else "0")
        kv_repo.set(conn, KV_WORD_ON, "1" if word else "0")
        kv_repo.set(conn, KV_WORDS, json.dumps(clean, ensure_ascii=False))
    return {"ok": True, "seq": bool(seq), "word": bool(word), "words": clean}


# ---------------------------------------------------------------- 扫描
def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def scan_invoice(conn, invoice_id: int, settings: Optional[dict] = None) -> List[dict]:
    """对单张发票执行风控扫描，返回 [{tag, text}]；结果写入 risk_note。"""
    cfg = settings or get_settings()
    if not cfg["seq"] and not cfg["word"]:
        return []
    inv = invoice_repo.get(conn, invoice_id)
    if not inv:
        return []
    items: List[dict] = []

    if cfg["seq"]:
        items.extend(_scan_sequence(conn, inv))
    if cfg["word"]:
        items.extend(_scan_words(inv, cfg["words"]))

    payload = json.dumps(items, ensure_ascii=False) if items else ""
    if payload != (inv.get("risk_note") or ""):
        invoice_repo.update(conn, invoice_id, {"risk_note": payload})
    return items


def _scan_sequence(conn, inv: dict) -> List[dict]:
    """同销售方 + 同开票日期的号码比对：差 1=连号，差 2=跳号。"""
    no = _digits(inv.get("invoice_no"))
    seller = (inv.get("seller_name") or "").strip()
    day = (inv.get("invoice_date") or "").strip()
    if len(no) < 4 or not seller or not day:
        return []
    rows = conn.execute(
        "SELECT id, invoice_no FROM invoice WHERE id <> ? AND deleted_at IS NULL "
        "AND seller_name = ? AND invoice_date = ?", (inv["id"], seller, day)).fetchall()
    out, seen = [], set()
    for r in rows:
        other = _digits(r["invoice_no"])
        if not other or other == no:
            continue
        try:
            delta = abs(int(other) - int(no))
        except ValueError:
            continue
        if delta == 1 and "连号" not in seen:
            seen.add("连号")
            out.append({"tag": "连号",
                        "text": f"连号，与发票 {r['invoice_no']} 号码相连"})
        elif delta == 2 and "跳号" not in seen:
            seen.add("跳号")
            out.append({"tag": "跳号",
                        "text": f"跳号，与发票 {r['invoice_no']} 中间存在跳号（中间可能有作废票）"})
    return out


def _scan_words(inv: dict, words: List[str]) -> List[dict]:
    text = f"{inv.get('seller_name') or ''} {inv.get('item_summary') or ''}"
    hits = [w for w in words if w and w in text]
    if not hits:
        return []
    # 多个词命中合并为一条，避免列表里堆叠多个一样的「敏感词」标签
    return [{"tag": "敏感词", "text": f"敏感词，命中违规词汇 [{'、'.join(hits)}]"}]


# ---------------------------------------------------------------- 展示辅助
def decode_note(note: str) -> tuple:
    """把 risk_note 解码成 (标签列表, 明细列表)，坏数据安全降级为空。"""
    if not note:
        return [], []
    try:
        items = json.loads(note)
        if not isinstance(items, list):
            raise ValueError
        tags = [i.get("tag") for i in items if i.get("tag")]
        return tags, items
    except Exception:
        return [], []
