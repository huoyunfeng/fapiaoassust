"""UI 层 - JSON API 路由。只做参数接收与结果返回，不含业务规则（§2 职责铁律）。"""
import json
from typing import List, Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from starlette.background import BackgroundTask

from app.service import (audit_service, backup_service, category_service,
                         invoice_service, match_service, reimburse_service,
                         report_service)
from app.service.mapping_service import mapping_service
from app.service.template_service import template_service
from app.infra.logger import get_logger

logger = get_logger(__name__)

api = APIRouter(prefix="/api", tags=["api"])


def _j(data):
    return {"ok": True, "data": data}


def _stale(err) -> JSONResponse:
    """并发失效：409 + 固定错误码，前端据此提示「该数据已失效」并刷新。"""
    return JSONResponse(status_code=409,
                        content={"ok": False, "code": "STALE", "message": err.message})


# ---------------------------------------------------------------- 发票
@api.post("/invoices/upload")
async def upload_invoices(files: List[UploadFile] = File(...), no_match: int = Form(0)):
    payload = []
    for f in files:
        payload.append((f.filename, f.content_type or "", await f.read()))
    # no_match=1: 前端逐文件上传以显示进度，导入完成后由前端统一触发一次匹配
    # 解析（尤其 OCR）耗时数秒，放线程池执行：事件循环保持畅通，删除等请求不被卡住
    return _j(await run_in_threadpool(
        invoice_service.import_files, payload, None, bool(no_match)))


@api.post("/invoices/manual")
async def manual_invoice(
    invoice_no: str = Form(""), invoice_code: str = Form(""),
    invoice_date: str = Form(""), buyer_name: str = Form(""),
    buyer_tax_no: str = Form(""), seller_name: str = Form(""),
    seller_tax_no: str = Form(""), amount: str = Form(""),
    tax_amount: str = Form(""), total_amount: str = Form(""),
    tax_rate: str = Form(""), item_summary: str = Form(""),
):
    fields = {
        "invoice_no": invoice_no, "invoice_code": invoice_code,
        "invoice_date": invoice_date, "buyer_name": buyer_name,
        "buyer_tax_no": buyer_tax_no, "seller_name": seller_name,
        "seller_tax_no": seller_tax_no, "amount": amount,
        "tax_amount": tax_amount, "total_amount": total_amount,
        "tax_rate": tax_rate, "item_summary": item_summary,
    }
    return _j(invoice_service.manual_create(fields))


@api.post("/invoices/{invoice_id}/fix")
async def fix_invoice(invoice_id: int, operator: str = Form("admin"),
                      reason: str = Form(""), payload: str = Form("{}")):
    try:
        fields = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return {"ok": True, "data": {"ok": False, "message": "payload 必须是合法 JSON"}}
    return _j(invoice_service.fix_abnormal(invoice_id, fields, operator, reason))


@api.post("/invoices/{invoice_id}/confirm-not-duplicate")
async def confirm_not_duplicate(invoice_id: int, operator: str = Form("admin"),
                                reason: str = Form("")):
    return _j(invoice_service.confirm_not_duplicate(invoice_id, operator, reason))


@api.post("/invoices/{invoice_id}/update-item-summary")
async def update_item_summary(invoice_id: int, item_summary: str = Form(""),
                              operator: str = Form("admin")):
    """双击编辑"报销内容"专用，任意状态都允许。"""
    return _j(invoice_service.update_item_summary(invoice_id, item_summary, operator))


@api.post("/invoices/{invoice_id}/category")
async def set_invoice_category(invoice_id: int, category: str = Form(""),
                               operator: str = Form("admin")):
    """确认/修正发票的费用分类（人工决策最高优先，并学习纠正记忆）。"""
    return _j(invoice_service.set_invoice_category(invoice_id, category, operator))


@api.get("/invoices/{invoice_id}/similar")
def similar_unmatched_invoices(invoice_id: int):
    """摘要相似且同为未匹配的发票（批量归类确认面板数据）。"""
    return _j(invoice_service.similar_unmatched(invoice_id))


