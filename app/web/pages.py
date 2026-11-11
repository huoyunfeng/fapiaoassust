"""UI 层 - 页面路由（Jinja2 服务端渲染）。只做展示与交互，不含业务规则。"""
import json
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import APP_VERSION, RESOURCE_DIR
from app.service import (audit_service, auth_service, category_service,
                         invoice_service, license_service, match_service,
                         reimburse_service, report_service)

pages = APIRouter(tags=["pages"])
# frozen 时模板在 PyInstaller 解压目录下的 app/web/templates
templates = Jinja2Templates(directory=str(RESOURCE_DIR / "app" / "web" / "templates"))

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
# 模板内可直接读取当前授权状态（左侧导航的套餐标识 / 升级入口）
templates.env.globals["license_status"] = license_service.status
# 模板内可直接判断某个付费能力是否开通（导航锁定态 / 按钮置灰 / 升级引导）
templates.env.globals["feature_allowed"] = license_service.feature_allowed
templates.env.filters["fromjson"] = lambda s: (json.loads(s) if isinstance(s, str) and s else (s if isinstance(s, dict) else {}))
# 业务规则中文展示（设置页规则列表）
RULE_TYPE_CN = {
    "buyer_tax_no": "购买方税号白名单", "seller_blacklist": "销方黑名单",
    "sensitive_words": "敏感词拦截", "period": "开票日期范围",
    "amount": "金额区间", "cross_period": "跨期限制",
    "red_flush": "红冲拦截", "dedup": "红冲拦截", "custom": "自定义",
}
RULE_SCOPE_CN = {"invoice": "单张发票", "reimburse": "单笔报销单"}
RULE_ACTION_CN = {"block": "阻断", "warn": "警告"}
templates.env.filters["rule_type_cn"] = lambda s: RULE_TYPE_CN.get(s, s)
templates.env.filters["rule_scope_cn"] = lambda s: RULE_SCOPE_CN.get(s, s)
templates.env.filters["rule_action_cn"] = lambda s: RULE_ACTION_CN.get(s, s)


def _rule_cfg_cn(cfg) -> str:
    """规则配置一栏的人话摘要（不展示 JSON）。"""
    if not isinstance(cfg, dict):
        return "—"
    tags = cfg.get("tax_nos") or cfg.get("sellers") or cfg.get("words")
    if tags:
        return "、".join(str(t) for t in tags)
    lo, hi = cfg.get("min"), cfg.get("max")
    if lo not in (None, "") and hi not in (None, ""):
        return f"{lo} ~ {hi} 元"
    if lo not in (None, ""):
        return f"不低于 {lo} 元"
    if hi not in (None, ""):
        return f"不超过 {hi} 元"
    start, end = cfg.get("start"), cfg.get("end")
    if start or end:
        return f"{start or '不限'} ~ {end or '不限'}"
    return "—"


templates.env.filters["rule_cfg_cn"] = _rule_cfg_cn


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
    # 数据隔离：员工仪表盘只统计自己上传的发票
    user = getattr(request.state, "user", None)
    owner = None if auth_service.is_staff(user) else (user or {}).get("id")
    inv_counts = invoice_service.invoice_status_counts(owner_user_id=owner)
    mc = {} if owner else match_service.match_counts()
    report = report_service.build_report(owner_user_id=owner)
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


def _range_since(range_: str) -> Optional[str]:
    """列表冷热分离下界（业务日期口径）。

    用于报销单侧：所有快捷选项都走下界过滤，保留无 reimburse_date 的行
    （手动导入/批次的报销单常有空日期，严格 BETWEEN 会静默排除它们）。

    返回 None 表示不限制下界。
    """
    today = date.today()
    r = (range_ or "").strip().lower()
    if r == "all":
        return None
    if r == "this_year":
        return date(today.year, 1, 1).isoformat()
    if r == "this_month":
        return date(today.year, today.month, 1).isoformat()
    if r == "last_month":
        # 上个月 1 号（上界在上层调用方显式处理；此处只给下界）
        first = _months_ago(date(today.year, today.month, 1), 1)
        return first.isoformat()
    if r == "last_3m":
        return _months_ago(today, 3).isoformat()
    # 默认 1y
    return (today - timedelta(days=365)).isoformat()


def _last_day_of_month(y: int, m: int) -> date:
    """返回 y 年 m 月的最后一天。"""
    if m == 12:
        return date(y, 12, 31)
    return date(y, m + 1, 1) - timedelta(days=1)


def _months_ago(d: date, months: int) -> date:
    """将 d 往前推 months 个月，日数自动 clamp 到目标月的最后一天（避免 1/31 越界）。"""
    total = d.year * 12 + (d.month - 1) - months
    y, m = divmod(total, 12)
    m += 1
    last = _last_day_of_month(y, m)
    return date(y, m, min(d.day, last.day))


