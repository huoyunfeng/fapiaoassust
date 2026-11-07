"""云端授权发码服务（部署在公网服务器，本机客户端只持有公钥）。

启动：
    pip install fastapi uvicorn cryptography requests
    python cloud/license_server.py          # 默认 0.0.0.0:80
    uvicorn cloud.license_server:app --host 0.0.0.0 --port 80

数据文件（与脚本同目录，可用 CLOUD_DATA_DIR 覆盖）：
    orders.json    订单：{订单号: {plan, days, status, machine_code, ...}}
    devices.json   子设备绑定：{子机器码: {parent, plan, exp, order_id}}
    blacklist.json 黑名单：{机器码: {reason, at}}

安全提示：私钥不写进代码，通过环境变量 CLOUD_PRIVATE_KEY_PEM 或文件 private_key.pem
提供（可用 CLOUD_PRIVATE_KEY_FILE 指定路径）；私钥只保存在服务器本地，禁止提交仓库。
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- 密钥（云端绝密，不入库）
def _load_private_key() -> Ed25519PrivateKey:
    """私钥不再硬编码：优先读环境变量，其次读本地 PEM 文件。"""
    pem = os.getenv("CLOUD_PRIVATE_KEY_PEM", "").strip()
    if not pem:
        key_file = Path(os.getenv("CLOUD_PRIVATE_KEY_FILE",
                                  str(Path(__file__).parent / "private_key.pem")))
        if not key_file.exists():
            raise RuntimeError("未找到云端私钥：请设置环境变量 CLOUD_PRIVATE_KEY_PEM，"
                               f"或将私钥 PEM 保存为 {key_file}")
        pem = key_file.read_text(encoding="utf-8")
    return serialization.load_pem_private_key(pem.encode("utf-8"), None)


_KEY = _load_private_key()
_ADMIN_TOKEN = os.getenv("CLOUD_ADMIN_TOKEN", "fapiao-admin-2026")
_DATA_DIR = Path(os.getenv("CLOUD_DATA_DIR", str(Path(__file__).parent)))
_LOCK = threading.RLock()

PLAN_DEVICES = {"personal": 0, "enterprise": 3}
PLAN_DAYS = {"personal": 365, "enterprise": 365}

# ---------------------------------------------------------------- 支付配置（虎皮椒聚合支付）
# TODO 上线前请把 XUNHUPAY_APPSECRET 换成虎皮椒商户后台的真实密钥（不要提交到仓库）
XUNHUPAY_APPID = "201906189707"
XUNHUPAY_APPSECRET = "PLACEHOLDER_SECRET"
NOTIFY_URL = "http://fapiao-assist.xyz/api/payment/callback"

XUNHUPAY_CREATE_URL = "https://api.xunhupay.com/payment/do.html"     # 下单
XUNHUPAY_QUERY_URL = "https://api.xunhupay.com/payment/query.html"   # 主动查单

# 套餐价格（单位：元）
PLAN_PRICES = {"personal": 99, "enterprise": 499}
# 支付标题（展示在收银台）
PLAN_TITLES = {"personal": "发票助手 个人版（1年）",
               "enterprise": "发票助手 企业版（1年）"}


def xunhupay_hash(params: dict) -> str:
    """生成虎皮椒签名：剔除空值与 hash 字段，按参数名 ASCII 升序拼成 k1=v1&k2=v2，
    末尾直接拼接 AppSecret，做 MD5 后返回小写。"""
    items = [(k, v) for k, v in params.items()
             if k != "hash" and v is not None and str(v) != ""]
    items.sort(key=lambda kv: str(kv[0]))
    raw = "&".join(f"{k}={v}" for k, v in items) + XUNHUPAY_APPSECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _nonce() -> str:
    return os.urandom(8).hex()


def _issue_license(order: dict, order_id: str) -> tuple:
    """复用原有私钥签名逻辑，为已支付订单签发授权码，返回 (license_key, exp)。

    平级续费叠加：同机器同套餐的未到期已支付订单，新授权在其到期时间上叠加，
    避免提前续费损失剩余天数。
    """
    plan = order.get("plan")
    if plan not in PLAN_DAYS:
        raise HTTPException(400, "订单套餐类型异常")
    days = int(order.get("days") or PLAN_DAYS[plan])
    # 支付成功即为激活起点，后续重复换码沿用该时间，避免反复续期
    activated_at = int(order.get("activated_at") or 0) or _now()
    order["activated_at"] = activated_at
    base = activated_at
    machine = (order.get("machine_code") or "").strip()
    if machine:
        for other in _load("orders.json", "orders").values():
            if (other.get("status") in ("paid", "used")
                    and (other.get("machine_code") or "").strip() == machine
                    and other.get("plan") == plan
                    and int(other.get("exp", 0) or 0) > base):
                base = int(other["exp"])          # 叠加到最迟到期的同类授权上
    payload = {
        "machine_code": machine,
        "plan": plan, "role": "master",
        "exp": base + days * 86400,
        "sub_limit": PLAN_DEVICES.get(plan, 0),
        "order_id": order_id,
        "issued_at": _now(),
    }
    return sign_payload(payload), payload["exp"]


def _b64e(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_payload(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = _KEY.sign(raw)
    return f"{_b64e(raw)}.{_b64e(sig)}"


def verify_code(code: str) -> dict:
    data_part, sig_part = (code or "").strip().split(".", 1)
    raw = _b64d(data_part)
    _KEY.public_key().verify(_b64d(sig_part), raw)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- 数据文件
def _path(name: str) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR / name


def _load(name: str, key: str) -> dict:
    try:
        return json.loads(_path(name).read_text(encoding="utf-8")).get(key, {})
    except Exception:
        return {}


def _save(name: str, key: str, data: dict) -> None:
    _path(name).write_text(json.dumps({key: data}, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def _now() -> int:
    return int(time.time())


def _is_blacklisted(machine: str) -> dict:
    return _load("blacklist.json", "blacklist").get(machine) or {}


app = FastAPI(title="发票助手云端授权服务", version="1.0")

# 换码接口简易限流：同一 IP 每分钟最多 20 次（防订单号暴力枚举）
_RATE_LIMIT = 20
_RATE_WIN = 60
_rate_hits: dict = {}
_rate_lock = threading.Lock()


def _rate_guard(ip: str) -> None:
    now = _now()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(ip, []) if now - t < _RATE_WIN]
        if len(hits) >= _RATE_LIMIT:
            _rate_hits[ip] = hits
            raise HTTPException(429, "请求过于频繁，请稍后再试")
        hits.append(now)
        _rate_hits[ip] = hits


class LicenseReq(BaseModel):
    order_id: str
    machine_code: str


class BindReq(BaseModel):
    invite_code: str
    machine_code: str


class StatusReq(BaseModel):
    machine_code: str


class UnlockReq(BaseModel):
    machine_code: str
    days: int = 3


class BlacklistReq(BaseModel):
    machine_code: str
    reason: str = ""
    enabled: bool = True


class ResetOrderReq(BaseModel):
    order_id: str


@app.get("/health")
def health():
    return {"ok": True, "service": "fapiao-license", "time": _now()}


@app.post("/api/get_license")
def get_license(req: LicenseReq, request: Request):
    """订单换授权码：一单一机，签名覆盖机器码、套餐、到期时间。"""
    _rate_guard(request.client.host if request.client else "unknown")
    order_id = (req.order_id or "").strip()
    machine = (req.machine_code or "").strip()
    if not order_id or not machine:
        raise HTTPException(400, "订单号和机器码不能为空")
    if _is_blacklisted(machine):
        raise HTTPException(403, "该设备已被列入黑名单")

    with _LOCK:
        orders = _load("orders.json", "orders")
        order = orders.get(order_id)
        if not order:
            raise HTTPException(404, "订单号不存在或已失效")
        if order.get("status") == "refunded":
            raise HTTPException(403, "该订单已退款，无法激活")
        # 支付订单未到账时不允许换码，避免用未支付订单号白拿授权
        if order.get("status") == "pending":
            raise HTTPException(403, "该订单尚未支付，请完成支付后再激活")
        bound = (order.get("machine_code") or "").strip()
        if bound and bound != machine:
            raise HTTPException(409, "该订单已绑定其他设备，请联系客服换机")
        plan = order.get("plan")
        if plan not in PLAN_DAYS:
            raise HTTPException(400, "订单套餐类型异常")
        days = int(order.get("days") or PLAN_DAYS[plan])
        # 首次激活时记录起始时间，重复换码沿用原起始时间，避免每次续期（无限续费）
        activated_at = int(order.get("activated_at") or 0)
        if not activated_at:
            activated_at = _now()
            order["activated_at"] = activated_at
        order.update({"status": "used", "machine_code": machine,
                      "used_at": order.get("used_at")
                      or time.strftime("%Y-%m-%d %H:%M:%S")})
        orders[order_id] = order
        _save("orders.json", "orders", orders)

    # 支付流订单的 exp 已含续费叠加，优先沿用；手工导入订单按激活起点计算
    exp = int(order.get("exp") or 0) or activated_at + days * 86400
    payload = {
        "machine_code": machine, "plan": plan, "role": "master",
        "exp": exp,
        "sub_limit": PLAN_DEVICES.get(plan, 0),
        "order_id": order_id,
        "issued_at": _now(),
    }
    return {"ok": True, "license_code": sign_payload(payload),
            "plan": plan, "exp": payload["exp"],
            "message": "授权签发成功"}


@app.post("/api/sub_bind")
def sub_bind(req: BindReq):
    """子设备绑定：校验主设备企业版授权 + 剩余可绑定台数，签发子授权。"""
    machine = (req.machine_code or "").strip()
    if not machine:
        raise HTTPException(400, "机器码不能为空")
    if _is_blacklisted(machine):
        raise HTTPException(403, "该设备已被列入黑名单")
    try:
        invite = verify_code(req.invite_code)
    except Exception:
        raise HTTPException(400, "企业邀请码无效或已损坏")
    if invite.get("plan") != "enterprise" or invite.get("role") == "sub":
        raise HTTPException(403, "仅企业版主设备可生成企业邀请码")
    parent = invite.get("machine_code")
    if int(invite.get("exp", 0)) < _now():
        raise HTTPException(403, "主设备授权已过期")
    if _is_blacklisted(parent):
        raise HTTPException(403, "主设备已被列入黑名单")

    with _LOCK:
        devices = _load("devices.json", "devices")
        # 统计在效期内且归属于该主设备的子设备（过期条目不再占位）
        bound_to_parent = [d for d in devices.values()
                           if d.get("parent") == parent and int(d.get("exp", 0)) >= _now()]
        limit = int(invite.get("sub_limit", 0) or 0)
        if len(bound_to_parent) >= limit:
            raise HTTPException(403, f"主设备可绑定子设备已达上限（{limit} 台）")
        if devices.get(machine):
            raise HTTPException(409, "本机已绑定其他设备授权，请先解绑")
        devices[machine] = {"parent": parent, "plan": "enterprise",
                            "exp": invite.get("exp"), "order_id": invite.get("order_id"),
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save("devices.json", "devices", devices)

    payload = {
        "machine_code": machine, "plan": "enterprise", "role": "sub",
        "exp": int(invite.get("exp")), "sub_limit": 0,
        "order_id": invite.get("order_id", ""), "parent": parent,
        "issued_at": _now(),
    }
    return {"ok": True, "license_code": sign_payload(payload),
            "message": "子设备绑定成功"}


@app.post("/api/status")
def status(req: StatusReq):
    """客户端定期/手动同步：返回是否被拉黑（拉黑后本地立即降级免费版）。"""
    machine = (req.machine_code or "").strip()
    hit = _is_blacklisted(machine)
    return {"ok": True, "blacklisted": bool(hit),
            "reason": hit.get("reason", "") if hit else "",
            "server_time": _now()}


@app.post("/api/unlock_code")
def unlock_code(req: UnlockReq, x_admin_token: str = Header(default="")):
    """客服侧签发超管解锁码（短期有效，绑定机器码）。"""
    if x_admin_token != _ADMIN_TOKEN:
        raise HTTPException(401, "管理员令牌无效")
    machine = (req.machine_code or "").strip()
    if not machine:
        raise HTTPException(400, "机器码不能为空")
    payload = {"type": "unlock", "machine_code": machine,
               "exp": _now() + max(1, int(req.days)) * 86400, "issued_at": _now()}
    return {"ok": True, "unlock_code": sign_payload(payload),
            "exp": payload["exp"]}


@app.post("/api/blacklist")
def set_blacklist(req: BlacklistReq, x_admin_token: str = Header(default="")):
    if x_admin_token != _ADMIN_TOKEN:
        raise HTTPException(401, "管理员令牌无效")
    with _LOCK:
        data = _load("blacklist.json", "blacklist")
        if req.enabled:
            data[req.machine_code] = {"reason": req.reason,
                                      "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        else:
            data.pop(req.machine_code, None)
        _save("blacklist.json", "blacklist", data)
    return {"ok": True, "blacklisted": req.enabled}


@app.post("/api/reset_order")
def reset_order(req: ResetOrderReq, x_admin_token: str = Header(default="")):
    """客服换机：解绑订单与旧机器码，允许重新激活。"""
    if x_admin_token != _ADMIN_TOKEN:
        raise HTTPException(401, "管理员令牌无效")
    with _LOCK:
        orders = _load("orders.json", "orders")
        order = orders.get(req.order_id)
        if not order:
            raise HTTPException(404, "订单不存在")
        devices = _load("devices.json", "devices")
        parent = order.get("machine_code")
        for m in [k for k, v in devices.items() if v.get("parent") == parent]:
            devices.pop(m, None)
        _save("devices.json", "devices", devices)
        order.update({"status": "unused", "machine_code": None, "used_at": None})
        orders[req.order_id] = order
        _save("orders.json", "orders", orders)
    return {"ok": True, "message": "订单已解绑，可在新设备重新激活"}


# ---------------------------------------------------------------- 在线支付（虎皮椒）
class PayCreateReq(BaseModel):
    plan_type: str
    machine_code: str


class PayCheckReq(BaseModel):
    trade_order_id: str


@app.post("/api/payment/create")
def payment_create(req: PayCreateReq):
    """支付下单：校验套餐 → 生成商户订单号 → 请求虎皮椒拿二维码，订单先记为 pending。"""
    plan = (req.plan_type or "").strip().lower()
    machine = (req.machine_code or "").strip()
    if plan not in PLAN_PRICES:
        raise HTTPException(400, "套餐类型不存在，可选：personal / enterprise")
    if not machine:
        raise HTTPException(400, "机器码不能为空")
    if _is_blacklisted(machine):
        raise HTTPException(403, "该设备已被列入黑名单")

    price = PLAN_PRICES[plan]
    total_fee = f"{price:.2f}"
    # 商户唯一订单号：FP + 时间戳 + 8 位随机数（32bit，防枚举猜测）
    trade_order_id = f"FP{_now()}{os.urandom(4).hex().upper()}"

    params = {
        "appid": XUNHUPAY_APPID,
        "version": "1.1",
        "trade_order_id": trade_order_id,
        "total_fee": total_fee,
        "title": PLAN_TITLES.get(plan, f"发票助手 {plan}"),
        "time": str(_now()),
        "notify_url": NOTIFY_URL,
        "callback_url": NOTIFY_URL,
        "nonce_str": _nonce(),
        "type": "WAP",
        "wap_url": NOTIFY_URL.replace("/api/payment/callback", ""),
        "wap_name": "发票助手",
        "plugins": "fapiao",
        "attach": plan,
    }
    params["hash"] = xunhupay_hash(params)

    try:
        resp = requests.post(XUNHUPAY_CREATE_URL, data=params, timeout=15)
        data = resp.json()
    except Exception as exc:  # 网络异常 / 网关返回非 JSON
        raise HTTPException(502, f"支付网关请求失败：{exc}")

    if int(data.get("errcode", -1)) != 0:
        raise HTTPException(502, f"下单失败：{data.get('errmsg') or data}")
    url_qrcode = data.get("url_qrcode") or data.get("url") or ""
    if not url_qrcode:
        raise HTTPException(502, f"下单失败：未返回支付二维码 {data}")

    # 先落库为 pending，回调到账后再改为 paid，防止未支付订单直接换码
    with _LOCK:
        orders = _load("orders.json", "orders")
        orders[trade_order_id] = {
            "plan": plan,
            "days": PLAN_DAYS.get(plan, 365),
            "status": "pending",
            "machine_code": machine,
            "amount": price * 100,          # 单位：分，便于回调金额比对
            "trade_order_id": trade_order_id,
            "trade_no": "",
            "license_key": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "paid_at": "",
        }
        _save("orders.json", "orders", orders)

    return {"ok": True, "trade_order_id": trade_order_id,
            "url_qrcode": url_qrcode, "url": data.get("url", ""),
            "plan": plan, "amount": price}


def _callback_fail(msg: str) -> PlainTextResponse:
    """回调处理失败：返回纯文本 fail，让虎皮椒稍后重试。"""
    return PlainTextResponse(f"fail: {msg}", status_code=400)


@app.post("/api/payment/callback")
async def payment_callback(request: Request):
    """支付异步回调：验签 → 校验状态与金额 → 发码 → 幂等落库，最后必须回纯文本 success。"""
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}

    # 1. 验签（剔除 hash 后按同样规则重算）
    recv_hash = (data.get("hash") or "").strip().lower()
    if not recv_hash or xunhupay_hash(data) != recv_hash:
        return _callback_fail("签名校验失败")

    trade_order_id = (data.get("trade_order_id") or "").strip()
    pay_status = (data.get("status") or "").strip().upper()
    if not trade_order_id:
        return _callback_fail("缺少商户订单号")

    with _LOCK:
        orders = _load("orders.json", "orders")
        order = orders.get(trade_order_id)
        if not order:
            return _callback_fail("订单不存在")

        # 已处理过则直接返回 success，保证虎皮椒重复通知不会重复发码
        if order.get("status") in ("paid", "used") and order.get("license_key"):
            return PlainTextResponse("success")

        # 2. 只处理已支付（OD）状态
        if pay_status != "OD":
            return _callback_fail(f"订单未支付（{pay_status or 'unknown'}）")

        # 3. 金额必须与下单时一致，且原状态必须是 pending（幂等防重）
        try:
            paid_cents = int(round(float(data.get("total_fee", "0")) * 100))
        except (TypeError, ValueError):
            return _callback_fail("金额格式异常")
        if paid_cents != int(order.get("amount", -1)):
            return _callback_fail("支付金额与订单金额不一致")
        if order.get("status") != "pending":
            return _callback_fail(f"订单状态异常（{order.get('status')}）")

        # 4. 复用原私钥签名逻辑发码
        license_key, exp = _issue_license(order, trade_order_id)

        # 5. 更新为已支付并记录授权码
        order.update({
            "status": "paid",
            "license_key": license_key,
            "trade_no": data.get("transaction_id") or data.get("open_order_id") or "",
            "paid_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "exp": exp,
        })
        orders[trade_order_id] = order
        _save("orders.json", "orders", orders)

    # 6. 必须返回纯文本 success，否则虎皮椒会持续重发通知
    return PlainTextResponse("success")


@app.post("/api/payment/check_active")
def payment_check_active(req: PayCheckReq):
    """主动查单补偿：前端轮询用。查到已支付就立即发码，避免回调丢失导致用户付款不到账。"""
    trade_order_id = (req.trade_order_id or "").strip()
    if not trade_order_id:
        raise HTTPException(400, "商户订单号不能为空")

    with _LOCK:
        orders = _load("orders.json", "orders")
        order = orders.get(trade_order_id)
    if not order:
        raise HTTPException(404, "订单不存在")

    # 已发过码，直接返回，避免重复签名
    if order.get("status") in ("paid", "used") and order.get("license_key"):
        return {"ok": True, "status": order.get("status"),
                "license_key": order.get("license_key"), "trade_order_id": trade_order_id}

    # 2. 向虎皮椒主动查单
    params = {
        "appid": XUNHUPAY_APPID,
        "trade_order_id": trade_order_id,
        "time": str(_now()),
        "nonce_str": _nonce(),
    }
    params["hash"] = xunhupay_hash(params)
    try:
        resp = requests.post(XUNHUPAY_QUERY_URL, data=params, timeout=15)
        raw = resp.json()
    except Exception as exc:
        raise HTTPException(502, f"查单请求失败：{exc}")

    if int(raw.get("errcode", -1)) != 0:
        raise HTTPException(502, f"查单失败：{raw.get('errmsg') or raw}")
    # 兼容网关把结果包在 data 里的返回结构
    info = raw.get("data") if isinstance(raw.get("data"), dict) else raw

    # 3. 查到已支付则立即发码（与回调相同的校验：金额一致 + 状态 pending）
    if (info.get("status") or "").strip().upper() == "OD":
        try:
            paid_cents = int(round(float(info.get("total_fee") or 0) * 100))
        except (TypeError, ValueError):
            paid_cents = -1
        with _LOCK:
            orders = _load("orders.json", "orders")
            order = orders.get(trade_order_id)
            if order and order.get("status") == "pending" and paid_cents == int(order.get("amount", -1)):
                license_key, exp = _issue_license(order, trade_order_id)
                order.update({"status": "paid", "license_key": license_key,
                              "trade_no": info.get("transaction_id") or "",
                              "paid_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "exp": exp})
                orders[trade_order_id] = order
                _save("orders.json", "orders", orders)
            if order:
                return {"ok": True, "status": order.get("status"),
                        "license_key": order.get("license_key", ""),
                        "trade_order_id": trade_order_id}

    # 4. 未支付：返回当前状态供前端继续轮询
    return {"ok": True, "status": "pending", "license_key": "",
            "trade_order_id": trade_order_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("CLOUD_PORT", "80")))
