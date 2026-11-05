"""商业化授权：Ed25519 验签、激活、套餐权益、月度额度、多设备绑定。

安全边界：
- 本机只内置**公钥**，只能验签，无法伪造授权码。
- 私钥仅存在于云端发码服务（cloud/license_server.py），绝不下发客户端。
- 授权码 = Base64(数据部分) + "." + Base64(Ed25519 签名)，签名覆盖数据原文。
"""
import base64
import hashlib
import json
import time
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db
from app.repository.repos import kv_repo

LICENSE_KV_KEY = "license_code"
CLOUD_TIMEOUT = 8.0

# 套餐权益（免费版额度按自然月）
PLANS = {
    "free": {
        "name": "免费版", "price": "0 元/年", "monthly_invoice_quota": 20,
        "matching": False, "export": False, "team": False, "devices": 1,
        "desc": "每月 20 张发票额度，仅基础台账",
    },
    "personal": {
        "name": "个人版", "price": "99 元/年", "monthly_invoice_quota": None,
        "matching": True, "export": True, "team": False, "devices": 1,
        "desc": "不限额度，开放智能匹配与导出",
    },
    "enterprise": {
        "name": "企业版", "price": "499 元/年", "monthly_invoice_quota": None,
        "matching": True, "export": True, "team": True, "devices": 3,
        "desc": "多账号、多设备协同与数据交接包",
    },
}


class LicenseError(Exception):
    """授权码非法/过期/机器码不符。"""


class CloudError(Exception):
    """云端不可达或业务拒绝。"""


class QuotaExceeded(Exception):
    """免费版月度发票额度已用完。"""


# ---------------------------------------------------------------- 编解码
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_code(code: str) -> dict:
    """用内置公钥验签，返回授权数据；任何异常都抛 LicenseError。"""
    try:
        data_part, sig_part = (code or "").strip().split(".", 1)
        raw = _b64d(data_part)
        sig = _b64d(sig_part)
        pub = serialization.load_pem_public_key(settings.license_public_key_pem)
        pub.verify(sig, raw)
        payload = json.loads(raw.decode("utf-8"))
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError) as e:
        raise LicenseError(f"授权码校验失败：{e}") from e
    except Exception as e:
        raise LicenseError(f"授权码格式不正确：{e}") from e
    if not isinstance(payload, dict) or "exp" not in payload:
        raise LicenseError("授权码内容不完整")
    if str(payload.get("machine_code", "")).strip() != machine_mod.machine_code():
        raise LicenseError("授权码与本机机器码不匹配")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise LicenseError("授权码已过期")
    return payload


def _decode_unchecked(code: str) -> Optional[dict]:
    """仅用于展示历史授权信息（签名校验失败返回 None）。"""
    try:
        data_part, _sig = (code or "").strip().split(".", 1)
        return json.loads(_b64d(data_part).decode("utf-8"))
    except Exception:
        return None


def _load_code() -> str:
    with db.read() as conn:
        return kv_repo.get(conn, LICENSE_KV_KEY, "") or ""


def _save_code(code: str) -> None:
    with db.tx() as conn:
        kv_repo.set(conn, LICENSE_KV_KEY, code)


def clear_license() -> None:
    with db.tx() as conn:
        kv_repo.delete(conn, LICENSE_KV_KEY)


