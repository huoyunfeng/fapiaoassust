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
# 状态码统一转中文显示（发票状态 + 匹配状态）
STATUS_CN = {
    "PARSING": "解析中", "PARSED": "已解析", "NORMAL": "未匹配",
    "ABNORMAL": "异常", "DUPLICATE": "重复", "MATCHED": "已匹配",
    "PENDING": "匹配中", "SUSPICIOUS": "无候选", "CONFLICT": "字段冲突",
    "UNMATCHED": "无候选", "CONFIRMED": "已确认", "REJECTED": "已拒绝",
}
templates.env.filters["status_cn"] = lambda s: STATUS_CN.get(s, s)
# 匹配类型统一转中文显示
MATCH_TYPE_CN = {
    "exact": "精确匹配", "fuzzy": "模糊匹配", "composite": "组合匹配",
    "many_to_one": "多对一", "manual": "人工指定",
}
templates.env.filters["match_type_cn"] = lambda s: MATCH_TYPE_CN.get(s, s)
templates.env.filters["fromjson"] = lambda s: (json.loads(s) if isinstance(s, str) and s else (s if isinstance(s, dict) else {}))


# ---------------------------------------------------------------- 分页
PAGE_SIZES = (20, 50, 100, 200)   # 预设每页条数（前端下拉选项）
MAX_PAGE_SIZE = 1000               # 自定义条数上限，防止一次渲染过多行卡死浏览器


def _norm_page_size(page_size: int) -> int:
    """每页条数：预设值或 1~1000 的自定义值，非法值回退 20。"""
    try:
        n = int(page_size)
    except (TypeError, ValueError):
        return 20
    return n if 1 <= n <= MAX_PAGE_SIZE else 20


def _paginate(items: list, page: int, page_size: int) -> tuple:
    """内存分页：返回 (当前页数据, 分页信息)。页码越界自动收敛到合法范围。"""
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    pager = {"total": total, "page": page, "pages": pages, "page_size": page_size}
    return items[start:start + page_size], pager


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
def invoices_page(request: Request, status: str = "", q: str = "",
                  category: str = "", date_from: str = "", date_to: str = "",
                  page: int = 1, page_size: int = 20, focus_id: int = 0):
    # 路由联动语义参数：unmatched 映射为 NORMAL（未匹配发票 = NORMAL 状态）
    if status == "unmatched":
        status = "NORMAL"
    page_size = _norm_page_size(page_size)
    status_f, kw = status or None, q or None
    category_f = category or None
    date_from_f, date_to_f = date_from or None, date_to or None
    total = invoice_service.count_invoices(status=status_f, keyword=kw,
                                           category=category_f,
                                           date_from=date_from_f, date_to=date_to_f)
    # focus 定位：目标行可能不在第 1 页，先算它所在的页码
    if focus_id:
        page = invoice_service.page_of_invoice(focus_id, status_f, kw, page_size,
                                               category=category_f,
                                               date_from=date_from_f,
                                               date_to=date_to_f) or page
    pages_n = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages_n)
    rows = invoice_service.list_invoices(status=status_f, keyword=kw,
                                         category=category_f,
                                         date_from=date_from_f, date_to=date_to_f,
                                         limit=page_size, offset=(page - 1) * page_size)
    return templates.TemplateResponse(request, "invoices.html", {
        "request": request, "rows": rows,
        "status": status, "q": q,
        "category": category, "date_from": date_from, "date_to": date_to,
        "counts": invoice_service.invoice_status_counts(),
        "pager": {"total": total, "page": page, "pages": pages_n,
                  "page_size": page_size},
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
def reimburses_page(request: Request, batch_id: str = "", q: str = "",
                    status: str = "", page: int = 1, page_size: int = 20,
                    focus_id: int = 0):
    rows = reimburse_service.list_reimburses(batch_id=batch_id or None, keyword=q or None)
    # 路由联动语义参数：按匹配状态过滤（matched 口径与差异报告一致）
    if status == "unmatched":
        rows = [r for r in rows if not r.get("matched")]
    elif status == "matched":
        rows = [r for r in rows if r.get("matched")]
    batches = reimburse_service.list_batches()
    columns = _display_columns(rows)
    for r in rows:
        r["cells"] = [_cell_value(r, c) for c in columns]
    _sort_rows(rows, columns)
    # 分页（排序后切片；focus 定位先算目标行所在页码）
    page_size = _norm_page_size(page_size)
    if focus_id:
        idx = next((i for i, r in enumerate(rows) if r.get("id") == focus_id), None)
        if idx is not None:
            page = idx // page_size + 1
    rows, pager = _paginate(rows, page, page_size)
    return templates.TemplateResponse(request, "reimburses.html", {
        "request": request, "rows": rows, "batches": batches,
        "batch_id": batch_id, "q": q, "status": status, "columns": columns,
        "pager": pager,
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
def report_page(request: Request, page_size: int = 20,
                dpage: int = 1, rpage: int = 1, ipage: int = 1,
                apage: int = 1, xpage: int = 1):
    report = report_service.build_report()
    page_size = _norm_page_size(page_size)
    # 各数据表格独立分页（互不影响的页码参数）
    pagers = {}
    for key, param, p in (("details", "dpage", dpage),
                          ("unmatched_reimburses", "rpage", rpage),
                          ("unmatched_invoices", "ipage", ipage),
                          ("abnormal", "apage", apage),
                          ("duplicates", "xpage", xpage)):
        report[key], pagers[param] = _paginate(report[key], p, page_size)
    return templates.TemplateResponse(request, "report.html", {
        "request": request, "r": report, "pagers": pagers,
    })


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