def _range_bounds(range_: str):
    """将时间快捷选项映射为 (date_from, date_to)。

    纯日历计算（本月/上月/近三月/今年/近一年），不依赖任何具体业务字段。
    调用方自行约定是开票日期还是报销日期口径。

    返回 (None, None) 表示不限制；date_to 为 None 表示只设下界。
    """
    today = date.today()
    r = (range_ or "").strip().lower()
    if r == "this_month":
        return (date(today.year, today.month, 1).isoformat(),
                _last_day_of_month(today.year, today.month).isoformat())
    if r == "last_month":
        first = _months_ago(date(today.year, today.month, 1), 1)
        return (first.isoformat(),
                _last_day_of_month(first.year, first.month).isoformat())
    if r == "last_3m":
        return (_months_ago(today, 3).isoformat(), today.isoformat())
    if r == "this_year":
        return (date(today.year, 1, 1).isoformat(),
                date(today.year, 12, 31).isoformat())
    if r == "1y":
        return ((today - timedelta(days=365)).isoformat(), today.isoformat())
    # all 或未知：不限制
    return (None, None)


@pages.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "", q: str = "",
                  category: str = "", date_from: str = "", date_to: str = "",
                  range_: str = Query("", alias="range"),
                  page: int = 1, page_size: int = 20, focus_id: int = 0):
    # 路由联动语义参数：unmatched 映射为 NORMAL（未匹配发票 = NORMAL 状态）
    if status == "unmatched":
        status = "NORMAL"
    page_size = _norm_page_size(page_size)
    status_f, kw = status or None, q or None
    category_f = category or None

    # ===== 时间范围解析：快捷选项 → (date_from, date_to)，均按开票日期 =====
    r = (range_ or "").strip().lower()
    date_from_f, date_to_f = date_from or None, date_to or None
    has_drill_dates = bool(date_from_f or date_to_f)

    if r == "all":
        date_from_f = date_to_f = None
        range_mode = "all"
    elif r == "custom":
        # 自定义时间：使用表单/下钻传入的起止日期
        range_mode = "custom"
    elif r in ("this_month", "last_month", "last_3m", "this_year", "1y"):
        # 快捷选项：计算起止日期，覆盖下钻传入的日期
        date_from_f, date_to_f = _range_bounds(r)
        range_mode = r
    else:
        # 未指定 range：若有下钻日期则沿用（视为自定义），否则默认近 1 年
        if has_drill_dates:
            range_mode = "custom"
        else:
            date_from_f, date_to_f = _range_bounds("1y")
            range_mode = "1y"

    # 数据隔离：员工强制只看自己上传的发票
    user = getattr(request.state, "user", None)
    owner = None if auth_service.is_staff(user) else (user or {}).get("id")
    total = invoice_service.count_invoices(status=status_f, keyword=kw,
                                           category=category_f,
                                           date_from=date_from_f, date_to=date_to_f,
                                           since=None, owner_user_id=owner)
    # focus 定位：目标行可能不在第 1 页，先算它所在的页码
    if focus_id:
        page = invoice_service.page_of_invoice(focus_id, status_f, kw, page_size,
                                               category=category_f,
                                               date_from=date_from_f,
                                               date_to=date_to_f,
                                               since=None,
                                               owner_user_id=owner) or page
    pages_n = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages_n)
    rows = invoice_service.list_invoices(status=status_f, keyword=kw,
                                         category=category_f,
                                         date_from=date_from_f, date_to=date_to_f,
                                         limit=page_size, offset=(page - 1) * page_size,
                                         since=None, owner_user_id=owner)
    return templates.TemplateResponse(request, "invoices.html", {
        "request": request, "rows": rows,
        "status": status, "q": q,
        "category": category, "date_from": date_from_f or "", "date_to": date_to_f or "",
        "range_mode": range_mode,
        "categories": category_service.enabled_names(),
        "counts": invoice_service.invoice_status_counts(owner_user_id=owner),
        "pager": {"total": total, "page": page, "pages": pages_n,
                  "page_size": page_size},
    })


@pages.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail_page(request: Request, invoice_id: int):
    row = invoice_service.invoice_detail(invoice_id)
    if row is None:
        return RedirectResponse("/invoices", status_code=302)
    # 数据隔离：员工不能查看他人的发票详情
    user = getattr(request.state, "user", None)
    if not auth_service.is_staff(user) and row.get("owner_user_id") != user["id"]:
        return RedirectResponse("/invoices", status_code=302)
    return templates.TemplateResponse(request, "invoice_detail.html", {
        "request": request, "inv": row,
    })