# ---------------------------------------------------------------- 云端通信
def _post(path: str, body: dict) -> dict:
    url = settings.cloud_base_url.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data,
                             headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=settings.cloud_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
            raise CloudError(payload.get("message") or payload.get("detail")
                            or f"授权服务返回 {e.code}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CloudError(f"授权服务返回异常（HTTP {e.code}）")
    except (urlerror.URLError, TimeoutError, OSError) as e:
        raise CloudError(f"无法连接授权服务器（{settings.cloud_base_url}），请检查网络后重试：{e}")


def activate(order_id: str) -> dict:
    """本地中转：把机器码 + 订单号发给云端换取授权码，验签后落地。"""
    order_id = (order_id or "").strip()
    if not order_id:
        raise CloudError("请输入订单号")
    resp = _post("/api/get_license", {
        "order_id": order_id,
        "machine_code": machine_mod.machine_code(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "订单校验失败")
    code = resp.get("license_code") or ""
    payload = verify_code(code)          # 机器码/过期校验都在这里
    _save_code(code)
    return payload


def bind_sub_device(invite_code: str) -> dict:
    """子设备绑定：用主设备的企业邀请码 + 本机机器码向云端换子授权。"""
    invite_code = (invite_code or "").strip()
    if not invite_code:
        raise CloudError("请输入企业邀请码")
    resp = _post("/api/sub_bind", {
        "invite_code": invite_code,
        "machine_code": machine_mod.machine_code(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "绑定失败")
    code = resp.get("license_code") or ""
    payload = verify_code(code)
    _save_code(code)
    return payload


def unlock_code_verify(code: str) -> dict:
    """超管解锁码：客服持私钥签发，绑定机器码 + 短期有效期。"""
    payload = verify_code(code)
    if payload.get("type") != "unlock":
        raise LicenseError("该码不是超管解锁码")
    return payload


def sync_license() -> dict:
    """同步黑名单/换机状态：云端判定拉黑则本地立即降级为免费版。"""
    resp = _post("/api/status", {"machine_code": machine_mod.machine_code()})
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "同步失败")
    if resp.get("blacklisted"):
        clear_license()
    return status()


# ---------------------------------------------------------------- 本机授权状态
def current() -> Optional[dict]:
    """当前生效授权（验签通过且未过期），否则 None。"""
    code = _load_code()
    if not code:
        return None
    try:
        return verify_code(code)
    except LicenseError:
        return None


def _effective() -> tuple:
    """(payload, error, expired)：签名合法但过期时不视为生效授权。"""
    code = _load_code()
    if not code:
        return None, "", False
    try:
        payload = verify_code(code)
    except LicenseError as e:
        raw = _decode_unchecked(code)
        exp = int((raw or {}).get("exp", 0))
        expired = bool(raw) and exp < int(time.time())
        # 过期属于正常降级，不当作"授权码非法"提示
        return raw, "" if expired else str(e), expired
    return payload, "", int(payload.get("exp", 0)) < int(time.time())


def dev_mode() -> bool:
    """开发者调试模式：跳过授权与企业版判定，仅本地开发/自测使用。

    由 app.config 在导入时根据 FAPIAO_DEV_MODE 环境变量或 config.json 计算，
    打包后的 exe 只认环境变量，避免把调试开关带到用户机器上。
    """
    return bool(settings.dev_mode)


def plan_key() -> str:
    if dev_mode():
        # 调试模式等价于企业版：权益全开、额度不限、不写额度计数
        return "enterprise"
    payload, error, expired = _effective()
    if not payload or error or expired:
        return "free"
    plan = payload.get("plan")
    return plan if plan in PLANS else "free"


def features() -> dict:
    return PLANS[plan_key()]


def status() -> dict:
    payload, error, expired = _effective()
    dev = dev_mode()
    if dev:
        # 调试模式：始终展示企业版且永久有效，前端据此显示「开发者模式」徽标
        active, required, expired, error = True, 0, False, ""
    else:
        active = bool(payload) and not expired and not error
        # 只要数据部分可解析就给出到期时间：过期页同样需要展示「已于 X 过期」
        required = int((payload or {}).get("exp", 0))
    current_plan = plan_key()
    return {
        "activated": active,
        "plan": current_plan,
        "plan_name": PLANS[current_plan]["name"],
        "features": PLANS[current_plan],
        "exp": required,
        "exp_date": time.strftime("%Y-%m-%d", time.localtime(required)) if required else "",
        "expired": expired,
        "error": error,
        "dev_mode": dev,
        "machine_code": machine_mod.machine_code(),
        "role": (payload or {}).get("role", "master") if active else "master",
        "sub_limit": int((payload or {}).get("sub_limit", 0) or 0) if active else 0,
        "order_id": (payload or {}).get("order_id", "") if active else "",
        "quota": quota_status(),
        "plans": [
            {"key": k, **v, "current": k == current_plan}
            for k, v in PLANS.items()
        ],
        "payment_qr_url": settings.payment_qr_url,
    }


def feature_allowed(feature: str) -> bool:
    return bool(features().get(feature, False))


def invite_code() -> str:
    """主设备展示给子设备的企业邀请码（即本机授权码原文）。"""
    payload = current()
    if not payload or payload.get("plan") != "enterprise":
        raise LicenseError("仅企业版主设备可生成企业邀请码")
    if payload.get("role") == "sub":
        raise LicenseError("子设备不能再次生成邀请码")
    return _load_code()


# ---------------------------------------------------------------- 免费版防刷额度
def _month(prefix: str = "%Y-%m") -> str:
    return time.strftime(prefix)


def _guard_count(month: str) -> int:
    guard = machine_mod.load_guard()
    key = machine_mod.month_key(machine_mod.machine_code(), month)
    try:
        return int((guard.get("quota") or {}).get(key, 0) or 0)
    except Exception:
        return 0


def quota_status() -> dict:
    limit = features().get("monthly_invoice_quota")
    month = _month()
    with db.read() as conn:
        db_used = int(kv_repo.get(conn, f"quota:{month}", "0") or 0)
    used = max(db_used, _guard_count(month))
    if limit is None:
        return {"limit": None, "used": used, "remaining": None,
                "month": month, "unlimited": True}
    return {"limit": limit, "used": used, "remaining": max(0, limit - used),
            "month": month, "unlimited": False}


def consume_invoice_quota(count: int = 1) -> None:
    """导入发票前预留额度：同一事务内重查 + 扣减，避免并发请求超额。"""
    if count <= 0:
        return
    limit = features().get("monthly_invoice_quota")
    if limit is None:
        return
    month = _month()
    guard = machine_mod.load_guard()
    guard_key = machine_mod.month_key(machine_mod.machine_code(), month)
    guard_used = int((guard.get("quota") or {}).get(guard_key, 0) or 0)
    with db.tx() as conn:
        db_used = int(kv_repo.get(conn, f"quota:{month}", "0") or 0)
        used = max(db_used, guard_used)
        if used + count > int(limit):
            raise QuotaExceeded(
                f"免费版每月额度 {limit} 张，本月已用 {used} 张。"
                "请升级个人版/企业版后继续导入")
        kv_repo.set(conn, f"quota:{month}", str(used + count))
    guard.setdefault("quota", {})[guard_key] = used + count
    machine_mod.save_guard(guard)


def refund_invoice_quota(count: int = 1) -> None:
    """导入失败/未实际入库时退回预留额度（数据库计数与隐藏登记簿同步扣减）。"""
    if count <= 0:
        return
    if features().get("monthly_invoice_quota") is None:
        return
    month = _month()
    with db.tx() as conn:
        db_used = int(kv_repo.get(conn, f"quota:{month}", "0") or 0)
        kv_repo.set(conn, f"quota:{month}", str(max(0, db_used - count)))
    guard = machine_mod.load_guard()
    key = machine_mod.month_key(machine_mod.machine_code(), month)
    guard.setdefault("quota", {})[key] = max(
        0, int((guard.get("quota") or {}).get(key, 0) or 0) - count)
    machine_mod.save_guard(guard)


def initial_password() -> str:
    return settings.initial_password


def plan_public() -> dict:
    """套餐卡片数据（无授权时也能展示）。"""
    return {
        "plans": [{"key": k, **v} for k, v in PLANS.items()],
        "payment_qr_url": settings.payment_qr_url,
        "initial_password": settings.initial_password,
    }
