"""登录、角色隔离与商业化拦截中间件。

职责（对应设计第四部分）：
1. 首次启动无账号 -> 只能访问初始化页；
2. 未登录 -> 页面跳登录，接口返回 401；
3. 普通员工：只保留「仪表盘 / 我的发票」，其余页面与写接口一律 403；
4. 免费版：匹配中心、导出等受限功能前端拦截（页面跳升级页、接口 402）；
5. 团队管理：仅管理员，且要求企业版。
"""
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.service import auth_service, license_service

PUBLIC_PAGES = {"/login", "/setup", "/forgot-password", "/recover", "/health"}
PUBLIC_API = {
    "/api/auth/login", "/api/auth/setup", "/api/auth/forgot", "/api/auth/recover",
    "/api/auth/logout",
    # 桌面端优雅退出：仅启动器持有随机令牌，鉴权在接口内部完成
    "/api/_shutdown",
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
    # 导出必须排在 /api/report 之前，否则会被 report 能力先匹配
    ("/api/invoices/export", "export"), ("/api/report/export", "export"),
    ("/api/match", "matching"), ("/matches", "matching"),
    ("/api/reimburses", "reimburse"), ("/reimburses", "reimburse"),
    ("/api/report", "report"), ("/report", "report"),
    ("/api/rules", "rules"),
    ("/api/audit", "audit"),
    ("/api/handover", "handover"),
)
# 只读模式下仍允许的写接口（续费激活、备份导出属于客户数据资产，绝不绑架）
READONLY_WRITE_WHITELIST = (
    "/api/auth/", "/api/license/", "/api/payment/", "/api/_shutdown",
    "/api/backup",
)
# 会员能力的中文名（403 提示 / 升级引导用词一致）
FREE_FEATURE_CN = {"matching": "智能匹配中心", "reimburse": "报销单管理",
                   "report": "差异报告", "export": "数据导出", "team": "团队与权限",
                   "rules": "智能风控规则", "audit": "审计日志",
                   "handover": "数据交接包", "custom_categories": "自定义费用字典"}


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
            elif path not in EMPLOYEE_PAGES and not path.startswith("/invoices/") \
                    and not path.startswith("/settings"):
                return _html_403("普通员工仅可访问仪表盘与我的发票")

        # 4b. 授权到期只读模式：登录、查看、导出、备份全部放行，写操作统一拦截
        readonly = license_service.readonly_mode()
        request.state.readonly = readonly
        if readonly and request.method not in ("GET", "HEAD", "OPTIONS") \
                and not path.startswith(READONLY_WRITE_WHITELIST):
            return JSONResponse(
                {"ok": False, "code": "READONLY",
                 "message": "授权已到期，系统处于只读模式。请前往「升级 / 授权」续费激活后再操作"},
                status_code=403)

        # 5. 免费版受限功能：前端拦截 + 服务端兜底（防抓包绕过）
        #    只读模式（授权到期）下不拦截读取类请求，客户数据资产必须随时可取
        for prefix, feature in FREE_BLOCKED:
            if readonly and request.method in ("GET", "HEAD", "OPTIONS"):
                break
            if path.startswith(prefix) and not license_service.feature_allowed(feature):
                if _is_api(path):
                    return JSONResponse(
                        {"ok": False, "code": "UPGRADE_REQUIRED",
                         "feature": feature,
                         "msg": "需要升级才能使用",
                         "message": f"{FREE_FEATURE_CN.get(feature, '该功能')}为付费能力，升级后立即解锁"},
                        status_code=403)
                return RedirectResponse(f"/plans?feature={feature}")

        # 5b. 自定义费用字典：免费版只保留基础字典，写操作一律拦截
        if path.startswith("/api/categories") \
                and request.method not in ("GET", "HEAD", "OPTIONS") \
                and not license_service.feature_allowed("custom_categories"):
            return JSONResponse(
                {"ok": False, "code": "UPGRADE_REQUIRED",
                 "feature": "custom_categories", "msg": "需要升级才能使用",
                 "message": "自定义费用字典为付费能力，升级后立即解锁"},
                status_code=403)

        # 6. 管理员专属：团队与权限 / 数据交接与存储迁移
        #    免费版允许打开「团队与权限」页面（展示升级引导），只拦截真正的读写接口
        admin_prefix = ("/team", "/settings/team", "/api/team",
                        "/api/exchange", "/api/storage")
        if path.startswith(admin_prefix):
            if not auth_service.can_manage_team(user):
                if _is_api(path):
                    return JSONResponse({"ok": False, "message": "仅管理员可操作"},
                                        status_code=403)
                # 页面统一回到「业务规则」，不把无权限用户留在空白页
                return RedirectResponse("/settings/rules", status_code=302)
            if path.startswith("/api/team") \
                    and not license_service.feature_allowed("team"):
                return JSONResponse(
                    {"ok": False, "code": "UPGRADE_REQUIRED",
                     "feature": "team",
                     "msg": "需要升级才能使用",
                     "message": "团队多账号为企业版专属能力，请升级"},
                    status_code=403)

        return await call_next(request)