@api.post("/invoices/category/batch")
async def batch_invoice_category(ids: str = Form(""), category: str = Form(""),
                                 operator: str = Form("admin")):
    """批量修改发票分类：只作用于未匹配发票（模块二）。"""
    invoice_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    return _j(invoice_service.set_invoices_category(invoice_ids, category, operator))


@api.post("/invoices/{invoice_id}/delete")
async def delete_invoice(invoice_id: int, operator: str = Form("admin")):
    return _j(invoice_service.delete_invoice(invoice_id, operator))


@api.post("/invoices/batch-delete")
async def batch_delete_invoices(ids: str = Form(""), operator: str = Form("admin")):
    invoice_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not invoice_ids:
        return _j({"ok": False, "message": "未选择要删除的发票"})
    return _j(invoice_service.delete_invoices(invoice_ids, operator))


# ---------------------------------------------------------------- 报销单
@api.post("/reimburses/import")
async def import_reimburses(file: UploadFile = File(...),
                            template_id: int = Form(None),
                            fields_config: str = Form("{}"),
                            remember_mapping: str = Form("1"),
                            on_duplicate: str = Form("")):
    """导入报销单 Excel。

    template_id: 指定模板 id（优先于默认模板）
    fields_config: JSON 字符串，临时字段映射（映射向导传入，优先级最高）
    remember_mapping: 是否回写映射缓存
    on_duplicate: 查重处置（空=先返回重复确认；skip=只导入不重复行；force=全部导入并打标）
    """
    content = await file.read()
    if not content:
        return {"ok": False, "message": "未收到文件，请重新选择 Excel"}
    try:
        fc = json.loads(fields_config) if fields_config else None
        if fc == {}:
            fc = None
    except json.JSONDecodeError:
        return {"ok": False, "message": "fields_config 必须是合法 JSON"}
    try:
        # Excel 解析+落库为阻塞操作，放线程池执行，避免卡住事件循环
        result = await run_in_threadpool(
            reimburse_service.import_excel,
            file.filename or "reimburses.xlsx", content,
            template_id=template_id,
            fields_config=fc,
            remember_mapping=remember_mapping == "1",
            on_duplicate=on_duplicate or None,
        )
    except Exception as e:
        logger.exception("报销单导入异常")
        return {"ok": False, "message": f"导入异常：{e}"}
    return _j(result)


@api.get("/reimburses/templates")
async def list_templates():
    """列出所有模板。"""
    return _j(template_service.list())


@api.post("/reimburses/templates")
async def create_template(name: str = Form(...),
                          description: str = Form(""),
                          fields_config: str = Form("{}"),
                          rules_config: str = Form("[]")):
    try:
        fc = json.loads(fields_config)
        rc = json.loads(rules_config)
    except json.JSONDecodeError:
        return {"ok": False, "message": "fields_config / rules_config 必须是合法 JSON"}
    return _j(template_service.create(name, fc, rc, description))


@api.put("/reimburses/templates/{template_id}")
async def update_template(template_id: int,
                          name: str = Form(None),
                          description: str = Form(None),
                          fields_config: str = Form(None),
                          rules_config: str = Form(None)):
    fc = rc = None
    try:
        if fields_config is not None:
            fc = json.loads(fields_config)
        if rules_config is not None:
            rc = json.loads(rules_config)
    except json.JSONDecodeError:
        return {"ok": False, "message": "fields_config / rules_config 必须是合法 JSON"}
    return _j(template_service.update(template_id, name, description, fc, rc))


@api.post("/reimburses/templates/{template_id}/copy")
async def copy_template(template_id: int, new_name: str = Form(...)):
    return _j(template_service.copy(template_id, new_name))


@api.post("/reimburses/templates/{template_id}/delete")
async def delete_template(template_id: int):
    return _j(template_service.delete(template_id))


@api.post("/reimburses/templates/{template_id}/set-default")
async def set_default_template(template_id: int):
    return _j(template_service.set_default(template_id))


