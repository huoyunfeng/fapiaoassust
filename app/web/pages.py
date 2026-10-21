"""UI 层 - 页面路由（Jinja2 服务端渲染）。只做展示与交互，不含业务规则。"""
import json
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.service import (invoice_service, match_service, reimburse_service,
                         report_service)

pages = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS_BADGE = {
    "PARSING": "badge-gray", "PARSED": "badge-blue", "NORMAL": "badge-green",
    "ABNORMAL": "badge-red", "DUPLICATE": "badge-orange", "MATCHED": "badge-purple",
    "PENDING": "badge-gray", "SUSPICIOUS": "badge-red", "CONFLICT": "badge-red",
    "UNMATCHED": "badge-red", "CONFIRMED": "badge-green", "REJECTED": "badge-gray",
}
templates.env.filters["badge"] = lambda s: STATUS_BADGE.get(s, "badge-gray")
# 需求2：SUSPICIOUS 状态已废弃，存量记录在前端统一按 UNMATCHED 展示
templates.env.filters["dstatus"] = lambda s: "UNMATCHED" if s == "SUSPICIOUS" else s
templates.env.filters["fromjson"] = lambda s: (json.loads(s) if isinstance(s, str) and s else (s if isinstance(s, dict) else {}))


@pages.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    inv_counts = invoice_service.invoice_status_counts()
    mc = match_service.match_counts()
    report = report_service.build_report()
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "inv_counts": inv_counts, "match_counts": mc,
        "reb_total": report["reb_total"],
        "active_matches": report["active_matches"],
        "unmatched_reimburses": report["unmatched_reimburses"],
        "unmatched_invoices": report["unmatched_invoices"],
        "abnormal": report["abnormal"][:10],
        "suspicious": [d for d in report["details"]
                       if d["match"]["status"] in ("SUSPICIOUS", "CONFLICT")
                       or (d["match"]["status"] == "UNMATCHED" and d["match"]["reimburse_id"])][:10],
    })


@pages.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "", q: str = ""):
    rows = invoice_service.list_invoices(status=status or None, keyword=q or None)
    return templates.TemplateResponse(request, "invoices.html", {
        "request": request, "rows": rows,
        "status": status, "q": q,
        "counts": invoice_service.invoice_status_counts(),
    })


@pages.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail_page(request: Request, invoice_id: int):
    row = invoice_service.invoice_detail(invoice_id)
    if row is None:
        return RedirectResponse("/invoices", status_code=302)
    return templates.TemplateResponse(request, "invoice_detail.html", {
        "request": request, "inv": row,
    })


@pages.get("/reimburses", response_class=HTMLResponse)
def reimburses_page(request: Request, batch_id: str = "", q: str = ""):
    rows = reimburse_service.list_reimburses(batch_id=batch_id or None, keyword=q or None)
    batches = reimburse_service.list_batches()
    columns = _display_columns(rows)
    for r in rows:
        r["cells"] = [_cell_value(r, c) for c in columns]
    _sort_rows(rows, columns)
    return templates.TemplateResponse(request, "reimburses.html", {
        "request": request, "rows": rows, "batches": batches,
        "batch_id": batch_id, "q": q, "columns": columns,
    })


def _sort_rows(rows: list, columns: list) -> None:
    """优先按「序号」从小到大排序；无序号时按导入顺序（id 升序）。"""
    idx = next((i for i, c in enumerate(columns) if c.get("label") == "序号"), None)
    if idx is None:
        rows.sort(key=lambda r: r.get("id") or 0)
        return

    def key(r):
        s = r["cells"][idx]["text"]
        try:
            return (0, float(s))
        except (ValueError, TypeError):
            return (1, s)

    rows.sort(key=key)


def _display_columns(rows: list) -> list:
    """数据列按模板字段顺序生成：取行中最常用的模板，兜底默认模板。"""
    from collections import Counter
    from app.service.template_service import template_service
    tpl_ids = [r.get("template_id") for r in rows if r.get("template_id")]
    tpl = template_service.get(Counter(tpl_ids).most_common(1)[0][0]) if tpl_ids else None
    if tpl is None:
        tpl = template_service.get_default()
    cols = []
    for f in ((tpl or {}).get("fields_config") or {}).get("fields", []):
        label = f.get("excel_header") or f.get("display_name") or f.get("system_key") or ""
        if f.get("system_key") == "remark" or label == "备注":
            continue  # 备注不单独成列，不匹配原因统一在「说明」列展示
        if f.get("is_custom"):
            cols.append({"label": label, "custom": f.get("display_name") or label})
        else:
            cols.append({"label": label, "key": f.get("system_key")})
    return cols


def _cell_value(row: dict, col: dict) -> dict:
    """单元格智能渲染：金额保留两位小数、长文本标记（>18字符）、空值。"""
    if "custom" in col:
        v = (row.get("extra_data") or {}).get(col["custom"], "")
    else:
        v = row.get(col["key"])
    if v is None or v == "":
        return {"text": "", "amount": False, "long": False}
    s = str(v).strip()
    if s.endswith(" 00:00:00"):
        s = s[:-9]
    is_amount = col.get("key") == "amount"
    if is_amount:
        try:
            s = f"{float(s.replace(',', '')):.2f}"
        except (ValueError, TypeError):
            pass
    return {"text": s, "amount": is_amount, "long": len(s) > 18}


@pages.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request):
    # 待人工处理：有报销单关联的 CONFLICT / UNMATCHED（含存量 SUSPICIOUS）；
    # 无候选发票：无报销单关联的 UNMATCHED（含汇总匹配 many_to_one）
    evals = match_service.list_matches(["SUSPICIOUS", "CONFLICT", "UNMATCHED"])
    pending = [m for m in evals if m["reimburse_id"] is not None or m["pair_reimburses"]]
    unmatched = [m for m in evals if m["reimburse_id"] is None and not m["pair_reimburses"]]
    active = match_service.list_matches(["MATCHED", "CONFIRMED"])
    options = match_service.matchable_options()
    return templates.TemplateResponse(request, "matches.html", {
        "request": request,
        "pending": pending, "active": active, "unmatched": unmatched,
        "opt_invoices": options["invoices"], "opt_reimburses": options["reimburses"],
        "counts": match_service.match_counts(),
    })


@pages.get("/report", response_class=HTMLResponse)
def report_page(request: Request):
    report = report_service.build_report()
    return templates.TemplateResponse(request, "report.html", {"request": request, "r": report})


@pages.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from app.repository import db
    from app.repository.repos import company_repo, rule_repo
    with db.read() as conn:
        companies = company_repo.list(conn)
        rules = rule_repo.list_all(conn)
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "companies": companies, "rules": rules,
    })

