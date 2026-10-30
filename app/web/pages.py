"""UI 层 - 页面路由（Jinja2 服务端渲染）。只做展示与交互，不含业务规则。"""
import json
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.service import (audit_service, category_service, invoice_service,
                         match_service, reimburse_service, report_service)

pages = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS_BADGE = {
    "PARSING": "badge-gray", "PARSED": "badge-blue", "NORMAL": "badge-green",
    "ABNORMAL": "badge-red", "DUPLICATE": "badge-orange", "MATCHED": "badge-purple",
    "PENDING": "badge-gray", "SUSPICIOUS": "badge-yellow", "CONFLICT": "badge-red",
    "UNMATCHED": "badge-red", "CONFIRMED": "badge-green", "REJECTED": "badge-gray",
}
templates.env.filters["badge"] = lambda s: STATUS_BADGE.get(s, "badge-gray")
# SUSPICIOUS 为真实状态（模糊候选待人工确认），前端原样展示
templates.env.filters["dstatus"] = lambda s: s
# 状态码统一转中文显示（发票状态 + 匹配状态）
STATUS_CN = {
    "PARSING": "解析中", "PARSED": "已解析", "NORMAL": "未匹配",
    "ABNORMAL": "异常", "DUPLICATE": "重复", "MATCHED": "已匹配",
    "PENDING": "匹配中", "SUSPICIOUS": "待人工确认", "CONFLICT": "字段冲突",
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
    """每页条数：预设值或 1~1000 的自定义值；超上限钳制到 1000，非法值回退 20。"""
    try:
        n = int(page_size)
    except (TypeError, ValueError):
        return 20
    if n < 1:
        return 20
    return min(n, MAX_PAGE_SIZE)


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
        "categories": category_service.enabled_names(),
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
def reimburses_page(request: Request, batch_id: Optional[str] = None, q: str = "",
                    status: str = "", page: int = 1, page_size: int = 20,
                    focus_id: int = 0):
    batches = reimburse_service.list_batches()
    # 默认激活最新批次：仅在 URL 完全没有 batch_id 参数时生效；
    # ?batch_id=（空串）是用户显式选择「全部批次」，不覆盖
    if batch_id is None:
        batch_id = batches[0]["batch_id"] if batches else ""
    rows = reimburse_service.list_reimburses(batch_id=batch_id or None, keyword=q or None)
    # 路由联动语义参数：按匹配状态过滤（matched 口径与差异报告一致）
    if status == "unmatched":
        rows = [r for r in rows if not r.get("matched")]
    elif status == "matched":
        rows = [r for r in rows if r.get("matched")]
    all_batches = not batch_id
    batch_tpl_id = None if all_batches else reimburse_service.batch_template_id(batch_id)
    columns = _display_columns(rows, all_batches, batch_tpl_id)
    if all_batches:
        # 全部批次模式：附上每行的来源模板名（公共列「来源模板」）
        from app.service.template_service import template_service
        tpl_names = {t["id"]: t["name"] for t in template_service.list()}
        for r in rows:
            r["_template_name"] = tpl_names.get(r.get("template_id")) or "手动录入"
    _assign_seq(rows, batches if all_batches else None)
    for r in rows:
        r["cells"] = [_cell_value(r, c) for c in columns]
    # 分页（focus 定位先算目标行所在页码）
    page_size = _norm_page_size(page_size)
    if focus_id:
        idx = next((i for i, r in enumerate(rows) if r.get("id") == focus_id), None)
        if idx is not None:
            page = idx // page_size + 1
    rows, pager = _paginate(rows, page, page_size)
    # 全部批次模式：当前页内按批次分组，每组首行插入分组标题行
    prev_batch = None
    for r in rows:
        b = r.get("batch_id")
        r["_group_break"] = bool(all_batches and b != prev_batch)
        prev_batch = b
    return templates.TemplateResponse(request, "reimburses.html", {
        "request": request, "rows": rows, "batches": batches,
        "batch_id": batch_id, "q": q, "status": status, "columns": columns,
        "pager": pager, "categories": category_service.enabled_names(),
        "all_batches": all_batches,
    })


def _assign_seq(rows: list, batches: list = None) -> None:
    """视觉序号：与 Excel / 数据库主键完全无关，只按当前展示顺序生成。

    - 具体批次：1,2,3… 全量连续（翻页自动衔接，如第二页 21 起）；
    - 全部批次：先按批次分组排序，每个批次各自从 1 开始。
    """
    if batches is None:
        rows.sort(key=lambda r: r.get("id") or 0)
        for i, r in enumerate(rows, start=1):
            r["_seq"] = i
        return
    order = {b["batch_id"]: i for i, b in enumerate(batches)}
    rows.sort(key=lambda r: (order.get(r.get("batch_id"), 999), r.get("id") or 0))
    counts = {}
    for r in rows:
        b = r.get("batch_id") or ""
        counts[b] = counts.get(b, 0) + 1
    seq = {}
    for r in rows:
        b = r.get("batch_id") or ""
        seq[b] = seq.get(b, 0) + 1
        r["_seq"] = seq[b]
        r["_group_name"] = f"{r.get('_template_name') or '手动录入'} · {b}"
        r["_group_count"] = counts[b]


def _display_columns(rows: list, all_batches: bool = False,
                     batch_tpl_id: int = None) -> list:
    """数据列生成。

    全部批次模式：只输出核心公共列（序号/单号/来源模板/提交人/日期/费用类型/金额/发票号），
    隐藏各模板的专属列，避免不同模板列混杂错位。
    具体批次模式：严格按该批次绑定的模板字段渲染列头；批次无行时兜底默认模板。
    两种模式都只有唯一一列「序号」（前端视觉计数，key=_seq），
    数据库自增主键与 Excel 原始序号绝不进入表格。
    """
    from app.infra.excel import is_seq_header
    if all_batches:
        return [
            {"label": "序号", "key": "_seq"},
            {"label": "报销单号", "key": "reimburse_no"},
            {"label": "来源模板", "key": "_template"},
            {"label": "提交人", "key": "submitter"},
            {"label": "报销日期", "key": "reimburse_date"},
            {"label": "费用类型", "key": "expense_type"},
            {"label": "金额", "key": "amount"},
            {"label": "发票号", "key": "invoice_no"},
        ]
    from collections import Counter
    from app.service.template_service import template_service
    tpl = template_service.get(batch_tpl_id) if batch_tpl_id else None
    if tpl is None:
        tpl_ids = [r.get("template_id") for r in rows if r.get("template_id")]
        tpl = template_service.get(Counter(tpl_ids).most_common(1)[0][0]) if tpl_ids else None
    if tpl is None:
        tpl = template_service.get_default()
    cols = []
    for f in ((tpl or {}).get("fields_config") or {}).get("fields", []):
        label = f.get("excel_header") or f.get("display_name") or f.get("system_key") or ""
        if f.get("system_key") == "remark" or label == "备注":
            continue  # 备注不单独成列，不匹配原因统一在「说明」列展示
        if is_seq_header(label) or f.get("system_key") in ("id", "_seq"):
            continue  # 模板误映射的「序号/行号」列一律丢弃
        if f.get("is_custom"):
            cols.append({"label": label, "custom": f.get("display_name") or label})
        else:
            cols.append({"label": label, "key": f.get("system_key")})
    # 模板里没有「费用类型」列时补一列：系统会按发票推测并标记【待确认】，需财务核实
    if not any(c.get("key") == "expense_type" for c in cols):
        cols.append({"label": "费用类型", "key": "expense_type"})
    # 模块一：报销单号列固定在「序号」列右侧，且始终存在（模板未映射该列时由系统补列）
    cols = [c for c in cols if c.get("key") not in ("reimburse_no", "_seq")]
    cols.insert(0, {"label": "序号", "key": "_seq"})
    cols.insert(1, {"label": "报销单号", "key": "reimburse_no"})
    return cols


def _cell_value(row: dict, col: dict) -> dict:
    """单元格智能渲染：金额保留两位小数、长文本标记（>18字符）、空值。

    「费用类型」列不读报表单字段，而是输出所匹配发票的分类决策（cat 载荷），
    保证与发票页展示/修改同一口径。
    """
    if col.get("key") == "expense_type":
        return {"text": "", "amount": False, "long": False,
                "cat": _category_payload(row)}
    if col.get("key") == "_template":
        v = row.get("_template_name") or ""
    elif "custom" in col:
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


def _category_payload(row: dict) -> dict:
    """费用类型单元格的分类载荷：报销单自身分类（可点击）+ 已匹配时的发票侧分类/冲突。"""
    category = row.get("category") or ""
    suggest = row.get("cat_suggest") or ""
    hint = row.get("cat_hint") or ""
    matched = bool(row.get("cat_matched"))
    inv_cat = row.get("cat_invoice_category") or ""
    conflict = bool(row.get("cat_conflict"))
    detail = row.get("cat_conflict_detail") or ""
    tips = []
    if matched:
        tips.append("该报销单已匹配发票；修改分类将无条件同步所有关联发票（主从联动）")
        if inv_cat:
            tips.append("发票侧当前分类：%s" % inv_cat)
        if conflict:
            tips.append("⚠️ 分类差异：%s（可在匹配中心一键拉平或保留差异）" % (detail or "双方分类不一致"))
    else:
        tips.append("该报销单尚未匹配发票；此处修改只影响报销单自身分类，不会改动任何发票")
    return {
        "category": category,
        "status": row.get("cat_status") or "",
        "source": row.get("cat_source") or "",
        "suggest": suggest,
        "hint": hint,
        "invoice_id": row.get("cat_invoice_id"),
        "reimburse_id": row.get("id"),
        "matched": matched,
        "invoice_category": inv_cat,
        "conflict": conflict,
        "conflict_detail": detail,
        "tip": "；".join(tips),
    }


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
        "conflicts": category_service.list_conflicts(),
        "conflict_unresolved": category_service.count_unresolved(),
    })


