"""功能矩阵测试公共支撑（下划线前缀，pytest 不会收集为测试模块）。

职责：
1. 把 settings 的数据目录/数据库/上传目录/日志目录全部指向临时目录，
   绝不触碰正式库 data/fapiao.db；
2. 重置 auth/license 的模块级缓存，避免上一个用例的库影响下一个用例；
3. 提供 TestClient 登录后的会话与常用造数函数。
"""
import base64
import json
import time
from datetime import date, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.infra import machine as machine_mod

ADMIN_USER = "admin"
ADMIN_PWD = "Admin@123456"


def configure(monkeypatch, tmp_path):
    """隔离全部落盘路径，返回临时目录。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    # 隐藏登记簿（免费额度/时间锚点）也放临时目录
    monkeypatch.setattr(machine_mod, "registry_dir", lambda: tmp_path / "hidden")
    monkeypatch.setattr(machine_mod, "machine_code", lambda: "TEST-0000-0000-0001")
    # 核心硬件指纹依赖 wmic/PowerShell，测试里固定为空串，避免每次几秒的硬件探测
    monkeypatch.setattr(machine_mod, "core_fp", lambda: "")
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def reset_caches():
    """清空跨用例的单例缓存（auth 是否有用户 / 会话密钥 / 登录失败计数 / 授权缓存）。"""
    from app.service import auth_service, license_service
    auth_service._HAS_USERS_CACHE = False
    auth_service._SESSION_SECRET_CACHE = ""
    with auth_service._LOGIN_FAILS_LOCK:
        auth_service._LOGIN_FAILS.clear()
    license_service._READONLY_CACHE.update({"at": 0.0, "value": False})
    license_service._CORE_OK.update({"sig": "", "at": 0.0})


def init_and_seed_admin():
    """建表 + 首个管理员 + 确认已保存恢复码。"""
    from app.repository.db import init_schema
    from app.service import auth_service
    init_schema()
    info = auth_service.create_first_admin(ADMIN_USER, ADMIN_PWD, "管理员")
    auth_service.ack_recovery()
    return info


def make_license_key(plan="enterprise", lifetime=True, days=365, sub_limit=2):
    """生成一对 Ed25519 密钥并注入客户端内置公钥，返回签发函数。"""
    key = Ed25519PrivateKey.generate()
    settings.license_public_key_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def issue(exp_days=days):
        now = int(time.time())
        payload = {
            "v": 2,
            "machine_code": machine_mod.machine_code(),
            "core_fp": machine_mod.core_fp(),
            "plan": plan,
            "role": "master",
            "lifetime": bool(lifetime),
            "exp": 4102444800 if lifetime else now + exp_days * 86400,
            "maint_exp": now + exp_days * 86400,
            "sub_limit": sub_limit,
            "order_id": f"TEST-{int(time.time())}",
            "duration_days": exp_days,
            "issued_at": now,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
        return f"{b64(raw)}.{b64(key.sign(raw))}"

    return issue


def login(client, username=ADMIN_USER, password=ADMIN_PWD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def today_str(days_ago=30):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _unwrap(resp):
    """接口统一 {ok, data} 信封：取 data；无 data 时返回原结构。"""
    try:
        body = resp.json()
    except Exception:
        return {}
    return body.get("data", body) if isinstance(body, dict) else body


def manual_invoice(client, no="INV0001", total="100.00", tax="0.00", when=None,
                   seller="测试供应商", buyer="北京示例科技有限公司", summary=""):
    """通过接口人工录入一张发票，返回 data 体（ok/invoice_id/status/message）。"""
    when = when or today_str()
    return _unwrap(client.post("/api/invoices/manual", data={
        "invoice_no": no, "invoice_code": "", "invoice_date": when,
        "buyer_name": buyer, "buyer_tax_no": "91110108MA01AAAA1A",
        "seller_name": seller, "seller_tax_no": "91310000MA01BBBB2B",
        "amount": total, "tax_amount": tax, "total_amount": total,
        "tax_rate": "0", "item_summary": summary,
    }))


def manual_reimburse(client, no="BX-TEST-0001", amount="100.00",
                     when=None, submitter="张三"):
    when = when or today_str()
    return _unwrap(client.post("/api/reimburses/manual", data={
        "reimburse_no": no, "submitter": submitter, "reimburse_date": when,
        "period_start": when, "period_end": when, "expense_type": "差旅费",
        "amount": amount, "invoice_no": "", "invoice_date": "", "remark": "",
    }))


def invoice_id_of(client, invoice_no):
    """按发票号从列表接口反查 id（列表页 HTML 解析不可靠，改用导出/预览兜底）。"""
    from app.repository import db
    with db.read() as conn:
        row = conn.execute(
            "SELECT id FROM invoice WHERE invoice_no=? AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (invoice_no,)).fetchone()
    return row["id"] if row else None


def reimburse_id_of(reimburse_no):
    from app.repository import db
    with db.read() as conn:
        row = conn.execute(
            "SELECT id FROM reimburse WHERE reimburse_no=? AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (reimburse_no,)).fetchone()
    return row["id"] if row else None


def count_rows(table, where="1=1", params=()):
    from app.repository import db
    with db.read() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params).fetchone()["c"]


def seed_raw_invoice(no, status="ABNORMAL", total="100.00", tax="13.00",
                     amount=None, when=None, summary="", seller="测试供应商"):
    """直接落一条指定状态的发票（ABNORMAL/DUPLICATE 无法通过人工录入接口产生，
    只能由解析链路或状态机产生；这里按仓储层契约直接插入，用于验证页面与修复流程）。"""
    import uuid as _uuid
    from app.repository import db
    from app.repository.repos import invoice_repo
    when = when or today_str()
    amount = amount if amount is not None else total
    with db.tx() as conn:
        return invoice_repo.insert(conn, {
            "file_hash": f"raw-{_uuid.uuid4().hex}",
            "file_type": "xml", "invoice_code": "", "invoice_no": no,
            "invoice_date": when,
            "buyer_name": "北京示例科技有限公司",
            "buyer_tax_no": "91110108MA01AAAA1A",
            "seller_name": seller, "seller_tax_no": "91310000MA01BBBB2B",
            "amount": amount, "tax_amount": tax, "total_amount": total,
            "item_summary": summary, "raw_json": "{}",
            "parse_engine": "xml", "parse_confidence": 1.0, "status": status,
        })