@api.get("/reimburses/templates/{template_id}")
async def get_template(template_id: int):
    tpl = template_service.get(template_id)
    if tpl is None:
        return {"ok": False, "message": "模板不存在"}
    return _j(tpl)


@api.get("/reimburses/system-fields")
async def system_fields():
    """返回系统字段元数据（供前端映射向导下拉选择）。"""
    from app.service.template_service import system_fields_meta
    return _j(system_fields_meta())


@api.post("/reimburses/mapping/detect")
async def detect_mapping(file: UploadFile = File(...)):
    """上传 Excel，返回表头 + 表头所在行 + 自动映射方案（命中缓存 / 自动推断）。"""
    content = await file.read()
    from app.infra.excel import read_headers
    try:
        headers, header_row = read_headers(content)
    except Exception as e:
        return {"ok": False, "message": f"无法读取 Excel：{e}"}
    if not headers:
        return {"ok": False, "message": "无法识别表头，请确认文件含表头行"}
    result = mapping_service.detect(headers, header_row=header_row)
    return _j(result)


@api.post("/reimburses/mapping/build-fields")
async def build_fields_config(headers: str = Form("[]"), mapping: str = Form("{}"),
                              header_row: int = Form(1)):
    """由表头 + 用户映射生成 fields_config（用于动态创建模板/直接导入）。"""
    try:
        hs = json.loads(headers)
        mp = json.loads(mapping)
    except json.JSONDecodeError:
        return {"ok": False, "message": "headers / mapping 必须是合法 JSON"}
    return _j(mapping_service.build_fields_config(hs, mp, header_row))


@api.post("/reimburses/manual")
async def manual_reimburse(
    reimburse_no: str = Form(""), submitter: str = Form(""),
    reimburse_date: str = Form(""), period_start: str = Form(""),
    period_end: str = Form(""), expense_type: str = Form(""),
    amount: str = Form(""), invoice_no: str = Form(""),
    invoice_date: str = Form(""), remark: str = Form(""),
    operator: str = Form("admin"),
):
    """手动录入单条报销单（设计 §12.4 降级入口）。"""
    return _j(reimburse_service.manual_create({
        "reimburse_no": reimburse_no, "submitter": submitter,
        "reimburse_date": reimburse_date, "period_start": period_start,
        "period_end": period_end, "expense_type": expense_type,
        "amount": amount, "invoice_no": invoice_no,
        "invoice_date": invoice_date, "remark": remark,
    }, operator))


@api.post("/reimburses/{reimburse_id}/category")
async def set_reimburse_category(reimburse_id: int, category: str = Form(""),
                                 operator: str = Form("admin"),
                                 mode: str = Form("sync")):
    """报销单费用类型。

    未匹配：只改报销单自身分类，绝不影响发票（模块二）；
    已匹配：mode='self' 仅改报销单 / mode='sync' 同步全部关联发票（模块三）。
    """
    return _j(invoice_service.set_reimburse_category(reimburse_id, category, operator, mode))


@api.post("/reimburses/{reimburse_id}/delete")
async def delete_reimburse(reimburse_id: int, operator: str = Form("admin")):
    return _j(reimburse_service.delete_reimburse(reimburse_id, operator))


@api.post("/reimburses/batch/{batch_id}/delete")
async def delete_batch(batch_id: str, operator: str = Form("admin")):
    return _j(reimburse_service.delete_batch(batch_id, operator))


@api.get("/reimburses/batch/{batch_id}/ids")
async def list_batch_ids(batch_id: str):
    """批次内报销单 id 列表（前端逐条删除、显示进度用）。"""
    from app.repository import db
    from app.repository.repos import reimburse_repo
    with db.tx() as conn:
        rows = reimburse_repo.list(conn, batch_id=batch_id, limit=100000)
    return _j([r["id"] for r in rows])


# ---------------------------------------------------------------- 匹配
@api.post("/match/run")
async def run_match(operator: str = Form("admin")):
    return _j(await run_in_threadpool(match_service.run_matching, operator))


