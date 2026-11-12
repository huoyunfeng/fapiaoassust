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
PENDING_ORDER_KV = "pay_pending_order"
# 本地处理中订单有效期：超过后自动清除，避免 /plans 顶部横幅长期挂着过期订单
PENDING_ORDER_TTL = 30 * 60
CLOUD_TIMEOUT = 8.0

# 套餐等级（防降级比较用）：free < personal < enterprise
PLAN_RANK = {"free": 0, "personal": 1, "enterprise": 2}
# 离线宽限期：距最后一次成功联网校验超过该天数即降级免费版
OFFLINE_GRACE_DAYS = 15
# 防时间篡改：允许系统时间最多回拨 10 分钟（时区/校时误差），超过视为篡改
CLOCK_ROLLBACK_TOLERANCE = 600

# 套餐权益（免费版额度按自然月）
PLANS = {
    "free": {
        "name": "免费版", "price": "0 元/年", "monthly_invoice_quota": 20,
        "matching": False, "reimburse": False, "report": False,
        "export": False, "team": False, "devices": 1, "seat_limit": 1,
        "rules": False, "audit": False, "handover": False, "custom_categories": False,
        "desc": "每月 20 张发票额度，仅基础台账",
    },
    "personal": {
        "name": "个人版", "price": "99 元/年", "monthly_invoice_quota": None,
        "matching": True, "reimburse": True, "report": True,
        "export": True, "team": False, "devices": 1, "seat_limit": 1,
        "rules": True, "audit": False, "handover": False, "custom_categories": True,
        "desc": "不限额度，开放智能匹配、报销单、差异报告与基础风控",
    },
    "enterprise": {
        "name": "企业版", "price": "499 元/年", "monthly_invoice_quota": None,
        "matching": True, "reimburse": True, "report": True,
        "export": True, "team": True, "devices": 3, "seat_limit": 3,
        "rules": True, "audit": True, "handover": True, "custom_categories": True,
        "desc": "多账号、多设备协同、审计日志与数据交接",
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
        kv_repo.delete(conn, _LICENSE_META_KV)
    _READONLY_CACHE.update({"at": 0.0, "value": False})


# ---------------------------------------------------------------- 防时间篡改 + 离线宽限
def _guard_touch_runtime() -> None:
    """每次状态判定时刷新「最后正常运行时间」（取历史最大值，单调不减）。"""
    guard = machine_mod.load_guard()
    now = int(time.time())
    last = int(guard.get("last_run_at", 0) or 0)
    if now > last:
        guard["last_run_at"] = now
        machine_mod.save_guard(guard)


def clock_tampered() -> bool:
    """当前系统时间比「最后正常运行时间」早超过容忍阈值 → 判定时间被回拨。"""
    guard = machine_mod.load_guard()
    last = int(guard.get("last_run_at", 0) or 0)
    return bool(last) and int(time.time()) < last - CLOCK_ROLLBACK_TOLERANCE


def _mark_cloud_sync(server_time: int = 0) -> None:
    """记录最后一次成功联网校验时间；云端返回了服务器时间则同时校准本机时钟基准。"""
    now = int(time.time())
    with db.tx() as conn:
        kv_repo.set(conn, "last_cloud_sync_at", str(now))
    if server_time > 0:
        # 用云端时间修正防回拨基准：以 max(本机, 云端) 作为新的运行时间锚点
        guard = machine_mod.load_guard()
        anchor = max(now, int(server_time))
        if anchor > int(guard.get("last_run_at", 0) or 0):
            guard["last_run_at"] = anchor
            machine_mod.save_guard(guard)


def _last_cloud_sync() -> int:
    with db.read() as conn:
        return int(kv_repo.get(conn, "last_cloud_sync_at", "0") or 0)


def offline_grace_exceeded() -> bool:
    """超过宽限天数未成功联网校验 → True（应降级免费版并提示联网）。"""
    last = _last_cloud_sync()
    if not last:
        return False  # 从未联网过（如新装离线用解锁码激活），不做宽限限制
    return int(time.time()) - last > OFFLINE_GRACE_DAYS * 86400


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
    payload = apply_license_code(code)
    if _load_code() != code:
        raise LicenseError("授权码未能写入本地数据库，请重试或联系客服")
    _mark_cloud_sync()
    return payload


# ---------------------------------------------------------------- 续期元数据（叠加落地）
_LICENSE_META_KV = "license_meta"


def _load_meta() -> dict:
    with db.read() as conn:
        raw = kv_repo.get(conn, _LICENSE_META_KV, "") or ""
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    with db.tx() as conn:
        kv_repo.set(conn, _LICENSE_META_KV, json.dumps(meta, ensure_ascii=False))


def apply_license_code(code: str) -> dict:
    """验签 + 防降级 + 黑名单校验 后落地。

    安全红线：绝不轻信云端返回的"已支付"状态，必须本地验签通过才算数。
    有效期统一以云端签名的 exp 为准（云端已按本机历史订单叠加好），
    本地不再二次叠加，避免展示日期晚于真实有效期。
    """
    payload = verify_code(code)          # 签名/机器码/过期校验都在这里
    new_plan = payload.get("plan")
    if new_plan not in PLANS or new_plan == "free":
        raise LicenseError("授权码套餐类型无效")

    raw_exp = int(payload.get("exp", 0) or 0)
    if raw_exp <= 0:
        raise LicenseError("授权码缺少有效期")

    # ---- 读取旧数据 ----
    old = current()                       # 已验签且在有效期内的旧授权
    old_plan = old.get("plan") if old else None
    old_exp = int(old.get("exp", 0)) if old else 0

    # ---- 防降级：禁止高级版被低级版覆盖 ----
    if old_plan and PLAN_RANK.get(new_plan, -1) < PLAN_RANK.get(old_plan, -1):
        raise LicenseError(
            f"当前已是{PLANS[old_plan]['name']}，不能降级激活{PLANS[new_plan]['name']}。"
            "如需更换套餐请先联系客服")
    # 云端统一发放：若返回的有效期反而更短（重复激活旧订单），保留现有授权
    if old_exp > raw_exp:
        raise LicenseError("该订单有效期短于当前授权，无需重复激活")

    duration_days = int(payload.get("duration_days", 0) or 0)
    if duration_days <= 0:
        # 兼容旧版云端码：无 duration_days 字段时按签发有效期反推购买天数
        duration_days = max(1, (raw_exp - int(time.time())) // 86400)

    final_exp = raw_exp                   # 到期时间只由云端决定，本地仅展示
    _save_code(code)
    _save_meta({
        "final_exp": final_exp,
        "duration_days": duration_days,
        "old_plan": old_plan,
        "old_exp": old_exp,
        "new_plan": new_plan,
        "apply_reason": "云端签发",
        "applied_at": int(time.time()),
        "order_id": payload.get("order_id", ""),
    })

    payload["final_exp"] = final_exp
    _READONLY_CACHE.update({"at": 0.0, "value": False})
    payload["duration_days"] = duration_days
    payload["apply_reason"] = "云端签发"
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
    blacklisted = bool(resp.get("blacklisted"))
    if blacklisted:
        clear_license()
    _mark_cloud_sync(int(resp.get("server_time", 0) or 0))
    s = status()
    s["blacklisted"] = blacklisted
    return s


# 后台自动联网校验间隔：远小于 15 天离线宽限，避免正常用户被误降级
SYNC_INTERVAL = 6 * 3600


def auto_sync(max_age: int = SYNC_INTERVAL) -> bool:
    """后台自动同步（由启动线程周期调用）：已激活且距上次成功同步超过 max_age 才执行。

    解决「付费用户从不点『同步授权状态』→ 超过 15 天离线宽限被误降级为免费版」。
    任何异常都静默忽略，绝不影响主流程；有授权时同时刷新黑名单状态。
    """
    try:
        if dev_mode() or not _load_code():
            return False
        last = _last_cloud_sync()
        if last and int(time.time()) - last < max_age:
            return False
        sync_license()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 在线支付（本地代理云端）
def payment_create(plan_type: str) -> dict:
    """向云端下单：套餐 + 本机机器码 → trade_order_id + 支付二维码，并本地登记处理中订单。"""
    plan = (plan_type or "").strip().lower()
    if plan not in PLANS or plan == "free":
        raise CloudError("套餐类型无效")
    resp = _post("/api/payment/create", {
        "plan_type": plan,
        "machine_code": machine_mod.machine_code(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or resp.get("detail") or "下单失败，请稍后重试")
    with db.tx() as conn:
        kv_repo.set(conn, PENDING_ORDER_KV, json.dumps({
            "trade_order_id": resp.get("trade_order_id", ""),
            "plan": plan, "amount": resp.get("amount"),
            "created_at": int(time.time()),
        }, ensure_ascii=False))
    return resp


def payment_check(trade_order_id: str = "") -> dict:
    """主动查单：拿到 license_key 后本地验签（红线）才落地；成功则清除处理中订单。"""
    tid = (trade_order_id or "").strip()
    if not tid:
        pending = pending_order()
        tid = (pending or {}).get("trade_order_id", "")
    if not tid:
        raise CloudError("没有可查询的订单")
    resp = _post("/api/payment/check_active", {"trade_order_id": tid})
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or resp.get("detail") or "查询失败，请稍后重试")
    _mark_cloud_sync()
    code = resp.get("license_key") or ""
    if not code:
        return {"status": "pending", "message": "订单暂未支付", "trade_order_id": tid}
    # 状态显示已支付但验签失败 → 立刻拦截（防伪造云端响应）
    payload = apply_license_code(code)
    if _load_code() != code:
        raise LicenseError("授权码未能写入本地数据库，请点击「手动刷新」重试")
    clear_pending_order()
    return {"status": "paid", "message": "支付成功，授权已激活",
            "plan": payload.get("plan"), "exp": payload.get("exp"),
            "trade_order_id": tid}


def recover_license(order_suffix: str) -> dict:
    """自助找回：凭订单号后 6 位 + 本机机器码向云端取回授权码，验签后落地。

    用于「关掉支付弹窗 / 换机重装 / 回调延迟」等已付款但没拿到授权的情况。
    """
    suffix = (order_suffix or "").strip().upper()
    if len(suffix) < 6:
        raise CloudError("请输入订单号后 6 位")
    resp = _post("/api/payment/recover", {
        "order_suffix": suffix,
        "machine_code": machine_mod.machine_code(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "未找到可用授权，请联系客服")
    code = resp.get("license_key") or ""
    if not code:
        raise CloudError("该订单尚未签发授权，请稍后重试")
    payload = apply_license_code(code)
    if _load_code() != code:
        raise LicenseError("授权码未能写入本地数据库，请重试")
    _mark_cloud_sync()
    return {"status": "paid", "message": "授权已找回并激活",
            "plan": payload.get("plan"), "exp": payload.get("exp"),
            "trade_order_id": resp.get("trade_order_id", "")}


def pending_order() -> Optional[dict]:
    with db.read() as conn:
        raw = kv_repo.get(conn, PENDING_ORDER_KV, "") or ""
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not data.get("trade_order_id"):
        return None
    # 超过有效期自动清理：只影响横幅提示，订单号仍可在云端查回授权
    if int(time.time()) - int(data.get("created_at", 0) or 0) > PENDING_ORDER_TTL:
        clear_pending_order()
        return None
    return data


def clear_pending_order() -> None:
    with db.tx() as conn:
        kv_repo.delete(conn, PENDING_ORDER_KV)


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
    """(payload, error, expired)：签名合法但过期时不视为生效授权。

    附加安全闸：
    - 系统时间回拨 → 授权异常降级（防改时间白嫖）
    - 超过离线宽限期未联网校验 → 降级免费版并提示联网
    - 叠加续期：若 metadata 中存在 final_exp，用它覆盖 payload.exp
    """
    _guard_touch_runtime()
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

    # 到期时间一律以云端签名 exp 为准；历史 metadata 的 final_exp 仅用于展示，不参与判定
    if clock_tampered():
        return payload, "检测到系统时间异常，授权已暂停使用，请校准时间后重启软件", False
    if offline_grace_exceeded():
        return payload, f"已超过 {OFFLINE_GRACE_DAYS} 天未联网校验，请联网同步授权状态", False
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


def seat_limit() -> int:
    """本机当前套餐允许的本地账号席位（免费/个人 1，企业 3）。"""
    return int(PLANS[plan_key()].get("seat_limit", 1) or 1)


_READONLY_CACHE = {"at": 0.0, "value": False}


def readonly_mode() -> bool:
    """付费授权到期后的只读模式：登录与读取放行，写操作全部拦截。

    只在「曾经激活过付费版但已过期」时成立；从未付费的免费版不属于只读。
    结果缓存 30 秒，避免每个请求都读写防篡改登记簿。
    """
    if dev_mode():
        return False
    now = time.time()
    if now - _READONLY_CACHE["at"] < 30:
        return bool(_READONLY_CACHE["value"])
    payload, _error, expired = _effective()
    value = bool(payload) and bool(expired)
    _READONLY_CACHE["at"] = now
    _READONLY_CACHE["value"] = value
    return value


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

    # 叠加元数据：展示续费/升级的累计信息
    meta = _load_meta() if not dev else {}

    return {
        "activated": active,
        "plan": current_plan,
        "plan_name": PLANS[current_plan]["name"],
        "features": PLANS[current_plan],
        "exp": required,
        "exp_date": time.strftime("%Y-%m-%d", time.localtime(required)) if required else "",
        "expired": expired,
        "readonly": readonly_mode(),
        "seat_limit": seat_limit(),
        "device_total": int(PLANS[current_plan].get("devices", 1) or 1),
        "error": error,
        "dev_mode": dev,
        "machine_code": machine_mod.machine_code(),
        "role": (payload or {}).get("role", "master") if active else "master",
        "sub_limit": int((payload or {}).get("sub_limit", 0) or 0) if active else 0,
        "order_id": (payload or {}).get("order_id", "") if active else "",
        # 叠加续期元数据（前端用于展示续费/升级历史）
        "duration_days": int(meta.get("duration_days", 0) or 0),
        "apply_reason": meta.get("apply_reason", ""),
        "applied_at": int(meta.get("applied_at", 0) or 0),
        "quota": quota_status(),
        "plans": [
            {"key": k, **v, "current": k == current_plan}
            for k, v in PLANS.items()
        ],
        "payment_qr_url": settings.payment_qr_url,
        "pending_order": pending_order(),
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