@pages.get("/reimburses", response_class=HTMLResponse)
def reimburses_page(request: Request, batch_id: Optional[str] = None, q: str = "",
                    status: str = "", date_from: str = "", date_to: str = "",
                    range_: str = Query("", alias="range"),
                    page: int = 1, page_size: int = 20, focus_id: int = 0):
    batches = reimburse_service.list_batches()
    # 默认激活最新批次：仅在 URL 完全没有 batch_id 参数时生效；
    # ?batch_id=（空串）是用户显式选择「全部批次」，不覆盖
    if batch_id is None:
        batch_id = batches[0]["batch_id"] if batches else ""

    # ===== 时间范围解析（按报销日期口径）=====
    # 与 invoices_page 的故意差异：
    #   1) 发票侧：发票必有开票日期，所有快捷选项都走严格 BETWEEN。
    #   2) 报销侧：大量报销单没有 reimburse_date（纯批次/手动导入），
    #      严格 BETWEEN 会静默排除它们，用户以为"数据没了"。
    #      → 所有快捷选项统一走下界 since（保留空日期行）。
    #      → 只有"自定义时间"是用户显式给了起止日期，才走严格 BETWEEN。
    r = (range_ or "").strip().lower()
    date_from_f, date_to_f = date_from or None, date_to or None
    has_drill_dates = bool(date_from_f or date_to_f)

    if r == "all":
        date_from_f = date_to_f = None
        range_mode = "all"
        since_f = None
    elif r == "custom":
        # 用户显式给了起止日期 → 严格 BETWEEN（可以有空字段：只给 date_from 或只给 date_to）
        range_mode = "custom"
        since_f = None
    elif r in ("1y", "this_year", "this_month", "last_month", "last_3m"):
        # 所有快捷选项：下界 since，保留无日期行
        date_from_f = date_to_f = None
        range_mode = r
        since_f = _range_since(r)
    else:
        # 未指定 range：若有下钻日期则沿用（视为自定义），否则默认近 1 年（冷热分离）
        if has_drill_dates:
            range_mode = "custom"
            since_f = None
        else:
            range_mode = "1y"
            since_f = _range_since("1y")
            date_from_f = date_to_f = None

    rows = reimburse_service.list_reimburses(batch_id=batch_id or None,
                                             keyword=q or None,
                                             since=since_f,
                                             date_from=date_from_f,
                                             date_to=date_to_f)
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
        "range_mode": range_mode,
        "date_from": date_from_f or "",
        "date_to": date_to_f or "",
        "range_since": since_f or "",
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


# ---------------------------------------------------------------- 系统设置（左右分栏 + 分区路由）
# 四个分区与左侧二级导航一一对应；/settings 统一收敛到默认分区「业务规则」
SETTINGS_SECTIONS = ("rules", "categories", "team", "security")


@pages.get("/settings")
def settings_root():
    return RedirectResponse("/settings/rules", status_code=302)


@pages.get("/settings/{section}", response_class=HTMLResponse)
def settings_page(request: Request, section: str):
    # 非法分区一律回到默认分区，避免出现空白设置页
    if section not in SETTINGS_SECTIONS:
        return RedirectResponse("/settings/rules", status_code=302)

    user = getattr(request.state, "user", None) or {}
    role = user.get("role")
    # 权限兜底（与前端菜单隐藏双保险）：
    # 员工仅可访问 业务规则 / 费用字典；团队与权限仅管理员可访问
    if section == "security" and role == "employee":
        return RedirectResponse("/settings/rules", status_code=302)
    if section == "team" and role != "admin":
        return RedirectResponse("/settings/rules", status_code=302)

    from app.repository import db
    from app.repository.repos import company_repo, rule_repo
    from app.service import backup_service
    with db.read() as conn:
        companies = company_repo.list(conn)
        rules = rule_repo.list_all(conn)

    context = {
        "request": request, "section": section,
        "companies": companies,
        "rules": [r for r in rules if not r.get("is_core")],
        "core_rules": [r for r in rules if r.get("is_core")],
        "categories": category_service.list_categories(include_disabled=True),
        # 首页仅展示最新 10 条概览；完整日志由前端通过 /api/audit 动态加载
        "audit_logs": audit_service.list_recent(limit=10)["items"],
        "app_version": APP_VERSION,
    }
    if section == "security":
        # 附件容量监控（uploads 物理体积，超过 5GB 时页面黄色预警）
        context["storage"] = backup_service.storage_usage()
    if section == "team":
        users = auth_service.list_users()
        lic = license_service.status()
        # 席位上限取自授权（sub_limit）；未下发时按「不限」展示，不阻断操作
        context.update({
            "users": users,
            "license": lic,
            "seat_used": len(users),
            "seat_limit": int(lic.get("sub_limit") or 0),
        })
    return templates.TemplateResponse(request, "settings.html", context)


@pages.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """帮助与支持页面（所有角色可见，无需权限隔离）。"""
    return templates.TemplateResponse(request, "help.html", {
        "request": request,
        "app_version": APP_VERSION,
    })