@api.post("/match/{match_id}/confirm")
async def confirm_match(match_id: int, operator: str = Form("admin"),
                        note: str = Form(""), expect_status: str = Form(""),
                        reason_option: str = Form(""), exempt: str = Form("")):
    try:
        return _j(match_service.confirm(match_id, operator, note, expect_status,
                                        reason_option, exempt == "1"))
    except match_service.StaleError as e:
        return _stale(e)


@api.post("/match/{match_id}/reject")
async def reject_match(match_id: int, operator: str = Form("admin"),
                       note: str = Form(""), expect_status: str = Form(""),
                       reason_option: str = Form("")):
    try:
        return _j(match_service.reject(match_id, operator, note, expect_status, reason_option))
    except match_service.StaleError as e:
        return _stale(e)


@api.post("/match/auto-correct")
async def auto_correct_match(match_id: int = Form(...), operator: str = Form("admin")):
    return _j(match_service.auto_correct(match_id, operator))


@api.post("/match/{match_id}/unmatch")
async def unmatch_match(match_id: int, operator: str = Form("admin"),
                        reason: str = Form(""), expect_status: str = Form(""),
                        reason_option: str = Form("")):
    try:
        return _j(match_service.unmatch(match_id, operator, reason, expect_status, reason_option))
    except match_service.StaleError as e:
        return _stale(e)


@api.post("/match/manual")
async def manual_match(invoice_id: int = Form(...), reimburse_id: int = Form(...),
                       operator: str = Form("admin"),
                       reason_option: str = Form(""), reason: str = Form(""),
                       exempt: str = Form("")):
    return _j(match_service.manual_match(invoice_id, reimburse_id, operator,
                                         reason_option, reason, exempt == "1"))


@api.post("/matches/{match_id}/exempt")
async def exempt_match(match_id: int, operator: str = Form("admin"),
                       reason: str = Form("")):
    """临时豁免：管理员强制绑定被规则阻断的匹配（理由必填，强制审计）。"""
    return _j(match_service.exempt_match(match_id, operator, reason))


# ---------------------------------------------------------------- 报告
@api.get("/report/data")
def report_data(filter: str = "", q: str = "", page_size: int = 20, dpage: int = 1,
                rpage: int = 1, ipage: int = 1, apage: int = 1, xpage: int = 1,
                dsize: str = "", rsize: str = "", isize: str = "",
                asize: str = "", xsize: str = ""):
    """差异报告状态驱动数据包（卡片统计 + 四类清单分页）——前端操作后全量刷新入口。

    dsize/rsize/isize/asize/xsize：各分页器独立的每页条数（主表与底部 Tab 互不干扰）。
    """
    from app.web.pages import _norm_page_size
    sizes = {"dpage": dsize, "rpage": rsize, "ipage": isize,
             "apage": asize, "xpage": xsize}
    return _j(report_service.report_payload(
        filter=filter or "", q=(q or "").strip(), page_size=_norm_page_size(page_size),
        dpage=dpage, rpage=rpage, ipage=ipage, apage=apage, xpage=xpage,
        page_sizes=sizes))


@api.get("/report/options")
def report_options(q: str = ""):
    """差异报告绑定向导下拉：未占用报销单（支持关键词搜索）。"""
    keyword = (q or "").strip().lower()
    reb_rows = match_service.matchable_options()["reimburses"]
    if keyword:
        reb_rows = [r for r in reb_rows
                    if keyword in " ".join(str(r.get(k) or "") for k in
                                           ("reimburse_no", "submitter", "expense_type", "amount")).lower()]
    return _j({
        "reimburses": [{"id": r["id"], "reimburse_no": r["reimburse_no"],
                        "amount": _num_or_none(r.get("amount")),
                        "submitter": r.get("submitter") or "",
                        "expense_type": r.get("expense_type") or ""}
                       for r in reb_rows],
    })


