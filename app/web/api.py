"""UI 层 - JSON API 路由。只做参数接收与结果返回，不含业务规则（§2 职责铁律）。"""
import json
from typing import List, Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response

from app.service import (invoice_service, match_service, reimburse_service,
                         report_service)

api = APIRouter(prefix="/api", tags=["api"])


def _j(data):
    return {"ok": True, "data": data}


# ---------------------------------------------------------------- 发票
@api.post("/invoices/upload")
async def upload_invoices(files: List[UploadFile] = File(...)):
    payload = []
    for f in files:
        payload.append((f.filename, f.content_type or "", await f.read()))
    return _j(invoice_service.import_files(payload))


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
async def import_reimburses(file: UploadFile = File(...)):
    content = await file.read()
    return _j(reimburse_service.import_excel(file.filename or "reimburses.xlsx", content))


@api.post("/reimburses/{reimburse_id}/delete")
async def delete_reimburse(reimburse_id: int, operator: str = Form("admin")):
    return _j(reimburse_service.delete_reimburse(reimburse_id, operator))


@api.post("/reimburses/batch/{batch_id}/delete")
async def delete_batch(batch_id: str, operator: str = Form("admin")):
    return _j(reimburse_service.delete_batch(batch_id, operator))


# ---------------------------------------------------------------- 匹配
@api.post("/match/run")
async def run_match(operator: str = Form("admin")):
    return _j(match_service.run_matching(operator))


@api.post("/match/{match_id}/confirm")
async def confirm_match(match_id: int, operator: str = Form("admin"), note: str = Form("")):
    return _j(match_service.confirm(match_id, operator, note))


@api.post("/match/{match_id}/reject")
async def reject_match(match_id: int, operator: str = Form("admin"), note: str = Form("")):
    return _j(match_service.reject(match_id, operator, note))


@api.post("/match/{match_id}/unmatch")
async def unmatch_match(match_id: int, operator: str = Form("admin"), reason: str = Form("")):
    return _j(match_service.unmatch(match_id, operator, reason))


@api.post("/match/manual")
async def manual_match(invoice_id: int = Form(...), reimburse_id: int = Form(...),
                       operator: str = Form("admin")):
    return _j(match_service.manual_match(invoice_id, reimburse_id, operator))


# ---------------------------------------------------------------- 报告
@api.get("/invoices/export")
def export_invoices(status: str = "", q: str = ""):
    from datetime import datetime
    content = invoice_service.export_excel(status=status or None, keyword=q or None)
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
