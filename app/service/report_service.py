"""差异报告用例（应用服务层）：数据汇聚 + Excel 导出。"""
import json
from typing import Any, Dict, List, Optional

from app.domain.dto import ReimburseDTO
from app.domain.features import reimburse_features
from app.domain.normalize import to_decimal
from app.infra.excel import export_report, export_report_stream
from app.repository import db
from app.repository.repos import (category_dict_repo, category_repo,
                                  invoice_repo, match_repo, reimburse_repo)

from app.service.invoice_service import _raw_json  # 复用解析


def build_report() -> dict:
    """汇聚差异报告数据：汇总统计 + 匹配明细 + 异常清单。"""
    with db.read() as conn:
        inv_counts = invoice_repo.count_by_status(conn)
        match_counts = match_repo.count_by_status(conn)
        reb_total = reimburse_repo.count(conn)
        matches = match_repo.list(conn)
        pairs = match_repo.pairs_of(conn, [m["id"] for m in matches])
        occupied = match_repo.active_reimburse_ids(conn)

        all_reimburses = reimburse_repo.list(conn, limit=100000)
        reb_map = {r["id"]: r for r in all_reimburses}
        reb_no_map = {}
        for m in matches:
            for p in pairs.get(m["id"], []):
                reb_no_map[m["id"]] = reb_map.get(p["reimburse_id"])

        abnormal = [r for r in invoice_repo.list(conn, status="ABNORMAL", limit=10000)]
        duplicates = [r for r in invoice_repo.list(conn, status="DUPLICATE", limit=10000)]
        normal_invoices = invoice_repo.list(conn, status="NORMAL", limit=100000)
        matched_invoice_ids = {m["invoice_id"] for m in matches if m["status"] in ("MATCHED", "CONFIRMED")}

    # 附带 raw_json（解析错误/重复原因），供页面展示
    for i in abnormal + duplicates:
        i["raw"] = _raw_json(i)

    unmatched_reimburses = [r for r in all_reimburses if r["id"] not in occupied]
    # 未匹配发票 = NORMAL 且无有效匹配（MATCHED/CONFIRMED）的发票
    # 与发票列表页口径一致（NORMAL 即标红为未匹配），不再排除匹配失败的 pending 项
    unmatched_invoices = [i for i in normal_invoices if i["id"] not in matched_invoice_ids]

    # 匹配明细行（含汇总匹配的报销单号）
    details: List[Dict[str, Any]] = []
    for m in matches:
        reb = m.get("reimburse_id") and reb_map.get(m["reimburse_id"]) or reb_no_map.get(m["id"])
        inv_total = to_decimal(m.get("total_amount"))
        reb_amount = to_decimal(reb.get("amount")) if reb else None
        diff = abs(inv_total - reb_amount) if (inv_total is not None and reb_amount is not None) else None
        details.append({
            "match": m, "reimburse": reb,
            "invoice_total": inv_total, "reimburse_amount": reb_amount,
            "amount_diff": diff,
        })

    return {
        "inv_counts": inv_counts, "match_counts": match_counts,
        "reb_total": reb_total,
        "active_matches": match_counts.get("MATCHED", 0) + match_counts.get("CONFIRMED", 0),
        "details": details,
        "abnormal": abnormal, "duplicates": duplicates,
        "unmatched_reimburses": unmatched_reimburses,
        "unmatched_invoices": unmatched_invoices,
    }


def _is_pending_detail(d: dict) -> bool:
    """待处理口径（与匹配中心一致）：待人工确认/冲突，或有报销单关联的无候选记录。"""
    st = d["match"]["status"]
    return st in ("SUSPICIOUS", "CONFLICT") or (st == "UNMATCHED" and d.get("reimburse") is not None)


def _paginate(items: list, page: int, page_size: int) -> tuple:
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    return items[start:start + page_size], {
        "total": total, "page": page, "pages": pages, "page_size": page_size}