def _num_or_none(v):
    """报销金额字符串 -> float（前端做汇总金额比对），空值返回 None。"""
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@api.post("/report/quick-fix")
async def report_quick_fix(
    match_id: int = Form(None), invoice_id: int = Form(None),
    reimburse_id: int = Form(None),
    invoice_no: str = Form(""), invoice_total: str = Form(""),
    reb_amount: str = Form(""), reb_invoice_no: str = Form(""),
    reb_expense_type: str = Form(""),
    category: str = Form(""), force_bind: int = Form(0),
    operator: str = Form("admin"), reason: str = Form(""),
    expect_status: str = Form(""), reason_option: str = Form(""),
):
    """快捷修复：单点重匹配（改金额/补票号/改分类/强制绑定）。"""
    inv_fields, reb_fields = {}, {}
    if invoice_no.strip():
        inv_fields["invoice_no"] = invoice_no.strip()
    if invoice_total.strip():
        inv_fields["total_amount"] = invoice_total.strip()
    if reb_amount.strip():
        reb_fields["amount"] = reb_amount.strip()
    if reb_invoice_no.strip():
        reb_fields["invoice_no"] = reb_invoice_no.strip()
    if reb_expense_type.strip():
        reb_fields["expense_type"] = reb_expense_type.strip()
    try:
        return _j(match_service.quick_fix(
            match_id=match_id, invoice_id=invoice_id, reimburse_id=reimburse_id,
            inv_fields=inv_fields, reb_fields=reb_fields,
            category=category.strip(), force_bind=bool(force_bind),
            operator=operator, reason=reason, expect_status=expect_status,
            reason_option=reason_option))
    except match_service.StaleError as e:
        return _stale(e)


@api.post("/report/batch-bind")
async def report_batch_bind(invoice_ids: str = Form(""), reimburse_id: int = Form(...),
                            operator: str = Form("admin"),
                            reason_option: str = Form(""), reason: str = Form("")):
    """未报销发票批量绑定到报销单（复用人工匹配存法，match_type=manual）。"""
    ids = [int(x) for x in invoice_ids.split(",") if x.strip().isdigit()]
    try:
        return _j(match_service.manual_batch_bind(ids, reimburse_id, operator, reason_option, reason))
    except match_service.StaleError as e:
        return _stale(e)


@api.get("/invoices/export")
def export_invoices(status: str = "", q: str = "", ids: str = ""):
    from datetime import datetime
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()] if ids else None
    content = invoice_service.export_excel(
        status=status or None, keyword=q or None, ids=id_list)
    fname = f"invoices_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@api.get("/report/export")
def export_report(filter: str = "", q: str = "", cols: str = ""):
    """流式导出差异报告：与当前视图（筛选/关键词/可见列）保持一致。"""
    col_keys = [c.strip() for c in cols.split(",") if c.strip()] or None
    stream = report_service.export_excel_stream(filter or "", (q or "").strip(), col_keys)
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="report.xlsx"'},
    )


# ---------------------------------------------------------------- 发票详情抽屉
# 注意：必须声明在 GET /invoices/export 之后，避免 "export" 被当作 int 路径参数
@api.get("/invoices/{invoice_id}")
def invoice_preview_api(invoice_id: int):
    """抽屉详情：结构化字段 + 生效匹配记录 + 原件文件信息。"""
    data = invoice_service.invoice_preview(invoice_id)
    if data is None:
        return {"ok": False, "message": "发票不存在或已删除"}
    return _j(data)


# 可内联预览的原件类型（图片直接渲染，PDF 用浏览器内置查看器）
_FILE_INLINE_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


@api.get("/invoices/{invoice_id}/file")
def invoice_file_api(invoice_id: int):
    """发票原件文件流：图片/PDF 内联预览，XML/OFD 走附件下载。"""
    p = invoice_service.invoice_file_path(invoice_id)
    if p is None:
        return Response("原件文件不存在", status_code=404)
    ext = p.suffix.lower()
    mime = _FILE_INLINE_MIME.get(ext)
    if mime:
        return FileResponse(p, media_type=mime,
                            content_disposition_type="inline")
    return FileResponse(p, media_type="application/octet-stream",
                        filename=p.name, content_disposition_type="attachment")


