"""三版本权限矩阵 + 商业化拦截逻辑测试。

覆盖：
- 套餐能力矩阵（free / personal / enterprise）
- 中间件拦截：页面 302、接口 403（含错误码）
- 免费额度耗尽 LIMIT_EXCEEDED
- 设置页门控 UI（眼馋展示、模糊遮罩、大锁）
- 导航金色 👑 锁标记

实现方式：用 uvicorn（lifespan=off）在临时线程里拉起真实 ASGI 应用，
再用标准库 urllib 发请求，避免引入额外测试依赖。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db


# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立数据目录 + 独立签名密钥 + 已初始化管理员。"""
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    monkeypatch.setattr(machine_mod, "registry_dir", lambda: tmp_path / "hidden")
    monkeypatch.setattr(machine_mod, "machine_code", lambda: "1111-2222-3333-4444")

    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(settings, "license_public_key_pem",
                        key.public_key().public_bytes(
                            serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo))

    from app.repository.db import init_schema
    from app.service import auth_service, license_service

    init_schema()
    auth_service.create_first_admin("admin", "Admin@123456", "管理员")
    auth_service.ack_recovery()
    return {"key": key, "lic": license_service, "auth": auth_service, "db": db}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """保留 302 现场，便于断言服务端跳转目标。"""

    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def request(self, path, method="GET", body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "ignore")

    def get_json(self, path):
        status, _h, text = self.request(path)
        try:
            return status, json.loads(text)
        except Exception:
            return status, {}

    def post_json(self, path, payload=None):
        status, _h, text = self.request(
            path, "POST", json.dumps(payload or {}).encode(),
            {"Content-Type": "application/json"})
        try:
            return status, json.loads(text)
        except Exception:
            return status, {}

    def login(self, username="admin", password="Admin@123456"):
        status, data = self.post_json("/api/auth/login",
                                      {"username": username, "password": password})
        assert status == 200 and data.get("ok"), data
        return self


