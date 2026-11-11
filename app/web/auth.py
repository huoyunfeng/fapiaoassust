"""登录 / 初始化 / 找回 / 授权 / 团队 / 高级设置（页面 + 接口）。"""
from io import BytesIO

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)

from app.service import (auth_service, exchange_service, license_service,
                         storage_service)
from app.web.pages import templates

auth = APIRouter(tags=["auth"])


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status)


def _body(body: dict, key: str, default: str = "") -> str:
    return str((body or {}).get(key, default) or "").strip()


# ---------------------------------------------------------------- 页面
@auth.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})


@auth.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if auth_service.has_users():
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "setup.html", {"request": request})


@auth.get("/forgot-password", response_class=HTMLResponse)
def forgot_page(request: Request):
    return templates.TemplateResponse(request, "forgot.html", {"request": request})


@auth.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request):
    return templates.TemplateResponse(request, "recover.html", {
        "request": request, "machine_code": license_service.status()["machine_code"],
    })


@auth.get("/recovery-code", response_class=HTMLResponse)
def recovery_code_page(request: Request):
    return templates.TemplateResponse(request, "recovery_code.html", {"request": request})


@auth.get("/plans", response_class=HTMLResponse)
def plans_page(request: Request, feature: str = ""):
    return templates.TemplateResponse(request, "plans.html", {
        "request": request, "feature": feature,
        "license": license_service.status(),
    })


@auth.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    return templates.TemplateResponse(request, "change_password.html", {
        "request": request,
    })


@auth.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    # 团队管理已并入「系统设置 → 团队与权限」，旧入口统一重定向，避免书签失效
    return RedirectResponse("/settings/team", status_code=302)


# ---------------------------------------------------------------- 登录 / 初始化
@auth.post("/api/auth/login")
async def api_login(body: dict):
    try:
        user = auth_service.authenticate(_body(body, "username"), _body(body, "password"))
    except auth_service.AuthError as e:
        return _err(str(e))
    resp = JSONResponse({"ok": True,
                         "redirect": "/change-password" if user.get("must_change_pwd") else "/",
                         "user": {"username": user["username"],
                                  "display_name": user["display_name"],
                                  "role": user["role"]}})
    auth_service.set_session_cookie(resp, user, bool(body.get("remember")))
    return resp


@auth.post("/api/auth/setup")
async def api_setup(body: dict):
    if auth_service.has_users():
        return _err("系统已初始化，请直接登录")
    try:
        info = auth_service.create_first_admin(
            _body(body, "username"), _body(body, "password"),
            _body(body, "display_name"))
    except auth_service.AuthError as e:
        return _err(str(e))
    resp = JSONResponse({"ok": True, "recovery_code": info["recovery_code"],
                         "redirect": "/recovery-code"})
    auth_service.set_session_cookie(resp, auth_service.get_user(info["user_id"]),
                                    bool(body.get("remember")))
    return resp


@auth.post("/api/auth/change-password")
async def api_change_password(request: Request, body: dict):
    """强制改密 / 自助改密：校验原密码后更新并刷新会话，旧会话立即失效。"""
    user = getattr(request.state, "user", None)
    if not user:
        return _err("请先登录", 401)
    try:
        updated = auth_service.change_password(
            user["id"], _body(body, "old_password"), _body(body, "new_password"))
    except auth_service.AuthError as e:
        return _err(str(e))
    resp = JSONResponse({"ok": True, "message": "密码已修改", "redirect": "/"})
    auth_service.set_session_cookie(resp, updated, False)
    return resp


@auth.post("/api/auth/logout")
async def api_logout():
    resp = JSONResponse({"ok": True, "redirect": "/login"})
    auth_service.clear_session_cookie(resp)
    return resp


@auth.post("/api/auth/forgot")
async def api_forgot(body: dict):
    """管理员通道：用户名 + 应急恢复码重置密码；员工通道由前端提示联系管理员。"""
    username = _body(body, "username")
    if _body(body, "channel", "admin") == "employee":
        return _err("员工账号请联系系统管理员重置密码")
    try:
        auth_service.reset_by_recovery(
            username, _body(body, "recovery_code"), _body(body, "new_password"))
    except auth_service.AuthError as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "message": "密码已重置，请重新登录"})


@auth.post("/api/auth/recover")
async def api_recover(body: dict):
    """超管解锁码：校验签名 + 机器码 + 有效期后，把管理员密码重置为初始密码。"""
    try:
        license_service.unlock_code_verify(_body(body, "unlock_code"))
        auth_service.reset_admin_to_initial()
    except (license_service.LicenseError, auth_service.AuthError) as e:
        return _err(str(e))
    return JSONResponse({"ok": True,
                         "message": f"已重置为初始密码 {license_service.initial_password()}，请重新登录"})


@auth.post("/api/auth/recovery-ack")
async def api_recovery_ack():
    auth_service.ack_recovery()
    return JSONResponse({"ok": True})


