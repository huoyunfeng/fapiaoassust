"""差异报告用例（应用服务层）：数据汇聚 + Excel 导出。"""
from typing import Any, Dict, List

from app.domain.normalize import to_decimal
from app.infra.excel import export_report
from app.repository import db
from app.repository.repos import (invoice_repo, match_repo, reimburse_repo)

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
        pending_invoice_ids = {m["invoice_id"] for m in matches
                               if m["status"] in ("SUSPICIOUS", "CONFLICT")
                               or (m["status"] == "UNMATCHED" and m["reimburse_id"])}

    # 附带 raw_json（解析错误/重复原因），供页面展示
    for i in abnormal + duplicates:
        i["raw"] = _raw_json(i)

    unmatched_reimburses = [r for r in all_reimburses if r["id"] not in occupied]
    unmatched_invoices = [i for i in normal_invoices
                          if i["id"] not in matched_invoice_ids and i["id"] not in pending_invoice_ids]

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


def export_excel(report: dict) -> bytes:
    """导出差异报告 Excel（汇总 / 匹配明细 / 差异与异常）。"""
    inv_counts, mc = report["inv_counts"], report["match_counts"]
    summary_rows = [
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

    detail_header = ["匹配ID", "状态", "类型", "分数", "报销单号", "提交人", "费用类型",
                     "报销金额", "发票号", "开票日期", "发票价税合计", "金额差异",
                     "差异说明", "确认人"]
    detail_rows = []
    for d in report["details"]:
        m, reb = d["match"], d["reimburse"]
        detail_rows.append([
            m["id"], m["status"], m["match_type"], m["match_score"],
            reb["reimburse_no"] if reb else (m.get("reimburse_no") or ""),
            reb["submitter"] if reb else "",
            reb["expense_type"] if reb else "",
            str(d["reimburse_amount"]) if d["reimburse_amount"] is not None else "",
            m.get("invoice_no") or "",
            m.get("invoice_date") or "",
            str(d["invoice_total"]) if d["invoice_total"] is not None else "",
            str(d["amount_diff"]) if d["amount_diff"] is not None else "",
            m.get("message") or "", m.get("confirmed_by") or "",
        ])

    issue_header = ["类别", "单据", "关键信息", "说明"]
    issue_rows = []
    for i in report["abnormal"]:
        issues = _raw_json(i).get("issues", [])
        msg = "; ".join(x.get("message", "") for x in issues) or _raw_json(i).get("error", "")
        issue_rows.append(["异常发票(ABNORMAL)", f"#{i['id']} {i.get('invoice_no', '')}", msg, ""])
    for i in report["duplicates"]:
        raw = _raw_json(i)
        issue_rows.append(["重复发票(DUPLICATE)", f"#{i['id']} {i.get('invoice_no', '')}",
                           raw.get("dup_reason", ""), f"重复于发票 #{raw.get('dup_of', '')}"])
    for m in report["details"]:
        ms = m["match"]["status"]
        if ms in ("SUSPICIOUS", "CONFLICT") or (ms == "UNMATCHED" and m["match"]["reimburse_id"]):
            issue_rows.append([f"待处理({ms})",
                               f"match#{m['match']['id']} 发票 {m['match'].get('invoice_no', '')}",
                               m["match"].get("message", ""),
                               f"score={m['match']['match_score']}"])
    for r in report["unmatched_reimburses"]:
        issue_rows.append(["未匹配报销单", f"#{r['id']} {r['reimburse_no']}",
                           f"金额 {r['amount']}", r.get("remark", "")])
    for i in report["unmatched_invoices"]:
        issue_rows.append(["未匹配发票", f"#{i['id']} {i.get('invoice_no', '')}",
                           f"价税合计 {i.get('total_amount', '')}", i.get("item_summary", "")])

    return export_report([
        ("汇总", ["指标", "数值"], summary_rows),
        ("匹配明细", detail_header, detail_rows),
        ("差异与异常", issue_header, issue_rows),
    ])
