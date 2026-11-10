"""商业化授权 / 账号体系单元测试（不接触真实云端与真实机器码）。"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    init_schema()
    from app.service import auth_service, license_service
    return {"key": key, "auth": auth_service, "lic": license_service,
            "db": db, "tmp": tmp_path}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def sign(env, payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{_b64e(raw)}.{_b64e(env['key'].sign(raw))}"


def _license(env, **kw) -> str:
    payload = {"machine_code": "1111-2222-3333-4444", "plan": "enterprise",
               "role": "master", "exp": int(time.time()) + 365 * 86400,
               "sub_limit": 3, "order_id": "ORD-1"}
    payload.update(kw)
    return sign(env, payload)


# ---------------------------------------------------------------- 签名校验
class TestLicenseVerify:
    def test_valid_code(self, env):
        payload = env["lic"].verify_code(_license(env))
        assert payload["plan"] == "enterprise"

    def test_machine_mismatch(self, env):
        with pytest.raises(env["lic"].LicenseError):
            env["lic"].verify_code(_license(env, machine_code="9999-8888-7777-6666"))

    def test_expired(self, env):
        with pytest.raises(env["lic"].LicenseError):
            env["lic"].verify_code(_license(env, exp=int(time.time()) - 10))

    def test_tampered_payload(self, env):
        code = _license(env)
        data, sig = code.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        payload["plan"] = "free"
        payload["exp"] = int(time.time()) + 10 ** 8
        bad = _b64e(json.dumps(payload, separators=(",", ":")).encode()) + "." + sig
        with pytest.raises(env["lic"].LicenseError):
            env["lic"].verify_code(bad)

    def test_unlock_code_type(self, env):
        code = _license(env, type="unlock", exp=int(time.time()) + 3600)
        assert env["lic"].verify_code(code)["type"] == "unlock"


# ---------------------------------------------------------------- 激活 / 套餐
class TestActivation:
    def test_activate_and_status(self, env, monkeypatch):
        monkeypatch.setattr(env["lic"], "_post", lambda path, body: {
            "ok": True, "license_code": _license(env), "plan": "enterprise"})
        env["lic"].activate("ORD-1")
        st = env["lic"].status()
        assert st["activated"] and st["plan"] == "enterprise"
        assert st["features"]["team"] is True
        assert env["lic"].feature_allowed("matching") is True

    def test_expired_license_falls_back_to_free(self, env):
        with env["db"].tx() as conn:
            env["lic"].kv_repo.set(conn, env["lic"].LICENSE_KV_KEY,
                                   _license(env, exp=int(time.time()) - 10))
        st = env["lic"].status()
        assert st["plan"] == "free" and st["expired"] and not st["activated"]

    def test_cloud_error(self, env, monkeypatch):
        def boom(path, body):
            raise env["lic"].CloudError("无法连接授权服务器")
        monkeypatch.setattr(env["lic"], "_post", boom)
        with pytest.raises(env["lic"].CloudError):
            env["lic"].activate("ORD-1")

    def test_free_plan_blocks_matching(self, env):
        assert env["lic"].feature_allowed("matching") is False


# ---------------------------------------------------------------- 免费版额度
class TestQuota:
    def test_limit_and_guard_registry(self, env):
        lic = env["lic"]
        for _ in range(20):
            lic.consume_invoice_quota(1)
        assert lic.quota_status()["used"] == 20
        with pytest.raises(lic.QuotaExceeded):
            lic.consume_invoice_quota(1)

    def test_guard_survives_db_reset(self, env):
        lic = env["lic"]
        for _ in range(20):
            lic.consume_invoice_quota(1)
        with env["db"].tx() as conn:
            conn.execute("DELETE FROM kv WHERE k LIKE 'quota:%'")
        # 数据库额度被清空，但隐藏登记簿仍计数
        assert lic.quota_status()["used"] == 20

    def test_paid_plan_unlimited(self, env):
        with env["db"].tx() as conn:
            lic = env["lic"]
            lic.kv_repo.set(conn, lic.LICENSE_KV_KEY, _license(env, plan="personal"))
        assert lic.quota_status()["unlimited"] is True
        assert lic.consume_invoice_quota(999) is None


# ---------------------------------------------------------------- 账号 / 会话
class TestAuth:
    def test_password_hash(self, env):
        a = env["auth"]
        h = a.hash_password("Admin@123")
        assert a.verify_password("Admin@123", h)
        assert not a.verify_password("wrong", h)

    def test_first_admin_and_recovery(self, env):
        a = env["auth"]
        info = a.create_first_admin("admin", "Admin@123", "管理员")
        assert len(info["recovery_code"]) == 19  # XXXX-XXXX-XXXX-XXXX
        assert a.verify_recovery_code(info["recovery_code"],
                                     a.get_by_username("admin")["recovery_hash"])
        assert a.recovery_pending() is True
        a.ack_recovery()
        assert a.recovery_pending() is False

    def test_login_and_session(self, env):
        a = env["auth"]
        a.create_first_admin("admin", "Admin@123")
        user = a.authenticate("admin", "Admin@123")
        token = a.make_session_token(user["id"], 30)
        assert a.parse_session_token(token) == user["id"]
        assert a.parse_session_token("bad.token") is None

    def test_duplicate_admin_forbidden(self, env):
        a = env["auth"]
        a.create_first_admin("admin", "Admin@123")
        with pytest.raises(a.AuthError):
            a.create_first_admin("admin2", "Admin@123")

    def test_create_user_roles(self, env):
        a = env["auth"]
        a.create_first_admin("admin", "Admin@123")
        uid = a.create_user("zhangsan", "12345678", "employee", "张三")
        assert a.get_user(uid)["role"] == "employee"
        with pytest.raises(a.AuthError):
            a.create_user("zhangsan", "12345678", "employee")

    def test_reset_by_recovery(self, env):
        a = env["auth"]
        info = a.create_first_admin("admin", "Admin@123")
        a.reset_by_recovery("admin", info["recovery_code"], "NewPass@123")
        assert a.authenticate("admin", "NewPass@123")["username"] == "admin"
        with pytest.raises(a.AuthError):
            a.authenticate("admin", "Admin@123")


# ---------------------------------------------------------------- 修复回归
def test_free_blocked_uses_api_prefixes():
    """免费版拦截必须以 /api 开头，否则线上导出接口不会被拦。"""
    from app.web import middleware
    blocked = dict((p, f) for p, f in middleware.FREE_BLOCKED)
    assert blocked.get("/api/invoices/export") == "export"
    assert blocked.get("/api/report/export") == "export"
    assert blocked.get("/api/match") == "matching"


def test_expired_status_keeps_exp_date(env):
    """过期授权仍要展示到期日（避免「授权已于 过期」）。"""
    with env["db"].tx() as conn:
        env["lic"].kv_repo.set(conn, env["lic"].LICENSE_KV_KEY,
                               _license(env, exp=int(time.time()) - 10))
    st = env["lic"].status()
    assert st["expired"] is True
    assert st["exp_date"] == time.strftime("%Y-%m-%d",
                                           time.localtime(int(time.time()) - 10))


def test_dev_mode_grants_enterprise_and_unlimited_quota(env, monkeypatch):
    """开发者模式：企业版权益全开、额度不限、导入不写额度计数。"""
    monkeypatch.setattr(settings, "dev_mode", True)
    lic = env["lic"]
    st = lic.status()
    assert st["dev_mode"] is True and st["activated"] is True
    assert st["plan"] == "enterprise" and st["expired"] is False
    assert lic.feature_allowed("team") and lic.feature_allowed("export")
    assert lic.quota_status()["unlimited"] is True
    lic.consume_invoice_quota(50)                      # 不扣额度
    assert lic.quota_status()["used"] == 0


def test_dev_mode_off_keeps_free_limits(env, monkeypatch):
    """关闭开发者模式后恢复免费版额度拦截。"""
    monkeypatch.setattr(settings, "dev_mode", False)
    lic = env["lic"]
    assert lic.status()["dev_mode"] is False
    assert lic.status()["plan"] == "free"
    assert lic.feature_allowed("export") is False
    with pytest.raises(lic.QuotaExceeded):
        lic.consume_invoice_quota(21)


def test_invoice_status_counts_owner_scope(env):
    """员工统计只计算自己的发票。"""
    from app.service import invoice_service
    with env["db"].tx() as conn:
        for no, owner in (("A1", 1), ("A2", 1), ("B1", 2)):
            conn.execute(
                "INSERT INTO invoice (file_hash, file_type, parse_engine, invoice_no,"
                " status, owner_user_id) VALUES (?,?,?,?,?,?)",
                (no, "xml", "manual", no, "NORMAL", owner))
    assert invoice_service.invoice_status_counts()["NORMAL"] == 3
    assert invoice_service.invoice_status_counts(owner_user_id=1)["NORMAL"] == 2
    assert invoice_service.invoice_status_counts(owner_user_id=2)["NORMAL"] == 1


def test_session_invalid_after_password_reset(env):
    """重置密码后旧会话 token 立即失效（pv 与密码哈希绑定）。"""
    from starlette.requests import Request

    a = env["auth"]
    a.create_first_admin("admin", "Admin@123")
    user = a.authenticate("admin", "Admin@123")
    token = a.make_session_token(user["id"], 30, user["password_hash"])

    def fake_request(tok: str) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/",
                        "headers": [(b"cookie", f"{a.COOKIE_NAME}={tok}".encode())]})

    assert a.user_from_request(fake_request(token))["id"] == user["id"]
    a.reset_password(user["id"], "NewPass@123")
    assert a.get_user(user["id"])["must_change_pwd"] == 1
    assert a.user_from_request(fake_request(token)) is None


def test_change_password_clears_flag(env):
    """改密成功后清除强制改密标记，并可正常登录。"""
    a = env["auth"]
    a.create_first_admin("admin", "Admin@123")
    user = a.authenticate("admin", "Admin@123")
    a.reset_password(user["id"], "NewPass@123")
    a.change_password(user["id"], "NewPass@123", "NewPass@456")
    assert a.get_user(user["id"])["must_change_pwd"] == 0
    assert a.authenticate("admin", "NewPass@456")["username"] == "admin"
    with pytest.raises(a.AuthError):
        a.change_password(user["id"], "wrong-old", "NewPass@999")


def test_similar_endpoint_checks_ownership(monkeypatch):
    """similar 接口必须复用员工归属校验（越权返回 403）。"""
    from app.web import api as api_mod

    monkeypatch.setattr(api_mod, "_invoice_forbidden",
                        lambda request, invoice_id: True)
    resp = api_mod.similar_unmatched_invoices(
        type("R", (), {"state": type("S", (), {"user": {"id": 1}})()})(), 2)
    assert resp.status_code == 403 and resp.body


def test_cloud_get_license_never_extends_expiry(tmp_path, monkeypatch):
    """同一订单重复换码不能续期（首次激活时间固定）。"""
    import importlib

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setenv("CLOUD_PRIVATE_KEY_PEM", pem)
    monkeypatch.setenv("CLOUD_DATA_DIR", str(tmp_path / "cloud"))
    monkeypatch.setenv("XUNHUPAY_APPSECRET", "test-secret")
    import cloud.license_server as srv
    importlib.reload(srv)

    data_dir = tmp_path / "cloud"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "orders.json").write_text(json.dumps({
        "orders": {"ORD-1": {"plan": "personal", "days": 365}}}), encoding="utf-8")

    def _req():
        return type("R", (), {"client": type("C", (), {"host": "1.2.3.4"})()})()

    body = {"order_id": "ORD-1", "machine_code": "M-1"}
    first = srv.get_license(srv.LicenseReq(**body), _req())
    second = srv.get_license(srv.LicenseReq(**body), _req())
    assert first["ok"] and second["ok"]
    assert first["exp"] == second["exp"]


# ---------------------------------------------------------------- 云端支付闭环
def _cloud_srv(tmp_path, monkeypatch, orders: dict):
    """用临时私钥与临时数据目录重新加载云端服务模块。"""
    import importlib

    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("CLOUD_PRIVATE_KEY_PEM",
                       key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode())
    monkeypatch.setenv("CLOUD_DATA_DIR", str(tmp_path / "cloud"))
    monkeypatch.setenv("XUNHUPAY_APPSECRET", "test-secret")
    import cloud.license_server as srv
    importlib.reload(srv)
    d = tmp_path / "cloud"
    d.mkdir(parents=True, exist_ok=True)
    (d / "orders.json").write_text(json.dumps({"orders": orders}, ensure_ascii=False),
                                   encoding="utf-8")
    return srv


def _fake_request(host: str = "1.2.3.4"):
    return type("R", (), {"client": type("C", (), {"host": host})()})()


def test_cloud_recover_by_order_suffix(tmp_path, monkeypatch):
    """付款后关掉页面也能用订单号后 6 位找回授权；换机则拒绝。"""
    srv = _cloud_srv(tmp_path, monkeypatch, {
        "FP1762A1B2C3": {"plan": "personal", "days": 365, "status": "paid",
                         "machine_code": "M-1", "license_key": "CODE-1", "amount": 1},
    })
    r = srv.payment_recover(
        srv.PayRecoverReq(order_suffix="a1b2c3", machine_code="M-1"), _fake_request())
    assert r["ok"] and r["license_key"] == "CODE-1"

    with pytest.raises(Exception) as exc:
        srv.payment_recover(
            srv.PayRecoverReq(order_suffix="A1B2C3", machine_code="M-2"), _fake_request())
    assert "其他设备" in str(getattr(exc.value, "detail", exc.value))


def test_cloud_reconcile_issues_for_stale_paid_order(tmp_path, monkeypatch):
    """对账兜底：pending 超时订单主动查单为已支付后静默补发授权码。"""
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1200))
    srv = _cloud_srv(tmp_path, monkeypatch, {
        "FP1": {"plan": "enterprise", "days": 365, "status": "pending",
                "machine_code": "M-1", "amount": 49900, "created_at": old},
    })
    monkeypatch.setattr(srv, "_query_gateway", lambda oid: {
        "status": "OD", "total_fee": "499.00", "transaction_id": "T9"})
    assert srv._reconcile_pending() == 1
    orders = json.loads((tmp_path / "cloud" / "orders.json").read_text(encoding="utf-8"))["orders"]
    assert orders["FP1"]["status"] == "paid" and orders["FP1"]["license_key"]


def test_cloud_reconcile_skips_fresh_and_unpaid(tmp_path, monkeypatch):
    """未超时的 pending 与仍未支付的订单都不应被补发。"""
    now_s = time.strftime("%Y-%m-%d %H:%M:%S")
    srv = _cloud_srv(tmp_path, monkeypatch, {
        "FP-NEW": {"plan": "personal", "status": "pending", "amount": 1,
                   "created_at": now_s},
        "FP-OLD": {"plan": "personal", "status": "pending", "amount": 1,
                   "created_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(time.time() - 1200))},
    })
    monkeypatch.setattr(srv, "_query_gateway",
                        lambda oid: {"status": "WP", "total_fee": "0.01"})
    assert srv._reconcile_pending() == 0
    orders = json.loads((tmp_path / "cloud" / "orders.json").read_text(encoding="utf-8"))["orders"]
    assert all(o["status"] == "pending" for o in orders.values())


def test_cloud_create_reuses_pending_qr(tmp_path, monkeypatch):
    """同一机器同套餐重复下单：复用旧二维码，不重复请求网关（防刷单）。"""
    srv = _cloud_srv(tmp_path, monkeypatch, {})
    calls = []

    def fake_post(url, data):
        calls.append(url)
        return {"errcode": 0, "url_qrcode": "https://qr/1.png", "url": "https://pay/1"}

    monkeypatch.setattr(srv, "_http_post_form", fake_post)
    first = srv.payment_create(srv.PayCreateReq(plan_type="personal", machine_code="M-1"))
    second = srv.payment_create(srv.PayCreateReq(plan_type="personal", machine_code="M-1"))
    assert first["trade_order_id"] == second["trade_order_id"]
    assert second["reused"] is True and len(calls) == 1
    assert first["amount_fen"] == 1 and first["amount"] == 0.01

    orders = json.loads((tmp_path / "cloud" / "orders.json").read_text(encoding="utf-8"))["orders"]
    order = orders[first["trade_order_id"]]
    assert order["status"] == "pending" and order["amount"] == 1
    assert order["url_qrcode"] == "https://qr/1.png"


def test_cloud_create_rolls_back_placeholder_on_gateway_error(tmp_path, monkeypatch):
    """网关下单失败必须清掉占位单，不能留下永远 pending 的僵尸订单。"""
    srv = _cloud_srv(tmp_path, monkeypatch, {})

    def boom(url, data):
        raise RuntimeError("网关不可用")

    monkeypatch.setattr(srv, "_http_post_form", boom)
    with pytest.raises(Exception):
        srv.payment_create(srv.PayCreateReq(plan_type="enterprise", machine_code="M-2"))
    orders = json.loads((tmp_path / "cloud" / "orders.json").read_text(encoding="utf-8"))["orders"]
    assert orders == {}


def test_cloud_create_requires_appsecret(tmp_path, monkeypatch):
    """未配置商户密钥时必须显式报错，而不是用空密钥静默签名。"""
    srv = _cloud_srv(tmp_path, monkeypatch, {})
    monkeypatch.setattr(srv, "XUNHUPAY_APPSECRET", "")
    monkeypatch.setattr(srv, "_http_post_form",
                        lambda url, data: {"errcode": 0, "url_qrcode": "https://qr/x"})
    with pytest.raises(Exception) as exc:
        srv.payment_create(srv.PayCreateReq(plan_type="personal", machine_code="M-3"))
    assert "XUNHUPAY_APPSECRET" in str(exc.value)


def test_cloud_query_uses_out_trade_order(tmp_path, monkeypatch):
    """查单必须用 out_trade_order 字段，否则虎皮椒返回 Invalid open_order_id。"""
    srv = _cloud_srv(tmp_path, monkeypatch, {})
    seen = {}

    def fake_post(url, data):
        seen.update(data)
        return {"errcode": 0, "data": {"status": "WP"}}

    monkeypatch.setattr(srv, "_http_post_form", fake_post)
    info = srv._query_gateway("FP20260101ABCD")
    assert seen["out_trade_order"] == "FP20260101ABCD"
    assert "trade_order_id" not in seen
    assert info["status"] == "WP"


# ---------------------------------------------------------------- 本地支付落地
def test_payment_check_writes_license_before_success(env, monkeypatch):
    """查单成功必须先落库，再返回 paid（防丢码）。"""
    lic = env["lic"]
    monkeypatch.setattr(lic, "_post", lambda path, body: {
        "ok": True, "status": "paid", "license_key": _license(env),
        "trade_order_id": "T1"})
    r = lic.payment_check("T1")
    assert r["status"] == "paid"
    assert lic.current()["plan"] == "enterprise"


def test_recover_license_saves_to_db(env, monkeypatch):
    """自助找回同样要验签并写入数据库后才算成功。"""
    lic = env["lic"]
    monkeypatch.setattr(lic, "_post", lambda path, body: {
        "ok": True, "license_key": _license(env), "trade_order_id": "FP1",
        "plan": "enterprise"})
    r = lic.recover_license("A1B2C3")
    assert r["status"] == "paid" and r["trade_order_id"] == "FP1"
    assert lic.current()["plan"] == "enterprise"


def test_recover_license_rejects_short_suffix(env):
    with pytest.raises(env["lic"].CloudError):
        env["lic"].recover_license("ABC")