@auth.post("/api/auth/recovery-regenerate")
async def api_recovery_regenerate(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return _err("请先登录", 401)
    code = auth_service.regenerate_recovery(user["id"])
    auth_service.ack_recovery()
    return JSONResponse({"ok": True, "recovery_code": code})


# ---------------------------------------------------------------- 授权 / 升级
@auth.get("/api/license/status")
async def api_license_status():
    return JSONResponse({"ok": True, "data": license_service.status()})


@auth.get("/api/license/plans")
async def api_plans():
    return JSONResponse({"ok": True, "data": license_service.plan_public()})


@auth.post("/api/license/activate")
async def api_activate(request: Request, body: dict):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return _err("仅管理员可执行授权激活", 403)
    try:
        payload = license_service.activate(_body(body, "order_id"))
    except (license_service.CloudError, license_service.LicenseError) as e:
        return _err(str(e))
    return JSONResponse({"ok": True,
                         "message": f"激活成功：{license_service.PLANS.get(payload.get('plan'), {}).get('name', '已授权')}",
                         "data": license_service.status()})


@auth.post("/api/license/bind-sub")
async def api_bind_sub(request: Request, body: dict):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return _err("仅管理员可执行设备绑定", 403)
    try:
        payload = license_service.bind_sub_device(_body(body, "invite_code"))
    except (license_service.CloudError, license_service.LicenseError) as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "message": "子设备绑定成功",
                         "data": license_service.status()})


@auth.post("/api/payment/create")
async def api_payment_create(request: Request):
    """支付下单（管理员）：本地代理云端，返回支付二维码与商户订单号。"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return _err("仅管理员可发起购买", 403)
    body = await request.json()
    try:
        return JSONResponse(license_service.payment_create(body.get("plan_type", "")))
    except license_service.CloudError as e:
        return _err(str(e), 502)


@auth.post("/api/payment/check")
async def api_payment_check(request: Request):
    """主动查单（管理员）：验签通过才真正落地授权，绝不轻信云端支付状态。"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return _err("仅管理员可操作", 403)
    body = await request.json()
    try:
        return JSONResponse({"ok": True, **license_service.payment_check(
            body.get("trade_order_id", ""))})
    except license_service.LicenseError as e:
        return _err(f"授权异常：{e}", 400)
    except license_service.CloudError as e:
        return _err(str(e), 502)


@auth.post("/api/license/recover")
async def api_license_recover(request: Request, body: dict):
    """自助找回：输入订单号后 6 位，云端返回已购授权码并本地验签落地。"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return _err("仅管理员可执行授权找回", 403)
    try:
        result = license_service.recover_license(_body(body, "order_suffix"))
    except license_service.CloudError as e:
        return _err(str(e))
    except license_service.LicenseError as e:
        return _err(f"授权异常：{e}", 400)
    return JSONResponse({"ok": True, **result, "data": license_service.status()})


@auth.post("/api/license/deactivate")
async def api_license_deactivate(request: Request):
    """注销本机授权：清空本地授权码，恢复免费版（便于重新输入订单号测试）。"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return _err("仅管理员可注销授权", 403)
    license_service.clear_license()
    return JSONResponse({"ok": True, "message": "授权已注销，已恢复免费版",
                         "data": license_service.status()})


@auth.post("/api/license/sync")
async def api_license_sync():
    try:
        data = license_service.sync_license()
    except license_service.CloudError as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "data": data})


@auth.get("/api/license/invite")
async def api_license_invite():
    try:
        code = license_service.invite_code()
    except license_service.LicenseError as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "invite_code": code})


# ---------------------------------------------------------------- 团队管理
@auth.get("/api/team/users")
async def api_team_users():
    return JSONResponse({"ok": True, "data": auth_service.list_users()})


@auth.post("/api/team/users")
async def api_team_create(body: dict):
    try:
        uid = auth_service.create_user(
            _body(body, "username"), _body(body, "password"),
            _body(body, "role", "employee"), _body(body, "display_name"))
    except auth_service.AuthError as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "user_id": uid, "message": "账号已创建"})


@auth.post("/api/team/users/{user_id}/toggle")
async def api_team_toggle(user_id: int, body: dict):
    try:
        auth_service.set_active(user_id, bool(body.get("active")))
    except auth_service.AuthError as e:
        return _err(str(e))
    return JSONResponse({"ok": True})


@auth.post("/api/team/users/{user_id}/reset")
async def api_team_reset(user_id: int, body: dict):
    try:
        auth_service.reset_password(user_id, _body(body, "password"))
    except auth_service.AuthError as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "message": "密码已重置"})


@auth.delete("/api/team/users/{user_id}")
async def api_team_delete(user_id: int):
    try:
        auth_service.delete_user(user_id)
    except auth_service.AuthError as e:
        return _err(str(e))
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- 数据交接包
@auth.get("/api/exchange/export")
async def api_exchange_export():
    try:
        data = await run_in_threadpool(exchange_service.build_package)
    except Exception as e:
        return _err(f"导出失败：{e}", 500)
    return StreamingResponse(
        BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="fapiao_exchange.zip"'})


@auth.post("/api/exchange/import")
async def api_exchange_import(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        return _err("未收到交接包文件")
    try:
        result = await run_in_threadpool(exchange_service.import_package, content)
    except Exception as e:
        return _err(f"导入失败：{e}")
    return JSONResponse({"ok": True, "data": result})


# ---------------------------------------------------------------- 附件目录迁移
@auth.post("/api/storage/pick-folder")
async def api_pick_folder():
    folder = await run_in_threadpool(storage_service.pick_folder)
    if not folder:
        return _err("未选择目录")
    return JSONResponse({"ok": True, "path": folder})


@auth.post("/api/storage/migrate")
async def api_storage_migrate(body: dict):
    try:
        result = await run_in_threadpool(storage_service.migrate,
                                         _body(body, "path"))
    except storage_service.StorageError as e:
        return _err(str(e))
    return JSONResponse({"ok": True, "data": result})