def _b64e(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _code(env, plan: str) -> str:
    payload = {"machine_code": "1111-2222-3333-4444", "plan": plan,
               "role": "master", "exp": int(time.time()) + 365 * 86400,
               "duration_days": 365, "sub_limit": 3, "order_id": f"ORD-{plan}"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{_b64e(raw)}.{_b64e(env['key'].sign(raw))}"


def _activate(env, plan: str):
    env["lic"].apply_license_code(_code(env, plan))


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def client(env):
    """真实 ASGI 应用：跳过 lifespan（避免 OCR 预热），直接提供 HTTP 服务。"""
    from app.main import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, lifespan="off",
        log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    c = Client(f"http://127.0.0.1:{port}")
    for _ in range(80):
        try:
            if c.request("/health")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:  # pragma: no cover
        server.should_exit = True
        raise RuntimeError("测试服务启动失败")

    c.login()
    yield c
    server.should_exit = True
    thread.join(timeout=3)


def _upload(client):
    boundary = "----matrix"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
        "filename=\"x.txt\"\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return client.request("/api/invoices/upload", "POST", body,
                          {"Content-Type": f"multipart/form-data; boundary={boundary}"})


# ---------------------------------------------------------------- 套餐矩阵
class TestPlanMatrix:
    def test_free_matrix(self, env):
        f = env["lic"].PLANS["free"]
        assert f["monthly_invoice_quota"] == 20
        for key in ("matching", "reimburse", "report", "export", "team",
                    "rules", "audit", "handover", "custom_categories"):
            assert f[key] is False, key

    def test_personal_matrix(self, env):
        p = env["lic"].PLANS["personal"]
        for key in ("matching", "reimburse", "report", "export",
                    "rules", "custom_categories"):
            assert p[key] is True, key
        for key in ("team", "audit", "handover"):
            assert p[key] is False, key
        assert p["monthly_invoice_quota"] is None

    def test_enterprise_matrix(self, env):
        e = env["lic"].PLANS["enterprise"]
        for key in ("matching", "reimburse", "report", "export", "team",
                    "rules", "audit", "handover", "custom_categories"):
            assert e[key] is True, key
        assert e["devices"] == 3

    def test_feature_allowed_follows_plan(self, env):
        lic = env["lic"]
        assert lic.feature_allowed("audit") is False
        _activate(env, "personal")
        assert lic.feature_allowed("matching") is True
        assert lic.feature_allowed("team") is False
        _activate(env, "enterprise")
        assert lic.feature_allowed("audit") is True

    def test_blocked_table_covers_all_paid_features(self):
        from app.service.license_service import PLANS
        from app.web.middleware import FREE_BLOCKED

        features = {f for _prefix, f in FREE_BLOCKED}
        assert features == {"matching", "reimburse", "report", "export",
                            "rules", "audit", "handover"}
        for f in features:
            assert PLANS["free"][f] is False


# ---------------------------------------------------------------- 免费版拦截
class TestFreeBlocking:
    @pytest.mark.parametrize("path,feature", [
        ("/matches", "matching"),
        ("/reimburses", "reimburse"),
        ("/report", "report"),
    ])
    def test_locked_pages_redirect_to_plans(self, client, path, feature):
        status, headers, _ = client.request(path)
        assert status in (302, 307)
        assert headers.get("location") == f"/plans?feature={feature}"

    @pytest.mark.parametrize("path,method,feature", [
        ("/api/match/run", "POST", "matching"),
        ("/api/audit?page=1", "GET", "audit"),
        ("/api/rules", "POST", "rules"),
        ("/api/handover/export", "GET", "handover"),
    ])
    def test_locked_apis_return_403(self, client, path, method, feature):
        status, _h, text = client.request(path, method, b"{}",
                                          {"Content-Type": "application/json"})
        body = json.loads(text)
        assert status == 403
        assert body["code"] == "UPGRADE_REQUIRED"
        assert body["feature"] == feature

    def test_categories_write_blocked_but_read_allowed(self, client):
        status, _h, text = client.request("/api/categories", "POST", b"{}",
                                          {"Content-Type": "application/json"})
        body = json.loads(text)
        assert status == 403
        assert body["code"] == "UPGRADE_REQUIRED"
        assert body["feature"] == "custom_categories"

        # GET 不受限（免费版可查看基础字典）
        status, _h, _ = client.request("/api/categories")
        assert status != 403

    def test_quota_exhausted_returns_limit_exceeded(self, client, env):
        from app.repository.repos import kv_repo
        with env["db"].tx() as conn:
            kv_repo.set(conn, f"quota:{env['lic']._month()}", "20")

        status, _h, text = _upload(client)
        body = json.loads(text)
        assert status == 403
        assert body["code"] == "LIMIT_EXCEEDED"
        assert body["msg"] == "额度用尽"
        assert body["limit"] == 20

    def test_team_page_reachable_for_free(self, client):
        # 允许进入页面（内部用遮罩引导升级），不再整页重定向
        status, _h, html = client.request("/settings/team")
        assert status == 200
        assert "gate-overlay" in html


# ---------------------------------------------------------------- 个人版拦截
class TestPersonalBlocking:
    def test_business_pages_open(self, client, env):
        _activate(env, "personal")
        for path in ("/matches", "/reimburses", "/report"):
            status, _h, _ = client.request(path)
            assert status == 200, path

    def test_team_api_blocked(self, client, env):
        _activate(env, "personal")
        status, body = client.post_json("/api/team/users",
                                        {"username": "u1", "password": "Aa123456!"})
        assert status == 403
        assert body["code"] == "UPGRADE_REQUIRED"
        assert body["feature"] == "team"

    def test_audit_api_blocked(self, client, env):
        _activate(env, "personal")
        status, body = client.get_json("/api/audit?page=1")
        assert status == 403
        assert body["code"] == "UPGRADE_REQUIRED"

    def test_rules_allowed_for_personal(self, client, env):
        _activate(env, "personal")
        status, _h, html = client.request("/settings/rules")
        assert status == 200
        # 只看实际落在 pane 上的类名，避免命中 <style> 里的规则定义
        assert 'class="sg-pane active rule-locked"' not in html


# ---------------------------------------------------------------- 企业版放行
class TestEnterprise:
    def test_team_api_not_license_blocked(self, client, env):
        _activate(env, "enterprise")
        status, body = client.post_json("/api/team/users", {})
        # 业务校验可能 400/422，但绝不能再是「需要升级」
        assert not (status == 403 and body.get("code") == "UPGRADE_REQUIRED")

    def test_audit_api_not_license_blocked(self, client, env):
        _activate(env, "enterprise")
        status, body = client.get_json("/api/audit?page=1")
        assert not (status == 403 and body.get("code") == "UPGRADE_REQUIRED")

    def test_nav_has_no_locks(self, client, env):
        _activate(env, "enterprise")
        _s, _h, html = client.request("/")
        # 只断言导航真实属性，避免命中脚本里的选择器字符串
        for feature in ("reimburse", "matching", "report"):
            assert f'data-locked-feature="{feature}"' not in html


# ---------------------------------------------------------------- 设置页 UI
class TestSettingsGatingUI:
    def test_free_rules_banner_and_disabled(self, client):
        status, _h, html = client.request("/settings/rules")
        assert status == 200
        assert "当前为免费版" in html and "升级个人版即可解锁智能风控拦截" in html
        assert "rule-locked" in html
        assert "当前为免费版，升级解锁风控规则" in html

    def test_free_team_overlay(self, client):
        _s, _h, html = client.request("/settings/team")
        assert "gate-overlay" in html and "解锁企业级协同" in html
        assert "支持多账号权限隔离与防内鬼追溯，请升级企业版" in html

    def test_free_audit_empty_lock(self, client):
        _s, _h, html = client.request("/settings/security")
        assert "系统无审计记录" in html
        assert "数据交接包为企业版专属能力" in html

    def test_free_categories_readonly(self, client):
        _s, _h, html = client.request("/settings/categories")
        assert "cat-locked" in html
        assert "免费版仅支持基础字典，升级后可自定义" in html

    def test_personal_team_overlay_copy(self, client, env):
        _activate(env, "personal")
        _s, _h, html = client.request("/settings/team")
        assert "个人版仅限单人单机使用，如需多账号协同，请升级企业版" in html
        assert "👑 企业版" in html

    def test_personal_audit_blur_gate(self, client, env):
        _activate(env, "personal")
        _s, _h, html = client.request("/settings/security")
        assert "audit-blur" in html
        assert "升级企业版，永久保留合规审计日志，防员工乱改账目" in html


# ---------------------------------------------------------------- 导航锁标记
class TestNavLocks:
    def test_free_nav_gold_lock(self, client):
        _s, _h, html = client.request("/")
        for feature in ("reimburse", "matching", "report"):
            assert f'data-nav-feature="{feature}"' in html
            assert f'data-locked-feature="{feature}"' in html
        assert "👑" in html

    def test_personal_nav_no_locks(self, client, env):
        _activate(env, "personal")
        _s, _h, html = client.request("/")
        assert 'data-locked-feature="matching"' not in html
        assert 'data-locked-feature="reimburse"' not in html
