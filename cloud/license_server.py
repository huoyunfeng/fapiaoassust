"""云端授权发码服务（部署在公网服务器，本机客户端只持有公钥）。

启动：
    pip install fastapi uvicorn cryptography
    python cloud/license_server.py          # 默认 0.0.0.0:8000
    uvicorn cloud.license_server:app --host 0.0.0.0 --port 8000

数据文件（与脚本同目录，可用 CLOUD_DATA_DIR 覆盖）：
    orders.json    订单：{订单号: {plan, days, status, machine_code, ...}}
    devices.json   子设备绑定：{子机器码: {parent, plan, exp, order_id}}
    blacklist.json 黑名单：{机器码: {reason, at}}

安全提示：私钥不写进代码，通过环境变量 CLOUD_PRIVATE_KEY_PEM 或文件 private_key.pem
提供（可用 CLOUD_PRIVATE_KEY_FILE 指定路径）；私钥只保存在服务器本地，禁止提交仓库。
"""
import json
import os
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException, Request
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

    payload = {
        "machine_code": machine, "plan": plan, "role": "master",
        "exp": activated_at + days * 86400,
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
            "reason": hit.get("reason", "") if hit else ""}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("CLOUD_PORT", "8000")))
