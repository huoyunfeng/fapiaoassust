"""登录、角色隔离与商业化拦截中间件。

职责（对应设计第四部分）：
1. 首次启动无账号 -> 只能访问初始化页；
2. 未登录 -> 页面跳登录，接口返回 401；
3. 普通员工：只保留「仪表盘 / 我的发票」，其余页面与写接口一律 403；
4. 免费版：匹配中心、导出等受限功能前端拦截（页面跳升级页、接口 402）；
5. 团队管理 / 高级设置：仅管理员，团队管理还要求企业版。
"""
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.service import auth_service, license_service

PUBLIC_PAGES = {"/login", "/setup", "/forgot-password", "/recover", "/health"}
PUBLIC_API = {
    "/api/auth/login", "/api/auth/setup", "/api/auth/forgot", "/api/auth/recover",
    "/api/auth/logout",
}
# 员工可访问页面（其余业务页面均需财务及以上）
EMPLOYEE_PAGES = {"/", "/invoices", "/plans", "/recovery-code", "/change-password"}
EMPLOYEE_API_PREFIX = (
    "/api/auth/", "/api/license/status", "/api/license/plans",
    "/api/dashboard/expense-breakdown",
    "/api/invoices/upload", "/api/invoices/manual", "/api/invoices/",
)
# 员工允许的写接口（发票上传/手工录入/自己发票的分类；其余写操作一律 403）
EMPLOYEE_WRITE_PREFIX = (
    "/api/auth/", "/api/invoices/upload", "/api/invoices/manual",
)
FREE_BLOCKED = (
    ("/api/match", "matching"), ("/matches", "matching"),
    ("/api/invoices/export", "export"), ("/api/report/export", "export"),
)


def _html_403(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>无权限</title>
        <div style="font-family:system-ui;padding:60px;text-align:center;color:#303133">
        <h2 style="font-size:18px">无权限访问</h2>
        <p style="color:#909399;margin-top:10px">{message}</p>
        <a href="/" style="display:inline-block;margin-top:20px;color:#2563eb">返回仪表盘</a>
        </div>""",
        status_code=403,
    )


def _is_api(path: str) -> bool:
    return path.startswith("/api/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        user = auth_service.user_from_request(request)
        request.state.user = user

        # 1. 首次启动：没有账号时只允许初始化
        if not auth_service.has_users():
            if path in ("/setup", "/api/auth/setup", "/health"):
                return await call_next(request)
            return (JSONResponse({"ok": False, "message": "系统尚未初始化"},
                                 status_code=401)
                    if _is_api(path) else RedirectResponse("/setup"))

        # 2. 公开路由
        if path in PUBLIC_PAGES or path in PUBLIC_API:
            return await call_next(request)

        if not user:
            if _is_api(path):
                return JSONResponse({"ok": False, "code": "NO_LOGIN",
                                     "message": "登录已失效，请重新登录"},
                                    status_code=401)
            next_q = f"?next={path}" if path and path != "/" else ""
            return RedirectResponse(f"/login{next_q}")

        # 3. 首次初始化必须确认已保存恢复码（客户端弹窗 + 服务端兜底）
        if (user.get("role") == "admin" and auth_service.recovery_pending()
                and path not in ("/recovery-code", "/logout")
                and not path.startswith("/api/auth/")):
            if _is_api(path):
                return JSONResponse({"ok": False, "code": "RECOVERY_ACK",
                                     "message": "请先确认已保存应急恢复码"},
                                    status_code=428)
            return RedirectResponse("/recovery-code")

        # 4a. 强制改密：密码被重置后，除改密/退出外的入口一律拦截
        if user.get("must_change_pwd") and path not in ("/change-password", "/logout") \
                and not path.startswith("/api/auth/"):
            if _is_api(path):
                return JSONResponse({"ok": False, "code": "CHANGE_PASSWORD",
                                     "message": "请先修改初始密码"},
                                    status_code=428)
            return RedirectResponse("/change-password")

        # 4. 员工数据隔离：只放行仪表盘 / 我的发票，禁止任何删改
        if user.get("role") == "employee":
            if path.startswith("/api/"):
                # 员工无权接触财务侧数据（报销单 / 报告 / 导出 / 匹配）
                if path.startswith(("/api/reimburses", "/api/report",
                                    "/api/reports", "/api/match", "/api/matches",
                                    "/api/invoices/export", "/api/settings",
                                    "/api/audit")):
                    return JSONResponse({"ok": False, "message": "无权限访问该接口"},
                                        status_code=403)
                mutating = request.method not in ("GET", "HEAD", "OPTIONS")
                if mutating and not path.startswith(EMPLOYEE_WRITE_PREFIX):
                    return JSONResponse({"ok": False,
                                         "message": "普通员工无删除/修改权限"},
                                        status_code=403)
                if not path.startswith(EMPLOYEE_API_PREFIX):
                    return JSONResponse({"ok": False, "message": "无权限访问该接口"},
                                        status_code=403)
            elif path not in EMPLOYEE_PAGES and not path.startswith("/invoices/"):
                return _html_403("普通员工仅可访问仪表盘与我的发票")

        # 5. 免费版受限功能：前端拦截 + 服务端兜底
        for prefix, feature in FREE_BLOCKED:
            if path.startswith(prefix) and not license_service.feature_allowed(feature):
                if _is_api(path):
                    return JSONResponse(
                        {"ok": False, "code": "UPGRADE_REQUIRED",
                         "message": "免费版不支持该功能，请升级后使用"},
                        status_code=402)
                return RedirectResponse(f"/plans?feature={feature}")

        # 6. 管理员专属：团队管理 / 高级设置
        admin_prefix = ("/team", "/api/team", "/advanced", "/api/exchange",
                        "/api/storage")
        if path.startswith(admin_prefix):
            if not auth_service.can_manage_team(user):
                return (JSONResponse({"ok": False, "message": "仅管理员可操作"},
                                     status_code=403)
                        if _is_api(path) else _html_403("仅管理员可访问"))
            if path.startswith(("/team", "/api/team")) \
                    and not license_service.feature_allowed("team"):
                if _is_api(path):
                    return JSONResponse(
                        {"ok": False, "code": "UPGRADE_REQUIRED",
                         "message": "团队多账号为企业版专属能力，请升级"},
                        status_code=402)
                return RedirectResponse("/plans?feature=team")

        return await call_next(request)
