"""商业化授权：Ed25519 验签、激活、套餐权益、月度额度、多设备绑定。

安全边界：
- 本机只内置**公钥**，只能验签，无法伪造授权码。
- 私钥仅存在于云端发码服务（cloud/license_server.py），绝不下发客户端。
- 授权码 = Base64(数据部分) + "." + Base64(Ed25519 签名)，签名覆盖数据原文。
"""
import base64
import hashlib
import json
import threading
import time
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from app.config import SKUS, settings
from app.infra import machine as machine_mod
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import kv_repo

logger = get_logger(__name__)

LICENSE_KV_KEY = "license_code"
PENDING_ORDER_KV = "pay_pending_order"
# 本地处理中订单有效期：超过后自动清除，避免 /plans 顶部横幅长期挂着过期订单
PENDING_ORDER_TTL = 30 * 60
CLOUD_TIMEOUT = 8.0

# 套餐等级（防降级比较用）：free < basic < enterprise
PLAN_RANK = {"free": 0, "basic": 1, "enterprise": 2}
# 旧版授权码的套餐别名：personal 统一归入 basic（历史码永不失效）
_PLAN_ALIAS = {"personal": "basic"}
# 永久买断码的到期哨兵：exp 固定为该值，真正决定“能否升级新版本”的是 maint_exp
LIFETIME_EXP = int(settings.lifetime_exp)
# 离线宽限期：非买断授权距最后一次成功联网校验超过该天数即暂停使用
OFFLINE_GRACE_DAYS = 15
# 防时间篡改：允许系统时间最多回拨 10 分钟（时区/校时误差），超过视为篡改
CLOCK_ROLLBACK_TOLERANCE = 600
# 纯离线环境下免费体验额度硬上限：攻击者要想无限白嫖必须永远断网，
# 每次最多 10 张，用完还得手动删四源+重启软件——50 张要搞 5 次，成本足够
OFFLINE_TRIAL_MAX = 10

# 套餐权益（免费版为一次性体验额度，不做月度重置）
PLANS = {
    "free": {
        "name": "免费体验版", "price": "0 元", "monthly_invoice_quota": None,
        "trial_invoices": int(settings.trial_invoice_quota), "lifetime": False,
        "matching": False, "reimburse": False, "report": False,
        "export": False, "team": False, "devices": 1, "seat_limit": 1,
        "rules": False, "audit": False, "handover": False, "custom_categories": False,
        "desc": f"一次性体验 {int(settings.trial_invoice_quota)} 张发票额度，仅基础台账",
    },
    "basic": {
        "name": "基础版", "price": "199 元买断", "monthly_invoice_quota": None,
        "lifetime": True,
        "matching": True, "reimburse": True, "report": True,
        "export": True, "team": False, "devices": 1, "seat_limit": 1,
        "rules": True, "audit": False, "handover": False, "custom_categories": True,
        "desc": "永久可用，不限额度，开放智能匹配、报销单、差异报告与基础风控",
    },
    "enterprise": {
        "name": "企业版", "price": "499 元买断", "monthly_invoice_quota": None,
        "lifetime": True,
        "matching": True, "reimburse": True, "report": True,
        "export": True, "team": True, "devices": 3, "seat_limit": 3,
        "rules": True, "audit": True, "handover": True, "custom_categories": True,
        "desc": "3 台设备，永久可用，多账号协同、审计日志与数据交接",
    },
}


def _norm_plan(plan) -> str:
    """套餐名归一化：历史 personal 码映射为 basic，未知值原样返回。"""
    plan = str(plan or "").strip()
    return _PLAN_ALIAS.get(plan, plan)


def _is_lifetime(payload: dict) -> bool:
    """是否永久买断码：v2 用 lifetime/kind 字段；旧码视为年费订阅。"""
    if not payload:
        return False
    return bool(payload.get("lifetime")) or payload.get("kind") == "lifetime"


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