@api.get("/invoices/{invoice_id}/preview.png")
def invoice_pdf_preview_api(invoice_id: int, page: int = 0):
    """PDF 原件逐页渲染为 PNG 图片（抽屉预览用，与图片发票共用同一预览组件）。"""
    png = invoice_service.render_pdf_page(invoice_id, page)
    if png is None:
        return Response("PDF 渲染失败", status_code=404)
    return Response(png, media_type="image/png")


# ---------------------------------------------------------------- 仪表盘
@api.get("/dashboard/expense-breakdown")
def dashboard_expense_breakdown(date_from: str = "", date_to: str = ""):
    data = invoice_service.expense_breakdown(
        date_from=date_from or None,
        date_to=date_to or None,
        top_n=5,
    )
    return _j(data)


# ---------------------------------------------------------------- 分类字典（模块一）
@api.get("/categories")
def list_categories(include_disabled: int = 1):
    return _j(category_service.list_categories(bool(include_disabled)))


@api.post("/categories")
async def add_category(name: str = Form(...), merge_into: str = Form(""),
                       force: int = Form(0), operator: str = Form("admin")):
    """新增自定义分类；相似词先返回 need_confirm 提示合并。"""
    return _j(category_service.add_category(name, merge_into, bool(force), operator))


@api.post("/categories/{category_id}/toggle")
async def toggle_category(category_id: int, operator: str = Form("admin")):
    """启用/禁用分类（分类只能禁用，不能物理删除；系统预设分类锁定）。"""
    return _j(category_service.toggle_category(category_id, operator))


@api.post("/categories/{category_id}/rename")
async def rename_category(category_id: int, name: str = Form(...),
                          force: int = Form(0), operator: str = Form("admin")):
    """重命名自定义分类（仅限未被历史数据使用；系统预设分类锁定）。"""
    return _j(category_service.rename_category(category_id, name, bool(force), operator))


@api.post("/categories/{category_id}/merge")
async def merge_category(category_id: int, target_id: int = Form(...),
                         operator: str = Form("admin")):
    """合并自定义分类：历史数据改为目标分类，源分类自动禁用（系统预设分类锁定）。"""
    return _j(category_service.merge_category(category_id, target_id, operator))


# ---------------------------------------------------------------- 分类冲突（模块四）
@api.get("/conflicts")
def list_conflicts(include_resolved: int = 1):
    return _j(category_service.list_conflicts(bool(include_resolved)))


@api.post("/conflicts/{reimburse_id}/resolve")
async def resolve_conflict(reimburse_id: int, mode: str = Form(...),
                           operator: str = Form("admin")):
    """冲突仲裁：reimburse=以报销单为准 / invoice=以发票为准 / keep=保留差异。"""
    return _j(category_service.resolve_conflict(reimburse_id, mode, operator))


# ---------------------------------------------------------------- 审计日志（模块六）
@api.get("/audit")
def audit_list(limit: int = 100, target_type: str = "", action: str = "",
               keyword: str = "", date_from: str = "", date_to: str = "",
               page: int = 1, page_size: int = 20):
    return _j(audit_service.list_recent(
        limit=limit,
        target_type=target_type or None,
        action=action or None,
        keyword=keyword or None,
        date_from=date_from or None,
        date_to=date_to or None,
        page=page,
        page_size=page_size,
    ))


# ---------------------------------------------------------------- 设置
@api.post("/companies")
async def add_company(name: str = Form(...), tax_no: str = Form(...),
                      is_default: str = Form("")):
    from app.repository import db
    from app.repository.repos import company_repo
    with db.tx() as conn:
        cid = company_repo.insert(conn, name.strip(), tax_no.strip().upper(),
                                  is_default == "1")
    return _j({"id": cid})


@api.post("/companies/{company_id}/delete")
async def delete_company(company_id: int):
    from app.repository import db
    from app.repository.repos import company_repo
    with db.tx() as conn:
        company_repo.delete(conn, company_id)
    return _j({"deleted": company_id})


