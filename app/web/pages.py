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
    "PENDING": "badge-gray", "SUSPICIOUS": "badge-orange", "CONFLICT": "badge-red",
    "UNMATCHED": "badge-red", "CONFIRMED": "badge-green", "REJECTED": "badge-gray",
}
templates.env.filters["badge"] = lambda s: STATUS_BADGE.get(s, "badge-gray")
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
        "suspicious": [d for d in report["details"] if d["match"]["status"] in ("SUSPICIOUS", "CONFLICT")][:10],
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
    # 收集自定义字段列名（按出现顺序去重），用于动态表头
    extra_cols: list = []
    for r in rows:
        for k in (r.get("extra_data") or {}):
            if k not in extra_cols:
                extra_cols.append(k)
    return templates.TemplateResponse(request, "reimburses.html", {
        "request": request, "rows": rows, "batches": batches,
        "batch_id": batch_id, "q": q, "extra_cols": extra_cols,
    })


@pages.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request):
    pending = match_service.list_matches(["SUSPICIOUS", "CONFLICT"])
    active = match_service.list_matches(["MATCHED", "CONFIRMED"])
    unmatched = match_service.list_matches(["UNMATCHED"])
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