def verify_code(code: str, allow_core_match: bool = False) -> dict:
    """用内置公钥验签，返回授权数据；任何异常都抛 LicenseError。

    机器码策略：
    - 完全匹配 → 通过；
    - 仅核心硬件一致（换网卡/硬盘）→ 只有联网流程（allow_core_match=True）才放行，
      离线时一律要求完全匹配，避免克隆系统盘复制授权。
    永久买断码不做 exp 过期判断，维护期由 maint_exp 单独表达。
    """
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

    expected = str(payload.get("machine_code", "")).strip()
    if expected != machine_mod.machine_code():
        core = str(payload.get("core_fp", "")).strip()
        local_core = machine_mod.core_fp()
        if not (allow_core_match and core and local_core and core == local_core):
            raise LicenseError("授权码与本机机器码不匹配，请联系客服重置绑定")

    if not _is_lifetime(payload) and int(payload.get("exp", 0)) < int(time.time()):
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


# ---------------------------------------------------------------- 付费历史（只读判定依据）
def _mark_paid(payload: dict) -> None:
    """记录“曾经付费成功”的事实到隐藏登记簿：删库/清缓存也不会退回免费版。"""
    try:
        with machine_mod.GUARD_LOCK:
            guard = machine_mod.load_guard()
            guard["paid"] = {
                "plan": _norm_plan(payload.get("plan")),
                "lifetime": _is_lifetime(payload),
                "at": int(time.time()),
            }
            machine_mod.save_guard(guard)
    except Exception as e:
        logger.warning("写入付费历史失败：%s", e)


def has_paid_history() -> bool:
    try:
        return bool((machine_mod.load_guard() or {}).get("paid"))
    except Exception:
        return False


def clear_paid_history() -> None:
    """仅“管理员主动注销本机授权”时调用：清除付费痕迹，避免注销后落入只读。"""
    try:
        guard = machine_mod.load_guard()
        guard.pop("paid", None)
        guard.pop("device_role", None)   # 注销后不再是子设备，重新绑定/购买按主设备处理
        machine_mod.save_guard(guard)
    except Exception as e:
        logger.warning("清除付费历史失败：%s", e)


def _remember_device_role(payload: dict) -> None:
    """把「主设备/子设备」身份写进隐藏登记簿。

    授权码被云端拉黑清除后（clear_license）本地已无码可读，但子设备身份必须
    保留下来，才能在只读期继续拦住子设备的业务数据访问。
    """
    try:
        role = "sub" if payload.get("role") == "sub" else "master"
        with machine_mod.GUARD_LOCK:
            guard = machine_mod.load_guard()
            if guard.get("device_role") != role:
                guard["device_role"] = role
                machine_mod.save_guard(guard)
    except Exception as e:
        logger.warning("记录设备角色失败：%s", e)


def device_role() -> str:
    """本机角色：sub=子设备，master=主设备（默认）。"""
    try:
        return "sub" if machine_mod.load_guard().get("device_role") == "sub" else "master"
    except Exception:
        return "master"


# ---------------------------------------------------------------- 防时间篡改 + 离线宽限
def _guard_touch_runtime() -> None:
    """每次状态判定时刷新「最后正常运行时间」（取历史最大值，单调不减）。"""
    with machine_mod.GUARD_LOCK:
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


def _verify_server_time(resp: dict, nonce: str) -> int:
    """校验云端签名的服务器时间：验签失败一律返回 0（绝不采信未签名时间）。

    明文 HTTP 下 /api/status 可被中间人伪造，若直接采信 server_time，
    可把防回拨基准锚定到未来从而永久绕过过期限制。因此时间必须签名。
    """
    try:
        st = int(resp.get("server_time", 0) or 0)
        sig = resp.get("time_sig", "") or ""
        if st <= 0 or not sig:
            return 0
        pub = serialization.load_pem_public_key(settings.license_public_key_pem)
        pub.verify(_b64d(sig), f"{st}|{nonce}".encode("utf-8"))
        return st
    except Exception:
        return 0