@pages.get("/matches/{match_id}/compare", response_class=HTMLResponse)
def compare_page(request: Request, match_id: int):
    """并排比对：报销单业务特征 vs 推荐发票 / 平局候选（无票号场景）。"""
    data = report_service.compare_payload(match_id)
    if data is None:
        return RedirectResponse("/matches", status_code=303)
    return templates.TemplateResponse(request, "compare.html", {
        "request": request, "data": data,
    })


@pages.get("/report", response_class=HTMLResponse)
def report_page(request: Request, page_size: int = 20, filter: str = "", q: str = "",
                dpage: int = 1, rpage: int = 1, ipage: int = 1,
                apage: int = 1, xpage: int = 1):
    """差异报告（状态驱动工作台）：首屏直接注入数据包，后续交互走 /api/report/data 局部刷新。"""
    payload = report_service.report_payload(
        filter=filter if filter in ("pending", "active") else "",
        q=(q or "").strip(),
        page_size=_norm_page_size(page_size),
        dpage=dpage, rpage=rpage, ipage=ipage, apage=apage, xpage=xpage)
    return templates.TemplateResponse(request, "report.html", {
        "request": request, "initial": payload,
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
        "categories": category_service.list_categories(include_disabled=True),
        # 首页仅展示最新 10 条概览；完整日志由前端通过 /api/audit 动态加载
        "audit_logs": audit_service.list_recent(limit=10)["items"],
    })