def _pending_total(conn) -> int:
    """全局待处理数（不受匹配明细 500 行上限影响）。"""
    base = conn.execute(
        "SELECT COUNT(*) AS c FROM match_result "
        "WHERE status IN ('SUSPICIOUS','CONFLICT')").fetchone()["c"]
    paired = conn.execute(
        "SELECT COUNT(*) AS c FROM match_result m WHERE m.status = 'UNMATCHED' "
        "AND (m.reimburse_id IS NOT NULL OR EXISTS ("
        "  SELECT 1 FROM match_pair p WHERE p.match_id = m.id))").fetchone()["c"]
    return int(base) + int(paired)


def _num(v):
    """Decimal/str -> float（JSON 数值展示），空值返回 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _strip_note(text: Optional[str]) -> str:
    """剥掉系统写入的【不匹配】说明，只留用户原始备注。"""
    if not text:
        return ""
    idx = text.find("【不匹配】")
    return text[:idx].rstrip() if idx >= 0 else text


def _reb_features(reb: Optional[dict]) -> dict:
    """报销单业务特征（事由 / 起止地点），供并排比对与文本命中展示。"""
    if not reb:
        return {"reason": "", "origin": "", "destination": "", "has_text": False}
    dto = ReimburseDTO(
        reimburse_date=reb.get("reimburse_date"),
        period_start=reb.get("period_start"),
        period_end=reb.get("period_end"),
        remark=_strip_note(reb.get("remark")),
        extra_data=reb.get("extra_data") or {},
    )
    feat = reimburse_features(dto)
    return {"reason": feat.reason, "origin": feat.origin,
            "destination": feat.destination, "has_text": feat.has_text}


def _traffic_of(row: dict) -> dict:
    """发票 raw_json.traffic（车牌号 / 上下车时间 / 票面文字）。"""
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        raw = {}
    return raw.get("traffic") or {}


def _detail_payload(d: dict, cat_map: Dict[int, str]) -> dict:
    m, reb = d["match"], d.get("reimburse")
    feat = _reb_features(reb)
    traffic = _traffic_of(m)
    return {
        "id": m["id"],
        "invoice_id": m["invoice_id"],
        "reimburse_id": reb["id"] if reb else m.get("reimburse_id"),
        "status": m["status"],
        "match_type": m["match_type"],
        "score": m["match_score"],
        "reimburse_no": reb["reimburse_no"] if reb else (m.get("reimburse_no") or ""),
        "submitter": (reb.get("submitter") or "") if reb else "",
        "expense_type": (reb.get("expense_type") or "") if reb else "",
        "reimburse_amount": _num(d.get("reimburse_amount")),
        "reb_invoice_no": (reb.get("invoice_no") or "") if reb else "",
        "invoice_no": m.get("invoice_no") or "",
        "invoice_date": m.get("invoice_date") or "",
        "invoice_total": _num(d.get("invoice_total")),
        "amount_diff": _num(d.get("amount_diff")),
        "message": m.get("message") or "",
        "category": cat_map.get(m["invoice_id"], ""),
        "created_at": m.get("created_at") or "",
        "pending": _is_pending_detail(d),
        "active": m["status"] in ("MATCHED", "CONFIRMED"),
        # ---- 并排比对（无票号场景：金额 / 日期 / 事由 / 起止地点 / 票面文字）----
        "match_points": m.get("match_points") or [],
        "reb_reason": feat["reason"],
        "reb_origin": feat["origin"],
        "reb_destination": feat["destination"],
        "reb_date": (reb.get("reimburse_date") or "") if reb else "",
        "invoice_seller": m.get("seller_name") or "",
        "invoice_summary": m.get("item_summary") or "",
        "invoice_traffic_text": traffic.get("text") or "",
        "invoice_plate": traffic.get("plate_no") or "",
        "invoice_ride_time": traffic.get("ride_time") or "",
        "has_text_features": bool(feat["has_text"] or traffic.get("text")),
        "compare_url": f"/matches/{m['id']}/compare",
    }


def _invoice_view(inv: Optional[dict], m: Optional[dict]) -> dict:
    """并排比对用的发票视图（票面文字 / 车牌 / 上下车时间 + 命中点）。"""
    inv = inv or {}
    m = m or {}
    traffic = _traffic_of(inv)
    return {
        "match_id": m.get("id"),
        "invoice_id": inv.get("id") or m.get("invoice_id"),
        "invoice_no": inv.get("invoice_no") or "",
        "invoice_date": inv.get("invoice_date") or "",
        "total_amount": _num(inv.get("total_amount")),
        "seller_name": inv.get("seller_name") or "",
        "item_summary": inv.get("item_summary") or "",
        "plate_no": traffic.get("plate_no") or "",
        "ride_time": traffic.get("ride_time") or "",
        "traffic_text": traffic.get("text") or "",
        "score": m.get("match_score"),
        "status": m.get("status") or "",
        "match_points": m.get("match_points") or [],
        "message": m.get("message") or "",
    }


def compare_payload(match_id: int) -> Optional[dict]:
    """并排比对数据包：报销单 vs 推荐发票 + 平局候选（金额/日期相同的多张票）。

    只按业务字段（金额、日期、事由、起止地点、票面文字）展示，不使用任何序号。
    """
    with db.read() as conn:
        m = match_repo.get(conn, match_id)
        if m is None:
            return None
        reb_id = m.get("reimburse_id")
        if reb_id is None:
            pairs = match_repo.pairs_of(conn, [match_id]).get(match_id, [])
            reb_id = pairs[0]["reimburse_id"] if pairs else None
        reb = reimburse_repo.get(conn, reb_id) if reb_id else None
        current = _invoice_view(invoice_repo.get(conn, m["invoice_id"]), m)

        candidates: List[dict] = []
        if reb_id:
            rows = conn.execute(
                "SELECT id FROM match_result WHERE reimburse_id = ? "
                "AND status IN ('SUSPICIOUS','UNMATCHED','CONFLICT') "
                "ORDER BY match_score DESC, id ASC", (reb_id,)).fetchall()
            for r in rows:
                cm = match_repo.get(conn, r["id"])
                candidates.append(_invoice_view(invoice_repo.get(conn, cm["invoice_id"]), cm))
        if not candidates:
            candidates = [current]

    feat = _reb_features(reb)
    reb = reb or {}
    return {
        "match_id": match_id,
        "status": m.get("status") or "",
        "reimburse": {
            "id": reb.get("id") or reb_id,
            "reimburse_no": reb.get("reimburse_no") or "",
            "submitter": reb.get("submitter") or "",
            "expense_type": reb.get("expense_type") or "",
            "amount": _num(reb.get("amount")),
            "date": reb.get("reimburse_date") or "",
            "reason": feat["reason"],
            "origin": feat["origin"],
            "destination": feat["destination"],
            "invoice_no": reb.get("invoice_no") or "",
        },
        "invoice": current,
        "candidates": candidates,
    }


def _haystack(*values) -> str:
    return " ".join(str(v) for v in values if v is not None)


def _hit(text: str, q: str) -> bool:
    """关键词命中判断（空关键词视为全部命中）。"""
    return True if not q else q.lower() in text.lower()


def _sorted_details(report: dict) -> List[dict]:
    """匹配明细全量：异常优先，其次按创建时间倒序（稳定排序）。"""
    with db.read() as conn:
        inv_ids = [d["match"]["invoice_id"] for d in report["details"]]
        cat_map = {iid: (r.get("category") or "")
                   for iid, r in category_repo.decisions(conn, inv_ids).items()}
    rows = [_detail_payload(d, cat_map) for d in report["details"]]
    rows.sort(key=lambda r: (r["created_at"] or "", r["id"]), reverse=True)
    rows.sort(key=lambda r: 0 if r["pending"] else 1)
    return rows


def _apply_filter(rows: List[dict], filter: str, q: str) -> List[dict]:
    if filter == "pending":
        rows = [r for r in rows if r["pending"]]
    elif filter == "active":
        rows = [r for r in rows if r["active"]]
    if q:
        rows = [r for r in rows
                if _hit(_haystack(r["invoice_no"], r["reimburse_no"], r["submitter"],
                                  r["message"], r["category"], r["expense_type"],
                                  r["invoice_date"]), q)]
    return rows


def _apply_q(items: List[dict], q: str, keys: tuple) -> List[dict]:
    if not q:
        return items
    return [r for r in items if _hit(_haystack(*[r.get(k) for k in keys]), q)]


ALLOWED_PAGE_SIZES = (20, 50, 100)


def _size_of(page_sizes: Optional[Dict[str, int]], key: str, default: int) -> int:
    """每个分页器独立的每页条数（主表与底部 Tab 互不干扰）。"""
    raw = (page_sizes or {}).get(key)
    try:
        size = int(raw) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        size = default
    return size if size in ALLOWED_PAGE_SIZES else default


def report_payload(filter: str = "", q: str = "", page_size: int = 20,
                   dpage: int = 1, rpage: int = 1, ipage: int = 1,
                   apage: int = 1, xpage: int = 1,
                   page_sizes: Optional[Dict[str, int]] = None) -> dict:
    """差异报告「状态驱动」数据包：统计 + 四类清单分页 + 分类候选。

    filter: pending（待处理）/ active（生效匹配）/ 空（全部明细）。
    q: 关键词（发票号/报销单号/提交人/差异说明等），命中范围覆盖主表与底部清单。
    前端每次操作成功后重新拉取本数据包，卡片、主表、底部 Tab 同步翻转。
    """
    report = build_report()
    details = _apply_filter(_sorted_details(report), filter, q)

    unmatched_rebs = _apply_q(report["unmatched_reimburses"], q,
                              ("reimburse_no", "submitter", "expense_type", "remark"))
    unmatched_invs = _apply_q(report["unmatched_invoices"], q,
                              ("invoice_no", "seller_name", "item_summary", "invoice_date"))
    abnormal = _apply_q(report["abnormal"], q, ("invoice_no", "status"))
    duplicates = _apply_q(report["duplicates"], q, ("invoice_no", "status"))

    with db.read() as conn:
        categories = category_dict_repo.names(conn)
        pending = _pending_total(conn)

    # 左侧导航红点数量：全局口径（不受关键词搜索影响）
    badges = {
        "active": report["active_matches"],
        "pending": pending,
        "all": sum(report["match_counts"].values()),
        "unmatched_reimburses": len(report["unmatched_reimburses"]),
        "unmatched_invoices": len(report["unmatched_invoices"]),
        "abnormal": len(report["abnormal"]),
        "duplicates": len(report["duplicates"]),
    }
    badges["issues"] = badges["abnormal"] + badges["duplicates"]

    d_rows, d_pager = _paginate(details, dpage, _size_of(page_sizes, "dpage", page_size))
    r_rows, r_pager = _paginate(unmatched_rebs, rpage, _size_of(page_sizes, "rpage", page_size))
    i_rows, i_pager = _paginate(unmatched_invs, ipage, _size_of(page_sizes, "ipage", page_size))
    a_rows, a_pager = _paginate(abnormal, apage, _size_of(page_sizes, "apage", page_size))
    x_rows, x_pager = _paginate(duplicates, xpage, _size_of(page_sizes, "xpage", page_size))

    return {
        "stats": {
            "invoice_total": sum(report["inv_counts"].values()),
            "reimburse_total": report["reb_total"],
            "active_matches": report["active_matches"],
            "pending": pending,
            "unmatched_reimburses": len(unmatched_rebs),
            "unmatched_invoices": len(unmatched_invs),
            "abnormal": len(abnormal),
            "duplicates": len(duplicates),
        },
        "filter": filter if filter in ("pending", "active") else "",
        "q": q,
        "categories": categories,
        "badges": badges,
        "details": d_rows,
        "unmatched_reimburses": [
            {"id": r["id"], "reimburse_no": r["reimburse_no"], "amount": _num(r["amount"]),
             "submitter": r.get("submitter") or "", "invoice_no": r.get("invoice_no") or "",
             "expense_type": r.get("expense_type") or ""}
            for r in r_rows],
        "unmatched_invoices": [
            {"id": r["id"], "invoice_no": r.get("invoice_no") or "",
             "total_amount": _num(r.get("total_amount")),
             "invoice_date": r.get("invoice_date") or "",
             "item_summary": r.get("item_summary") or "",
             "seller_name": r.get("seller_name") or ""}
            for r in i_rows],
        "abnormal": [
            {"id": r["id"], "invoice_no": r.get("invoice_no") or "", "status": r["status"]}
            for r in a_rows],
        "duplicates": [
            {"id": r["id"], "invoice_no": r.get("invoice_no") or "", "status": r["status"]}
            for r in x_rows],
        "pagers": {"dpage": d_pager, "rpage": r_pager, "ipage": i_pager,
                   "apage": a_pager, "xpage": x_pager},
    }


_STATUS_CN = {"MATCHED": "已匹配", "CONFIRMED": "已确认", "SUSPICIOUS": "待人工确认",
              "CONFLICT": "字段冲突", "UNMATCHED": "无候选", "REJECTED": "已拒绝",
              "PENDING": "匹配中"}
_TYPE_CN = {"exact": "精确匹配", "fuzzy": "模糊匹配", "composite": "组合匹配",
            "many_to_one": "多对一", "manual": "人工指定"}


def _money(v) -> str:
    return "" if v is None else f"{float(v):.2f}"


# 匹配明细可见列（key 与前端 COLS 保持一致，导出按用户当前可见列裁剪）
REPORT_COLUMNS = [
    ("id", "匹配ID", lambda d: d["id"]),
    ("status", "状态", lambda d: _STATUS_CN.get(d["status"], d["status"])),
    ("type", "类型", lambda d: _TYPE_CN.get(d["match_type"], d["match_type"] or "")),
    ("score", "分数", lambda d: f"{d['score'] or 0:.2f}"),
    ("reimburse_no", "报销单号", lambda d: d["reimburse_no"]),
    ("submitter", "提交人", lambda d: d["submitter"]),
    ("expense_type", "费用类型", lambda d: d["expense_type"]),
    ("reimburse_amount", "报销金额", lambda d: _money(d["reimburse_amount"])),
    ("invoice_no", "发票号", lambda d: d["invoice_no"]),
    ("invoice_date", "开票日期", lambda d: d["invoice_date"]),
    ("invoice_total", "发票价税合计", lambda d: _money(d["invoice_total"])),
    ("amount_diff", "金额差异", lambda d: _money(d["amount_diff"])),
    ("message", "差异说明", lambda d: d["message"]),
    ("category", "费用分类", lambda d: d["category"]),
]
_COL_MAP = {c[0]: c for c in REPORT_COLUMNS}
_ISSUE_HEADER = ["类别", "单据", "关键信息", "说明"]


def _summary_rows(report: dict) -> List[list]:
    inv_counts, mc = report["inv_counts"], report["match_counts"]
    return [
        ["发票总数", sum(inv_counts.values())],
        ["发票-NORMAL（校验通过）", inv_counts.get("NORMAL", 0)],
        ["发票-ABNORMAL（校验失败）", inv_counts.get("ABNORMAL", 0)],
        ["发票-DUPLICATE（重复）", inv_counts.get("DUPLICATE", 0)],
        ["发票-MATCHED（已匹配）", inv_counts.get("MATCHED", 0)],
        ["报销单总数", report["reb_total"]],
        ["生效匹配数", report["active_matches"]],
        ["待人工处理（UNMATCHED/CONFLICT）",
         mc.get("CONFLICT", 0) + sum(1 for m in report["details"]
                                     if m["match"]["status"] == "UNMATCHED" and m["match"]["reimburse_id"])],
        ["字段冲突（CONFLICT）", mc.get("CONFLICT", 0)],
        ["未匹配报销单", len(report["unmatched_reimburses"])],
        ["未匹配发票", len(report["unmatched_invoices"])],
    ]


def _issue_rows(report: dict) -> List[list]:
    rows = []
    for i in report["abnormal"]:
        issues = _raw_json(i).get("issues", [])
        msg = "; ".join(x.get("message", "") for x in issues) or _raw_json(i).get("error", "")
        rows.append(["异常发票(ABNORMAL)", i.get("invoice_no") or "（无号）", msg, ""])
    for i in report["duplicates"]:
        raw = _raw_json(i)
        rows.append(["重复发票(DUPLICATE)", i.get("invoice_no") or "（无号）",
                     raw.get("dup_reason", ""), "重复于已存在的同内容发票"])
    for m in report["details"]:
        ms = m["match"]["status"]
        if ms in ("SUSPICIOUS", "CONFLICT") or (ms == "UNMATCHED" and m["match"]["reimburse_id"]):
            reb_no = (m["match"].get("reimburse_no")
                      or ((m.get("reimburse") or {}).get("reimburse_no")) or "—")
            rows.append([f"待处理({ms})",
                         f"发票 {m['match'].get('invoice_no', '') or '（无号）'} / 报销单 {reb_no}",
                         m["match"].get("message", ""),
                         f"score={m['match']['match_score']}"])
    for r in report["unmatched_reimburses"]:
        rows.append(["未匹配报销单", r["reimburse_no"] or "—",
                     f"金额 {r['amount']}", r.get("remark", "")])
    for i in report["unmatched_invoices"]:
        rows.append(["未匹配发票", i.get("invoice_no") or "（无号）",
                     f"价税合计 {i.get('total_amount', '')}", i.get("item_summary", "")])
    return rows


def _detail_sheet(details: List[dict], cols: List[str] = None) -> tuple:
    """按可见列生成匹配明细表头与行（cols 为空 = 全部列）。"""
    keys = [k for k in (cols or []) if k in _COL_MAP] or [c[0] for c in REPORT_COLUMNS]
    picked = [_COL_MAP[k] for k in keys]
    header = [c[1] for c in picked]
    rows = [[c[2](d) for c in picked] for d in details]
    return header, rows


def export_excel(report: dict) -> bytes:
    """导出差异报告 Excel（汇总 / 匹配明细 / 差异与异常）。"""
    header, rows = _detail_sheet(_sorted_details(report))
    return export_report([
        ("汇总", ["指标", "数值"], _summary_rows(report)),
        ("匹配明细", header, rows),
        ("差异与异常", _ISSUE_HEADER, _issue_rows(report)),
    ])


def export_excel_stream(filter: str = "", q: str = "", cols: List[str] = None):
    """按当前视图（筛选条件 / 关键词 / 可见列）流式导出 Excel。

    返回分块字节迭代器，供 StreamingResponse 边生成边下发，大报表不占内存。
    """
    report = build_report()
    details = _apply_filter(_sorted_details(report), filter, q)
    header, rows = _detail_sheet(details, cols)
    issue_rows = [r for r in _issue_rows(report)
                  if _hit(" ".join(str(x) for x in r), q)]
    return export_report_stream([
        ("汇总", ["指标", "数值"], _summary_rows(report)),
        ("匹配明细", header, rows),
        ("差异与异常", _ISSUE_HEADER, issue_rows),
    ])