@api.post("/rules")
async def add_rule(name: str = Form(...), rule_type: str = Form(...),
                   scope: str = Form("invoice"), action: str = Form("block"),
                   stop_on_fail: str = Form(""), enabled: str = Form("1"),
                   tags: str = Form("[]"), config: str = Form("{}")):
    """新增规则：优先级按 10/20/30… 自动生成，保存前做同作用域逻辑冲突校验。"""
    from app.domain.dto import RuleDTO
    from app.domain.rules_engine import (SCOPE_TYPES, TAG_CONFIG_KEYS,
                                         find_conflicts)
    from app.repository import db
    from app.repository.repos import rule_repo
    try:
        cfg = json.loads(config or "{}")
        tag_list = json.loads(tags or "[]")
        if not isinstance(cfg, dict) or not isinstance(tag_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "message": "config / tags 必须是合法 JSON"}
    if scope not in SCOPE_TYPES or rule_type not in SCOPE_TYPES[scope]:
        return {"ok": False, "message": "该规则类型不适用于所选校验作用域"}
    if rule_type in TAG_CONFIG_KEYS:
        cfg[TAG_CONFIG_KEYS[rule_type]] = [str(t).strip() for t in tag_list if str(t).strip()]

    candidate = RuleDTO(name=name.strip(), rule_type=rule_type, scope=scope,
                        action=action, config=cfg, tags=tag_list)
    with db.tx() as conn:
        existing = [RuleDTO(id=r["id"], name=r["name"], rule_type=r["rule_type"],
                            scope=r.get("scope") or "invoice",
                            action=r.get("action") or "block",
                            priority=r["priority"], config=r["config"],
                            enabled=bool(r["enabled"]))
                    for r in rule_repo.list_all(conn)]
        conflicts = find_conflicts(existing, candidate)
        if conflicts:
            return {"ok": False, "message": "规则逻辑冲突：" + "；".join(conflicts)}
        rid = rule_repo.insert(conn, {
            "name": name.strip(), "rule_type": rule_type, "scope": scope,
            "action": action, "priority": rule_repo.next_priority(conn),
            "stop_on_fail": stop_on_fail == "1", "config": cfg,
            "tags": tag_list, "enabled": enabled == "1",
        })
    return _j({"id": rid})


@api.post("/rules/reorder")
async def reorder_rules(ids: str = Form("")):
    """按前端上移/下移后的顺序重排：自动生成 10/20/30… 优先级。"""
    from app.repository import db
    from app.repository.repos import rule_repo
    order = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not order:
        return {"ok": False, "message": "未提供规则顺序"}
    with db.tx() as conn:
        rule_repo.reorder(conn, order)
    return _j({"ids": order})


@api.post("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int):
    from app.repository import db
    from app.repository.repos import rule_repo
    with db.tx() as conn:
        rule = rule_repo.get(conn, rule_id)
        if rule is None:
            return {"ok": False, "message": "规则不存在"}
        rule["enabled"] = not rule["enabled"]
        rule_repo.update(conn, rule_id, rule)
    return _j({"id": rule_id, "enabled": rule["enabled"]})


@api.post("/rules/{rule_id}/delete")
async def delete_rule(rule_id: int):
    from app.repository import db
    from app.repository.repos import rule_repo
    with db.tx() as conn:
        rule_repo.delete(conn, rule_id)
    return _j({"deleted": rule_id})


# ---------------------------------------------------------------- 数据备份与恢复
@api.get("/backup")
async def download_backup():
    """一键备份：数据库 + 全部发票附件打包为 zip 并触发浏览器下载。"""
    zip_path = await run_in_threadpool(backup_service.create_backup)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        background=BackgroundTask(zip_path.unlink, True),  # 发送完成后清理临时 zip
    )


@api.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    """恢复数据：上传 zip 覆盖数据库与附件（旧数据自动 .bak 保底，失败回滚）。"""
    content = await file.read()
    if not content:
        return _j({"ok": False, "message": "未收到备份文件，请重新选择"})
    if not (file.filename or "").lower().endswith(".zip"):
        return _j({"ok": False, "message": "仅支持 .zip 格式的备份文件"})
    # 覆盖解压为阻塞 IO，放线程池执行
    result = await run_in_threadpool(backup_service.restore_backup, content)
    return _j(result)
