"""UI 层 - JSON API 路由。只做参数接收与结果返回，不含业务规则（§2 职责铁律）。"""
import json
from typing import List, Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from app.service import (backup_service, invoice_service, match_service,
                         reimburse_service, report_service)
from app.service.mapping_service import mapping_service
from app.service.template_service import template_service
from app.infra.logger import get_logger

logger = get_logger(__name__)

api = APIRouter(prefix="/api", tags=["api"])


def _j(data):
    return {"ok": True, "data": data}


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
                            remember_mapping: str = Form("1")):
    """导入报销单 Excel。

    template_id: 指定模板 id（优先于默认模板）
    fields_config: JSON 字符串，临时字段映射（映射向导传入，优先级最高）
    remember_mapping: 是否回写映射缓存
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
async def confirm_match(match_id: int, operator: str = Form("admin"), note: str = Form("")):
    return _j(match_service.confirm(match_id, operator, note))


@api.post("/match/{match_id}/reject")
async def reject_match(match_id: int, operator: str = Form("admin"), note: str = Form("")):
    return _j(match_service.reject(match_id, operator, note))


@api.post("/match/auto-correct")
async def auto_correct_match(match_id: int = Form(...), operator: str = Form("admin")):
    return _j(match_service.auto_correct(match_id, operator))


@api.post("/match/{match_id}/unmatch")
async def unmatch_match(match_id: int, operator: str = Form("admin"), reason: str = Form("")):
    return _j(match_service.unmatch(match_id, operator, reason))


@api.post("/match/manual")
async def manual_match(invoice_id: int = Form(...), reimburse_id: int = Form(...),
                       operator: str = Form("admin")):
    return _j(match_service.manual_match(invoice_id, reimburse_id, operator))


# ---------------------------------------------------------------- 报告
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
def export_report():
    report = report_service.build_report()
    content = report_service.export_excel(report)
    return Response(
        content,
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


# ---------------------------------------------------------------- 仪表盘
@api.get("/dashboard/expense-breakdown")
def dashboard_expense_breakdown(date_from: str = "", date_to: str = ""):
    data = invoice_service.expense_breakdown(
        date_from=date_from or None,
        date_to=date_to or None,
        top_n=5,
    )
    return _j(data)


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
                   priority: int = Form(100), stop_on_fail: str = Form(""),
                   enabled: str = Form("1"), config: str = Form("{}")):
    from app.repository import db
    from app.repository.repos import rule_repo
    try:
        cfg = json.loads(config or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "message": "config 必须是合法 JSON"}
    with db.tx() as conn:
        rid = rule_repo.insert(conn, {"name": name.strip(), "rule_type": rule_type,
                                      "priority": priority, "stop_on_fail": stop_on_fail == "1",
                                      "config": cfg, "enabled": enabled == "1"})
    return _j({"id": rid})


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