def _new_nonce() -> str:
    import secrets
    return secrets.token_hex(8)


def _mark_cloud_sync(server_time: int = 0) -> None:
    """记录最后一次成功联网校验时间；云端返回了签名服务器时间则校正本机时钟基准。

    有签名时间时以它为准**覆盖**基准（而不是只抬高）：用户把系统时间改到未来
    再改回来时，只有服务器时间能把被污染的基准拉回真实值，否则会永久误判回拨。
    """
    now = int(time.time())
    with db.tx() as conn:
        kv_repo.set(conn, "last_cloud_sync_at", str(now))
    if server_time > 0:
        with machine_mod.GUARD_LOCK:
            guard = machine_mod.load_guard()
            # 签名服务器时间就是真实世界时间：直接作为基准（可上调也可下调），
            # 这样“把时间改到过去→联网→再改回来”以及“时间被改到未来被污染”都能自愈。
            guard["last_run_at"] = int(server_time)
            guard["last_cloud_time"] = int(server_time)
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
        "core_fp": machine_mod.core_fp(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "订单校验失败")
    code = resp.get("license_code") or ""
    payload = apply_license_code(code, allow_core_match=True)
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


def apply_license_code(code: str, allow_core_match: bool = False) -> dict:
    """验签 + 防降级 + 防状态丢失 后落地。

    安全红线：绝不轻信云端返回的"已支付"状态，必须本地验签通过才算数。
    生命周期规则：
    - 永久买断：exp 固定为哨兵值，永不过期；maint_exp 只表达维护期；
    - 续费/维护码：不得降级套餐、不得把买断降为年费、不得缩短维护期；
    - 年费历史码：有效期不得倒退（防重复激活旧订单）。
    """
    payload = verify_code(code, allow_core_match=allow_core_match)
    new_plan = _norm_plan(payload.get("plan"))
    if new_plan not in PLANS or new_plan == "free":
        raise LicenseError("授权码套餐类型无效")

    now = int(time.time())
    new_lifetime = _is_lifetime(payload)
    if new_lifetime:
        final_exp = LIFETIME_EXP
        final_maint = int(payload.get("maint_exp") or 0) or (now + 365 * 86400)
    else:
        final_exp = int(payload.get("exp", 0) or 0)
        if final_exp <= 0:
            raise LicenseError("授权码缺少有效期")
        final_maint = int(payload.get("maint_exp") or 0) or final_exp

    # ---- 读取旧数据 ----
    old = current()
    old_plan = _norm_plan(old.get("plan")) if old else None
    old_lifetime = _is_lifetime(old) if old else False
    old_exp = int(old.get("exp", 0)) if old else 0
    old_maint = int((old or {}).get("maint_exp", old_exp) or 0)

    # ---- 防降级：套餐等级不能降 ----
    if old_plan and PLAN_RANK.get(new_plan, -1) < PLAN_RANK.get(old_plan, -1):
        raise LicenseError(
            f"当前已是{PLANS[old_plan]['name']}，不能降级激活{PLANS[new_plan]['name']}。"
            "如需更换套餐请先联系客服")
    # ---- 防状态丢失：永久买断不能被年费码覆盖 ----
    if old_lifetime and not new_lifetime:
        raise LicenseError("当前已是永久买断授权，无需也不能替换为年费授权")
    # ---- 续费不得缩短维护期：旧码维护期更晚时拒绝覆盖（防用旧码回滚）----
    if old_lifetime and new_lifetime and final_maint < old_maint:
        raise LicenseError("该维护码的维护期早于当前授权，无需重复激活")
    # 年费码：有效期反而更短（重复激活旧订单）时拒绝
    if not new_lifetime and old_exp > final_exp:
        raise LicenseError("该订单有效期短于当前授权，无需重复激活")

    duration_days = int(payload.get("duration_days", 0) or 0)
    if duration_days <= 0:
        # 兼容旧版云端码：无 duration_days 字段时按签发时长反推
        base = final_maint if new_lifetime else final_exp
        duration_days = max(1, (base - now) // 86400)

    _save_code(code)
    _save_meta({
        "final_exp": final_exp,
        "final_maint": final_maint,
        "lifetime": new_lifetime,
        "duration_days": duration_days,
        "old_plan": old_plan,
        "old_exp": old_exp,
        "new_plan": new_plan,
        "apply_reason": "云端签发",
        "applied_at": now,
        "order_id": payload.get("order_id", ""),
    })
    _mark_paid(payload)
    _remember_device_role(payload)
    if allow_core_match:
        _remember_core_match(code)
    _READONLY_CACHE.update({"at": 0.0, "value": False})

    payload["plan"] = new_plan
    payload["lifetime"] = new_lifetime
    payload["final_exp"] = final_exp
    payload["final_maint"] = final_maint
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
        "core_fp": machine_mod.core_fp(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "绑定失败")
    code = resp.get("license_code") or ""
    return apply_license_code(code, allow_core_match=True)


def unlock_code_verify(code: str) -> dict:
    """超管解锁码：客服持私钥签发，绑定机器码 + 短期有效期。"""
    payload = verify_code(code)
    if payload.get("type") != "unlock":
        raise LicenseError("该码不是超管解锁码")
    return payload


def sync_license() -> dict:
    """同步黑名单/换机状态/免费额度锁定：云端判定拉黑则本地立即降级为只读/免费版。

    同步返回里的 trial_locked 必须写入本地登记簿：云端是唯一可信来源，
    本地怎么清 guard.json / 注册表 / DB 计数字段都无法让云端"忘记"这台机器。
    """
    nonce = _new_nonce()
    resp = _post("/api/status", {
        "machine_code": machine_mod.machine_code(),
        "core_fp": machine_mod.core_fp(),
        "nonce": nonce,
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "同步失败")
    blacklisted = bool(resp.get("blacklisted"))
    if blacklisted:
        clear_license()
    # 云端是免费额度的可信来源：只要云端标记 locked，本地所有存储都必须以它为准
    trial_locked = bool(resp.get("trial_locked"))
    if trial_locked:
        try:
            with machine_mod.GUARD_LOCK:
                guard = machine_mod.load_guard()
                if not guard.get("trial_locked"):
                    guard["trial_locked"] = True
                    machine_mod.save_guard(guard)
        except Exception as e:
            logger.warning("同步免费额度锁失败：%s", e)
    # 只采信验签通过的服务器时间作为防回拨基准
    _mark_cloud_sync(_verify_server_time(resp, nonce))
    s = status()
    s["blacklisted"] = blacklisted
    s["trial_locked"] = trial_locked
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
    """向云端下单：只传套餐代码，金额与有效期完全由云端按字典匹配。

    前端永远不传金额/到期时间/版本类型；即使被篡改请求体也无法改变价格。
    """
    key = (plan_type or "").strip().lower()
    if key in SKUS:
        sku, plan = key, SKUS[key]["plan"]
    elif _norm_plan(key) in PLANS and _norm_plan(key) != "free":
        # 兼容旧前端：传 plan 名时映射到对应的买断 SKU
        plan = _norm_plan(key)
        sku = {"basic": "basic_lifetime", "enterprise": "ent_lifetime"}.get(plan, "")
    else:
        raise CloudError("套餐类型无效")
    resp = _post("/api/payment/create", {
        "sku": sku,
        "plan_type": plan,
        "machine_code": machine_mod.machine_code(),
        "core_fp": machine_mod.core_fp(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or resp.get("detail") or "下单失败，请稍后重试")
    with db.tx() as conn:
        kv_repo.set(conn, PENDING_ORDER_KV, json.dumps({
            "trade_order_id": resp.get("trade_order_id", ""),
            "sku": sku, "plan": plan, "amount": resp.get("amount"),
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
    payload = apply_license_code(code, allow_core_match=True)
    if _load_code() != code:
        raise LicenseError("授权码未能写入本地数据库，请点击「手动刷新」重试")
    clear_pending_order()
    return {"status": "paid", "message": "支付成功，授权已激活",
            "plan": payload.get("plan"), "exp": payload.get("exp"),
            "lifetime": payload.get("lifetime"),
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
        "core_fp": machine_mod.core_fp(),
    })
    if not resp.get("ok"):
        raise CloudError(resp.get("message") or "未找到可用授权，请联系客服")
    code = resp.get("license_key") or ""
    if not code:
        raise CloudError("该订单尚未签发授权，请稍后重试")
    payload = apply_license_code(code, allow_core_match=True)
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
# 联网时凭核心硬件软匹配成功 → 本次会话内（24h）允许离线继续使用；
# 超过 24h 未联网仍要求完全匹配，避免克隆盘无限期复制授权。
_CORE_OK = {"sig": "", "at": 0.0}
CORE_OK_TTL = 24 * 3600


def _code_sig(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()[:24]


def _remember_core_match(code: str) -> None:
    _CORE_OK.update({"sig": _code_sig(code), "at": time.time()})


def _core_allowed(code: str) -> bool:
    return (bool(_CORE_OK["sig"]) and _CORE_OK["sig"] == _code_sig(code)
            and time.time() - _CORE_OK["at"] < CORE_OK_TTL)


def current() -> Optional[dict]:
    """当前生效授权（验签通过且未过期），否则 None。"""
    code = _load_code()
    if not code:
        return None
    try:
        return verify_code(code, allow_core_match=_core_allowed(code))
    except LicenseError:
        return None


def _effective() -> tuple:
    """(payload, error, expired)：四态判定的唯一入口。

    安全闸：
    - 系统时间回拨 → 授权暂停（防改时间白嫖），买断用户同样受限；
    - 非买断授权超离线宽限 → 暂停（买断永久有效，不做离线宽限，避免误伤客户）；
    - 永久买断码不以 exp 判过期，维护期只在展示层使用。
    """
    _guard_touch_runtime()
    code = _load_code()
    if not code:
        return None, "", False
    try:
        payload = verify_code(code, allow_core_match=_core_allowed(code))
    except LicenseError as e:
        raw = _decode_unchecked(code)
        lifetime = _is_lifetime(raw or {})
        exp = int((raw or {}).get("exp", 0))
        expired = bool(raw) and not lifetime and exp < int(time.time())
        # 过期属于正常降级，不当作"授权码非法"提示
        return raw, "" if expired else str(e), expired

    lifetime = _is_lifetime(payload)
    if clock_tampered():
        return payload, "检测到系统时间异常，授权已暂停使用，请校准时间后重启软件", False
    if not lifetime and offline_grace_exceeded():
        return payload, f"已超过 {OFFLINE_GRACE_DAYS} 天未联网校验，请联网同步授权状态", False
    if lifetime:
        return payload, "", False
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
    plan = _norm_plan(payload.get("plan"))
    return plan if plan in PLANS else "free"


def features() -> dict:
    return PLANS[plan_key()]


def seat_limit() -> int:
    """本机当前套餐允许的本地账号席位（免费/基础 1，企业 3）。"""
    return int(PLANS[plan_key()].get("seat_limit", 1) or 1)


_READONLY_CACHE = {"at": 0.0, "value": False}


def readonly_mode() -> bool:
    """只读模式：只在「曾付费但当前授权失效」时成立。

    买断授权永久可用，永远不会因“维护到期/授权码过期”进入只读；
    触发只读的情形仅为：被拉黑、退款、授权码损坏、机器码不符、时间篡改。
    从未付费的免费体验版不属于只读。
    """
    if dev_mode():
        return False
    now = time.time()
    if now - _READONLY_CACHE["at"] < 30:
        return bool(_READONLY_CACHE["value"])
    payload, error, expired = _effective()
    active = bool(payload) and not error and not expired
    if active:
        value = False
    else:
        # 触发只读的情形：被拉黑、退款、授权码损坏、机器码/核心硬件不符、时间篡改。
        # 只要本机曾付费成功过，就进入只读而不是掉回免费版（免费体验用户不受影响）。
        # 买断用户的“维护期到期”不算授权失效，前面 active 已为 True，绝不进只读。
        value = has_paid_history()
    _READONLY_CACHE["at"] = now
    _READONLY_CACHE["value"] = value
    return value


def maint_status() -> dict:
    """维护期状态：只影响“能否升级新款发票识别/新版本”，不影响已有功能。"""
    payload = current() or {}
    lifetime = _is_lifetime(payload)
    maint_exp = int(payload.get("maint_exp") or 0) or int(payload.get("exp") or 0)
    return {
        "lifetime": lifetime,
        "maint_exp": maint_exp,
        "maint_date": time.strftime("%Y-%m-%d", time.localtime(maint_exp)) if maint_exp else "",
        "maint_expired": bool(lifetime and maint_exp and maint_exp < int(time.time())),
    }


def status() -> dict:
    payload, error, expired = _effective()
    dev = dev_mode()
    payload = payload or {}
    lifetime = _is_lifetime(payload)
    if dev:
        # 调试模式：始终展示企业版且永久有效，前端据此显示「开发者模式」徽标
        active, expired, error, lifetime = True, False, "", True
    else:
        active = bool(payload) and not expired and not error
    # 买断码展示「永久」，年费码展示到期日；过期页同样需要展示「已于 X 过期」
    required = int(payload.get("exp", 0) or 0)
    exp_date = "永久" if lifetime else (
        time.strftime("%Y-%m-%d", time.localtime(required)) if required else "")
    current_plan = plan_key()

    # 叠加元数据：展示续费/升级的累计信息
    meta = _load_meta() if not dev else {}
    maint = maint_status() if not dev else {
        "lifetime": False, "maint_exp": 0, "maint_date": "", "maint_expired": False}

    return {
        "activated": active,
        "plan": current_plan,
        "plan_name": PLANS[current_plan]["name"],
        "features": PLANS[current_plan],
        "lifetime": lifetime,
        "exp": required,
        "exp_date": exp_date,
        "maint_exp": maint["maint_exp"],
        "maint_date": maint["maint_date"],
        "maint_expired": maint["maint_expired"],
        "expired": expired,
        "readonly": readonly_mode(),
        "seat_limit": seat_limit(),
        "device_total": int(PLANS[current_plan].get("devices", 1) or 1),
        "error": error,
        "dev_mode": dev,
        "machine_code": machine_mod.machine_code(),
        "core_fp": machine_mod.core_fp(),
        "role": payload.get("role", "master") if active else "master",
        "sub_limit": int(payload.get("sub_limit", 0) or 0) if active else 0,
        "order_id": payload.get("order_id", "") if active else "",
        # 叠加续期元数据（前端用于展示续费/升级历史）
        "duration_days": int(meta.get("duration_days", 0) or 0),
        "apply_reason": meta.get("apply_reason", ""),
        "applied_at": int(meta.get("applied_at", 0) or 0),
        "quota": quota_status(),
        "trial_total": int(settings.trial_invoice_quota),
        "plans": [
            {"key": k, **v, "current": k == current_plan}
            for k, v in PLANS.items()
        ],
        "skus": [{"key": k, **v} for k, v in SKUS.items()],
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


# ---------------------------------------------------------------- 免费体验额度（一次性，防重置）
# 额度扣减串行锁：隐藏登记簿是“读文件→加一→写回”，并发上传不串行会丢更新。
# 与时间锚点刷新共用同一把锁（machine.GUARD_LOCK），避免互相覆盖。
_QUOTA_LOCK = machine_mod.GUARD_LOCK


def _trial_used() -> int:
    """已用张数：取隐藏登记簿（加密文件 + 加密注册表）两者的最大值。"""
    try:
        return max(0, int((machine_mod.load_guard() or {}).get("trial_used", 0) or 0))
    except Exception:
        return 0


def trial_status() -> dict:
    total = int(settings.trial_invoice_quota)
    used = min(_trial_used(), total)
    return {"limit": total, "used": used, "remaining": max(0, total - used), "trial": True}


def quota_status() -> dict:
    """额度展示：免费版为一次性体验额度，付费版不限。"""
    if plan_key() != "free":
        return {"limit": None, "used": 0, "remaining": None,
                "month": "", "unlimited": True, "trial": False}
    t = trial_status()
    return {**t, "month": "", "unlimited": False}


def _ensure_trial_locked() -> bool:
    """确保本机免费额度已被云端锁定。

    首次消耗额度前必须先联网确认云端知道这台机器：
    - 联网成功 → 云端自动记录 trial_locked=true，本地也写一份；返回 True
    - 联网失败（真离线）→ 返回 False（调用方此时只能允许最多 OFFLINE_TRIAL_MAX 张）
    """
    try:
        with machine_mod.GUARD_LOCK:
            guard = machine_mod.load_guard()
            if guard.get("trial_locked"):
                return True  # 已经锁过了
        sync_license()
        return True
    except Exception as e:
        logger.info("首次同步云端失败，进入离线体验模式：%s", e)
        return False


def consume_invoice_quota(count: int = 1) -> None:
    """导入前预留体验额度。

    体验额度**只增不减、跨月不恢复**：删除已导入发票不会退还，避免
    "导入→删除→再导入"无限刷。仅"解析失败未入库"才允许 refund。

    首次消耗前强制联网锁：云端是唯一可信来源，本地删四源也无法让云端
    忘记这台机器已开启过免费额度；真离线时硬编码只允许 OFFLINE_TRIAL_MAX 张。
    """
    if count <= 0 or plan_key() != "free":
        return
    total = int(settings.trial_invoice_quota)
    with _QUOTA_LOCK:
        guard = machine_mod.load_guard()
        used = int(guard.get("trial_used", 0) or 0)

        if not guard.get("trial_locked"):
            locked = _ensure_trial_locked()
            if not locked:
                if used + count > OFFLINE_TRIAL_MAX:
                    raise QuotaExceeded(
                        "当前离线环境，免费体验最多 " + str(OFFLINE_TRIAL_MAX)
                        + " 张（已用 " + str(used) + "）。"
                        + "请联网后继续使用（联网后解锁剩余 "
                        + str(max(0, total - used)) + " 张），"
                        + "或升级基础版/企业版后不限额度导入。")
            guard = machine_mod.load_guard()
            used = int(guard.get("trial_used", 0) or 0)

        if used + count > total:
            raise QuotaExceeded(
                f"免费体验额度共 {total} 张，已用 {used} 张。"
                "升级基础版/企业版后可不限额度导入")
        guard["trial_used"] = used + count
        machine_mod.save_guard(guard)


def refund_invoice_quota(count: int = 1) -> None:
    """仅回退“未实际入库”的预留（如解析失败）。已入库发票被删除时不调用本函数。"""
    if count <= 0 or plan_key() != "free":
        return
    with _QUOTA_LOCK:
        guard = machine_mod.load_guard()
        guard["trial_used"] = max(0, int(guard.get("trial_used", 0) or 0) - count)
        machine_mod.save_guard(guard)


def initial_password() -> str:
    return settings.initial_password


def plan_public() -> dict:
    """套餐卡片数据（无授权时也能展示）。金额只用于展示，下单以云端字典为准。"""
    return {
        "plans": [{"key": k, **v} for k, v in PLANS.items()],
        "skus": [{"key": k, **v} for k, v in SKUS.items()],
        "trial_total": int(settings.trial_invoice_quota),
        "payment_qr_url": settings.payment_qr_url,
        "initial_password": settings.initial_password,
    }
