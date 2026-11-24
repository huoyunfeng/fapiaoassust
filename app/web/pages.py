"""UI 层 - 页面路由（Jinja2 服务端渲染）。只做展示与交互，不含业务规则。"""
import json
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import APP_VERSION, RESOURCE_DIR
from app.service import (audit_service, auth_service, category_service,
                         demo_service, invoice_service, license_service,
                         match_service, reimburse_service, report_service)

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
def _attach_risk(rows: list) -> None:
    """把 risk_note 解码成模板可直接使用的标签与明细（坏数据安全降级）。"""
    from app.service import risk_service
    for r in rows:
        if isinstance(r, dict):
            tags, items = risk_service.decode_note(r.get("risk_note") or "")
            r["risk_tags"], r["risk_items"] = tags, items


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
                  cat_status: str = "",
                  range_: str = Query("", alias="range"),
                  page: int = 1, page_size: int = 20, focus_id: int = 0):
    # 路由联动语义参数：unmatched 映射为 NORMAL（未匹配发票 = NORMAL 状态）
    if status == "unmatched":
        status = "NORMAL"
    cat_status = cat_status if cat_status in ("pending",) else ""
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
                                           since=None, owner_user_id=owner,
                                           cat_status=cat_status)
    # focus 定位：目标行可能不在第 1 页，先算它所在的页码
    if focus_id:
        page = invoice_service.page_of_invoice(focus_id, status_f, kw, page_size,
                                               category=category_f,
                                               date_from=date_from_f,
                                               date_to=date_to_f,
                                               since=None,
                                               owner_user_id=owner,
                                               cat_status=cat_status) or page
    pages_n = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages_n)
    rows = invoice_service.list_invoices(status=status_f, keyword=kw,
                                         category=category_f,
                                         date_from=date_from_f, date_to=date_to_f,
                                         limit=page_size, offset=(page - 1) * page_size,
                                         since=None, owner_user_id=owner,
                                         cat_status=cat_status)
    _attach_risk(rows)
    return templates.TemplateResponse(request, "invoices.html", {
        "request": request, "rows": rows,
        "status": status, "q": q,
        "category": category, "cat_status": cat_status,
        "date_from": date_from_f or "", "date_to": date_to_f or "",
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
                    sort: str = "",
                    page: int = 1, page_size: int = 20, focus_id: int = 0):
    batches = reimburse_service.list_batches()
    # 视图唯一事实源：URL 的 batch_id。
    # 缺省与空串都表示「全部数据」全局视图（时间过滤可用）；
    # 只有在用户显式点击某个批次卡片/抽屉项后，才进入特定批次视图。
    batch_id = (batch_id or "").strip()

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

    # ===== 批次与时间过滤解耦 =====
    # 特定批次视图：时间过滤一律失效（批次本身就是数据范围），避免两套条件叠加后
    # 静默少数据。前端同时会把时间下拉框置灰并提示「时间筛选已锁定」。
    if batch_id:
        range_mode = "batch"
        since_f = None
        date_from_f = date_to_f = None

    rows = reimburse_service.list_reimburses(batch_id=batch_id or None,
                                             keyword=q or None,
                                             since=since_f,
                                             date_from=date_from_f,
                                             date_to=date_to_f)
    # 列表排序轮换：详见下方 _assign_seq 的 reverse 参数
    # 路由联动语义参数：按匹配状态过滤（matched 口径与差异报告一致）
    if status == "unmatched":
        rows = [r for r in rows if not r.get("matched")]
    elif status == "matched":
        rows = [r for r in rows if r.get("matched")]
    all_batches = not batch_id
    batch_tpl_id = None if all_batches else reimburse_service.batch_template_id(batch_id)
    columns = _display_columns(rows, all_batches, batch_tpl_id, batches)
    # 批次在页面上统一用「Excel 文件名去后缀」展示（与卡片一致），绝不暴露内部 batch_id
    batch_name = ""
    if batch_id:
        current = next((b for b in batches if b["batch_id"] == batch_id), None)
        batch_name = _batch_display_name(current) if current else batch_id
    # 列表排序轮换：asc = 最早的在前（首次进入），desc = 最新的在前（再次进入）。
    # 方向固化到 URL 的 sort 参数，删除后刷新沿用当前方向，不会突然翻转。
    _assign_seq(rows, batches if all_batches else None,
                reverse=(sort or "").lower() == "desc")
    for r in rows:
        r["cells"] = [_cell_value(r, c) for c in columns]
    # 列宽：按「表头 + 内容长度 + 预留留白」动态计算，与发票页 table-layout:fixed 一致。
    # 用分页前的完整数据算，翻页时列宽保持稳定，不会忽宽忽窄。
    col_widths = _column_widths(columns, rows)
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
    view_groups = None
    if all_batches:
        view_groups = []
        for r in rows:
            if r.get("_group_break") or not view_groups:
                view_groups.append({"name": r.get("_group_name") or "",
                                    "count": r.get("_group_count") or 0,
                                    "rows": []})
            view_groups[-1]["rows"].append(r)
    return templates.TemplateResponse(request, "reimburses.html", {
        "request": request, "rows": rows, "batches": batches,
        "batch_id": batch_id, "batch_name": batch_name,
        "q": q, "status": status, "columns": columns,
        "col_widths": col_widths,
        "view_groups": view_groups,
        "pager": pager, "categories": category_service.enabled_names(),
        "all_batches": all_batches,
        "range_mode": range_mode,
        "date_from": date_from_f or "",
        "date_to": date_to_f or "",
        "range_since": since_f or "",
    })


def _batch_display_name(batch: dict) -> str:
    """批次展示名：Excel 文件名去后缀（与卡片标题一致）。"""
    name = (batch.get("source_file") or "").strip()
    if name:
        return name.rsplit(".", 1)[0] if "." in name else name
    return batch.get("template_name") or "手动录入"


def _assign_seq(rows: list, batches: list = None, reverse: bool = False) -> None:
    """视觉序号：与 Excel / 数据库主键完全无关，只按当前展示顺序生成。

    - 具体批次：1,2,3… 全量连续（翻页自动衔接，如第二页 21 起）；
    - 全部批次：先按批次分组排序，每个批次各自从 1 开始。
    - reverse=True 时整体倒序（用于列表排序轮换），序号仍按展示顺序重排。
    """
    if batches is None:
        rows.sort(key=lambda r: r.get("id") or 0, reverse=reverse)
        for i, r in enumerate(rows, start=1):
            r["_seq"] = i
        return
    order = {b["batch_id"]: i for i, b in enumerate(batches)}
    rows.sort(key=lambda r: (order.get(r.get("batch_id"), 999), r.get("id") or 0),
              reverse=reverse)
    counts = {}
    for r in rows:
        b = r.get("batch_id") or ""
        counts[b] = counts.get(b, 0) + 1
    seq = {}
    for r in rows:
        b = r.get("batch_id") or ""
        seq[b] = seq.get(b, 0) + 1
        r["_seq"] = seq[b]
        # 分组标题用批次展示名（文件名去后缀），不展示模板名与内部批次号
        r["_group_name"] = _batch_display_name(r)
        r["_group_count"] = counts[b]


def _display_columns(rows: list, all_batches: bool = False,
                     batch_tpl_id: int = None, batches: list = None) -> list:
    """数据列生成。

    全部批次模式：合并所有批次模板的列（取并集），保证「A 有发票号、
    B 有项目名称」时两列都显示，缺失值留空；同名字段只出现一次。
    具体批次模式：严格按该批次绑定的模板字段渲染列头；批次无行时兜底默认模板。
    两种模式都只有唯一一列「序号」（前端视觉计数，key=_seq），
    数据库自增主键与 Excel 原始序号绝不进入表格。
    """
    from app.infra.excel import is_seq_header
    if all_batches:
        return _union_columns(batches)
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


def _union_columns(batches: list) -> list:
    """全部数据视图：合并所有批次模板的列（并集）。

    不同模板字段不同（如 A 有「发票号」、B 有「项目名称」）时取并集，
    两类数据都显示，缺失值由单元格渲染为「—」；同名字段只出现一次。
    列顺序：序号 / 报销单号 → 各模板字段（按批次新旧首次出现） → 费用类型。
    """
    from app.infra.excel import is_seq_header
    from app.service.template_service import template_service
    tpl_ids = []
    for b in (batches or []):
        tid = b.get("template_id")
        if tid and tid not in tpl_ids:
            tpl_ids.append(tid)
    cols, seen_keys, seen_labels = [], set(), set()

    def add(col: dict, label: str) -> None:
        key = col.get("key") or ("custom:" + str(col.get("custom")))
        if label in seen_labels or key in seen_keys:
            return
        seen_keys.add(key)
        seen_labels.add(label)
        cols.append(col)

    for tid in tpl_ids:
        tpl = template_service.get(tid)
        if not tpl:
            continue
        for f in ((tpl.get("fields_config") or {}).get("fields", [])):
            label = f.get("excel_header") or f.get("display_name") or f.get("system_key") or ""
            if not label or f.get("system_key") == "remark" or label == "备注":
                continue
            if is_seq_header(label) or f.get("system_key") in ("id", "_seq"):
                continue
            if f.get("is_custom"):
                add({"label": label, "custom": f.get("display_name") or label}, label)
            else:
                add({"label": label, "key": f.get("system_key")}, label)
    if not cols:
        # 无模板数据时回退到系统核心列，保证空库也有稳定表头
        cols = [
            {"label": "提交人", "key": "submitter"},
            {"label": "报销日期", "key": "reimburse_date"},
            {"label": "费用类型", "key": "expense_type"},
            {"label": "金额", "key": "amount"},
            {"label": "发票号", "key": "invoice_no"},
        ]
    if not any(c.get("key") == "expense_type" for c in cols):
        cols.append({"label": "费用类型", "key": "expense_type"})
    if not any(c.get("key") == "amount" for c in cols):
        cols.insert(2, {"label": "金额", "key": "amount"})
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


def _text_units(text) -> int:
    """显示宽度折算：中文/全角按 2，半角按 1（用于估算列宽）。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(text or ""))


def _column_widths(columns: list, rows: list) -> list:
    """按内容长度计算每列的基础像素宽度（含表头与单元格留白）。

    表格剩余空间由前端按各列基础宽度等比例放大，保证宽度分配均匀、
    不会让某一列独占空白；短列（姓名/状态）始终比长文本列窄。
    返回值在模板列之外追加「状态」「操作」两列（固定像素）。
    """
    PAD = 20   # 单元格左右内边距（16px）+ 呼吸空间
    CAP = 240  # 长文本列的内容宽度上限，超出部分省略号截断（悬停看全文）
    MIN_PX = {  # 语义列的最小宽度，保证表头/内容不被裁切
        "_seq": 44, "reimburse_no": 132, "submitter": 56,
        "reimburse_date": 96, "amount": 70, "expense_type": 88,
        "invoice_no": 120,
    }
    layout = []
    for i, c in enumerate(columns):
        head_px = _text_units(c.get("label")) * 6
        body_units = 0
        has_cat = False
        for r in rows:
            cells = r.get("cells") or []
            if i >= len(cells):
                continue
            cell = cells[i]
            text = cell.get("text") or ""
            if not text and isinstance(cell.get("cat"), dict):
                has_cat = True
                text = cell["cat"].get("category") or "选择分类"
            body_units = max(body_units, _text_units(text))
        body_px = min(body_units * 6, CAP)
        is_long = body_units > 16 and not has_cat
        content = max(head_px, body_px) + PAD
        if has_cat:
            content += 18       # 胶囊标签额外开销
        elif is_long:
            content += 8        # 长文本列额外留一点余量，尽量完整显示
        col = {"px": int(max(MIN_PX.get(c.get("key"), 60), content))}
        if is_long:
            # 长文本列（如项目名称）：按内容定宽，不再随表格放大，减少空白
            col["fit"] = True
        layout.append(col)
        # 对齐规范：金额右对齐；短字段居中更整齐；长文本左对齐便于阅读
        c["align"] = ("right" if c.get("key") == "amount"
                      else ("left" if body_units > 16 else "center"))
    layout.append({"px": 76})   # 状态徽标
    layout.append({"px": 64})   # 操作按钮
    return layout


def _category_payload(row: dict) -> dict:
    """费用类型单元格的分类载荷：报销单自身分类（可点击）+ 已匹配时的发票侧分类/冲突。"""
    category = row.get("category") or ""
    suggest = row.get("cat_suggest") or ""
    hint = row.get("cat_hint") or ""
    matched = bool(row.get("cat_matched"))
    inv_cat = row.get("cat_invoice_category") or ""
    conflict = bool(row.get("cat_conflict"))
    detail = row.get("cat_conflict_detail") or ""
    source = row.get("cat_source") or ""
    tips = []
    if source == "linked":
        tips.append("由关联发票继承而来（source=linked）")
    elif source == "legacy":
        tips.append("历史分类不在当前字典中，保留原文展示")
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
        "source": source,
        "version": int(row.get("cat_version") or 0),
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


def _pending_has_diff(m: dict) -> bool:
    """待人工行是否存在金额/分类差异（决定操作列是否提供「一键更正」）。"""
    from decimal import Decimal
    a, b = m.get("reimburse_amount"), m.get("total_amount")
    if a is not None and b is not None:
        try:
            if abs(Decimal(str(a)) - Decimal(str(b))) > Decimal("0.005"):
                return True
        except Exception:
            pass
    return "分类" in (m.get("message") or "")


@pages.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request):
    # 待人工处理：有报销单关联的 CONFLICT / UNMATCHED（含存量 SUSPICIOUS）；
    # 无候选发票：无报销单关联的 UNMATCHED（含汇总匹配 many_to_one）
    evals = match_service.list_matches(["SUSPICIOUS", "CONFLICT", "UNMATCHED"])
    pending = [m for m in evals if m["reimburse_id"] is not None or m["pair_reimburses"]]
    unmatched = [m for m in evals if m["reimburse_id"] is None and not m["pair_reimburses"]]
    active = match_service.list_matches(["MATCHED", "CONFIRMED"])
    # 操作列分流：有差异走「去处理」下拉（确认/一键更正/豁免/拒绝），无差异直接「确认匹配」
    for m in pending:
        m["has_diff"] = _pending_has_diff(m)
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


# ---------------------------------------------------------------- 内置示例（Demo）：空库一键看到「发现 ¥4,860 问题」报告
@pages.get("/demo/report", response_class=HTMLResponse)
def demo_report_page(request: Request):
    return templates.TemplateResponse(request, "demo_report.html", {
        "request": request, "report": demo_service.demo_report(),
    })


@pages.get("/demo/seed")
def demo_seed(request: Request):
    """一键生成样例数据并跳转到示例报告（GET 方便做成「看示例」直达链接）。"""
    demo_service.seed_demo()
    return RedirectResponse("/demo/report", status_code=303)


@pages.post("/demo/clear")
def demo_clear(request: Request):
    """清除全部样例数据，回到首页。"""
    demo_service.clear_demo()
    return RedirectResponse("/", status_code=303)


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
        # 席位按套餐账号数（免费/个人 1，企业 3），与设备数（devices）是两个维度
        context.update({
            "users": users,
            "license": lic,
            "seat_used": len(users),
            "seat_limit": int(lic.get("seat_limit", 1) or 1),
            "device_total": int(lic.get("device_total", 1) or 1),
            "device_role": lic.get("role", "master"),
        })
    return templates.TemplateResponse(request, "settings.html", context)


@pages.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """帮助与支持页面（所有角色可见，无需权限隔离）。"""
    return templates.TemplateResponse(request, "help.html", {
        "request": request,
        "app_version": APP_VERSION,
    })

