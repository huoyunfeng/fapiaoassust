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
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlrequest

try:                       # 云端部署已装 requests；本地只跑单元测试时退回标准库
    import requests
except ImportError:        # pragma: no cover
    requests = None
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
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
# 订单库并发锁：所有 orders.json 的「读-改-写」都必须在这把锁内完成，
# 否则多个客户同时支付时可能互相覆盖、甚至写坏文件。
# 注意使用不可重入的 Lock，因此禁止在持锁期间再次调用会加锁的函数。
_LOCK = threading.Lock()

# 企业版共 3 台设备 = 主设备 1 台 + 子设备 2 台（此处只填「子设备」名额）
# 注意：修改后需重新部署授权服务，仅影响新签发/续费的授权码
PLAN_DEVICES = {"personal": 0, "enterprise": 2}
PLAN_DAYS = {"personal": 365, "enterprise": 365}

# ---------------------------------------------------------------- 支付配置（虎皮椒聚合支付）
XUNHUPAY_APPID = "201906189707"
# 商户密钥只从环境变量读取，严禁写进仓库；未配置时支付相关接口会明确报错
XUNHUPAY_APPSECRET = (os.getenv("XUNHUPAY_APPSECRET") or "").strip()
NOTIFY_URL = "http://182.92.198.99/api/payment/callback"

XUNHUPAY_CREATE_URL = "https://api.xunhupay.com/payment/do.html"     # 下单
XUNHUPAY_QUERY_URL = "https://api.xunhupay.com/payment/query.html"   # 主动查单

# 套餐价格（单位：分）—— 后端写死，前端传什么都不会影响实际扣款
# 联调期 personal 为 1 分钱；正式上线改为 9900（99 元）
PLAN_PRICES = {"personal": 1, "enterprise": 49900}

# 同一机器 + 同一套餐的 pending 二维码复用时长：前端倒计时 5 分钟，此处保持一致，
# 重复点击下单直接返回旧二维码，既有真实码也避免刷单刷爆网关。
PENDING_REUSE_SECONDS = 300

# 对账兜底：pending 超过 10 分钟即视为"回调可能丢失"，每 10 分钟主动查单一次
PAYMENT_PENDING_TIMEOUT = 600
PAYMENT_RECHECK_INTERVAL = 600
# 支付标题（展示在收银台）
PLAN_TITLES = {"personal": "发票助手 个人版（1年）",
               "enterprise": "发票助手 企业版（1年）"}


def _appsecret() -> str:
    """取商户密钥；未配置环境变量时抛出明确异常，避免用空密钥签名后静默失败。"""
    if not XUNHUPAY_APPSECRET:
        raise RuntimeError("云端未配置 XUNHUPAY_APPSECRET 环境变量，支付功能不可用")
    return XUNHUPAY_APPSECRET


def xunhupay_hash(params: dict) -> str:
    """生成虎皮椒签名：剔除空值与 hash 字段，按参数名 ASCII 升序拼成 k1=v1&k2=v2，
    末尾直接拼接 AppSecret，做 MD5 后返回小写。"""
    items = [(k, v) for k, v in params.items()
             if k != "hash" and v is not None and str(v) != ""]
    items.sort(key=lambda kv: str(kv[0]))
    raw = "&".join(f"{k}={v}" for k, v in items) + _appsecret()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _nonce() -> str:
    return os.urandom(8).hex()


def _http_post_form(url: str, data: dict) -> dict:
    """向虎皮椒 POST 表单并解析 JSON，返回 {errcode, ...}。

    优先用 requests；未安装时退回标准库 urllib，保证云端与本地测试行为一致。
    """
    if requests is not None:
        return requests.post(url, data=data, timeout=15).json()
    body = urlparse.urlencode(data).encode("utf-8")
    req = urlrequest.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlrequest.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    """原子写入：先写临时文件再 os.replace，避免并发/断电写坏订单库。

    调用方必须已持有 _LOCK（本模块所有 orders.json 的读改写都在锁内完成）。
    """
    path = _path(name)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({key: data}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)      # 同分区替换是原子的，读者要么看到旧文件要么看到新文件


def _now() -> int:
    return int(time.time())


def _is_blacklisted(machine: str) -> dict:
    return _load("blacklist.json", "blacklist").get(machine) or {}


logger = logging.getLogger("fapiao.license")
_STOP = threading.Event()


@asynccontextmanager
async def _lifespan(_app):
    """启动后台对账线程：定时扫描超时未到账订单，防止回调丢失导致付款不到账。"""
    _STOP.clear()
    t = threading.Thread(target=_reconcile_loop, name="pay-reconcile", daemon=True)
    t.start()
    logger.info("支付对账线程已启动：每 %s 秒扫描一次超时未支付订单",
                PAYMENT_RECHECK_INTERVAL)
    try:
        yield
    finally:
        _STOP.set()


app = FastAPI(title="发票助手云端授权服务", version="1.1", lifespan=_lifespan)

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


LANDING_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>发票报销核对助手</title>
<style>
  :root{
    --bg:#0a1120; --panel:#111b2e; --line:rgba(255,255,255,.08);
    --text:#e9effb; --muted:#8fa2c2; --brand:#3b82f6; --cyan:#22d3ee;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    color:var(--text);min-height:100vh;padding:72px 20px 48px;
    background:
      radial-gradient(900px 520px at 50% -12%, rgba(59,130,246,.28) 0%, transparent 62%),
      radial-gradient(700px 420px at 88% 8%, rgba(34,211,238,.16) 0%, transparent 60%),
      var(--bg);
    display:flex;flex-direction:column;align-items:center;
  }
  .wrap{width:100%;max-width:1040px;display:flex;flex-direction:column;align-items:center}
  .badge{
    display:inline-flex;align-items:center;gap:8px;padding:7px 16px;border-radius:999px;
    border:1px solid var(--line);background:rgba(255,255,255,.04);
    color:var(--muted);font-size:13px;letter-spacing:.4px;
  }
  .badge i{width:7px;height:7px;border-radius:50%;background:#22c55e;box-shadow:0 0 10px #22c55e}
  h1{
    margin:26px 0 14px;font-size:clamp(30px,5vw,50px);line-height:1.2;letter-spacing:-.5px;
    text-align:center;font-weight:700;
    background:linear-gradient(120deg,#ffffff 10%,#a9c8ff 55%,#6ee7f9 95%);
    -webkit-background-clip:text;background-clip:text;color:transparent;
  }
  .subtitle{font-size:clamp(15px,2.2vw,19px);color:#c7d5ee;text-align:center}
  .desc{
    margin-top:16px;max-width:640px;text-align:center;color:var(--muted);
    font-size:14.5px;line-height:1.9;
  }
  .grid{
    margin-top:52px;width:100%;display:grid;gap:20px;
    grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  }
  .card{
    position:relative;padding:30px 26px 28px;border-radius:18px;
    border:1px solid var(--line);
    background:linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.015));
    transition:transform .25s ease, border-color .25s ease, box-shadow .25s ease;
  }
  .card:hover{transform:translateY(-5px);border-color:rgba(96,165,250,.45);
    box-shadow:0 18px 40px rgba(2,8,20,.55)}
  .ico{
    width:46px;height:46px;border-radius:13px;display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg, rgba(59,130,246,.28), rgba(34,211,238,.18));
    border:1px solid rgba(96,165,250,.28);margin-bottom:18px;
  }
  .ico svg{width:23px;height:23px;stroke:#9ec5ff;fill:none;stroke-width:1.7;
    stroke-linecap:round;stroke-linejoin:round}
  .card h3{font-size:17.5px;margin-bottom:9px;font-weight:600;letter-spacing:.2px}
  .card p{font-size:13.8px;color:var(--muted);line-height:1.85}
  footer{
    margin-top:64px;width:100%;max-width:1040px;padding-top:28px;
    border-top:1px solid var(--line);
    display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;
    color:var(--muted);font-size:13px;
  }
  .wx{
    display:inline-flex;align-items:center;gap:9px;padding:9px 16px;border-radius:10px;
    border:1px solid rgba(34,197,94,.35);background:rgba(34,197,94,.08);color:#b9f6c9;
    font-size:13.5px;letter-spacing:.4px;
  }
  .wx b{color:#fff;font-weight:600;letter-spacing:.6px}
  @media (max-width:520px){
    body{padding-top:52px}
    .grid{margin-top:38px;gap:14px}
    footer{margin-top:44px;justify-content:center;text-align:center}
  }
</style>
</head>
<body>
<div class="wrap">
  <span class="badge"><i></i>本地部署 · 数据不出本机</span>
  <h1>发票报销核对助手</h1>
  <p class="subtitle">纯本地部署，保障数据绝对安全</p>
  <p class="desc">
    自动识别发票、智能匹配报销单，一键核对差异。全部计算在本机完成，
    不上传、不留存，让财务核对又快又安心。
  </p>

  <div class="grid">
    <div class="card">
      <div class="ico">
        <svg viewBox="0 0 24 24"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>
        <path d="M9.5 12h5"/><path d="M9.5 16h5"/></svg>
      </div>
      <h3>多格式解析</h3>
      <p>支持 PDF、图片、Excel 等常见票据格式，自动提取金额、税额、发票号与开票日期等关键字段。</p>
    </div>

    <div class="card">
      <div class="ico">
        <svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="3.2"/><circle cx="16.5" cy="15.5" r="3.2"/>
        <path d="M10.4 10.2l3.7 3.1"/></svg>
      </div>
      <h3>智能匹配</h3>
      <p>按金额、日期、销方等维度自动配对发票与报销单，模糊项标注待确认，异常与重复一目了然。</p>
    </div>

    <div class="card">
      <div class="ico">
        <svg viewBox="0 0 24 24"><path d="M12 3l7 3v5.5c0 4.4-2.9 7.9-7 9.5-4.1-1.6-7-5.1-7-9.5V6z"/>
        <path d="M9 12l2 2 4-4"/></svg>
      </div>
      <h3>绝对安全</h3>
      <p>全流程本地运算，数据不经过任何云端；授权离线校验，断网也能正常工作。</p>
    </div>
  </div>

  <footer>
    <span>© 发票报销核对助手 · 本地版</span>
    <span class="wx">客服微信 <b>fengyunhuo123</b></span>
  </footer>
</div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def homepage():
    """官网主页：仅用于支付平台审核与品牌门面展示，不涉及任何授权/订单逻辑。"""
    return HTMLResponse(LANDING_HTML)


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
    """支付下单：校验套餐 → 复用未过期待支付二维码 → 请求虎皮椒拿码 → 订单记 pending。"""
    plan = (req.plan_type or "").strip().lower()
    machine = (req.machine_code or "").strip()
    if plan not in PLAN_PRICES:
        raise HTTPException(400, "套餐类型不存在，可选：personal / enterprise")
    if not machine:
        raise HTTPException(400, "机器码不能为空")
    if _is_blacklisted(machine):
        raise HTTPException(403, "该设备已被列入黑名单")

    price = PLAN_PRICES[plan]                       # 单位：分，后端写死
    total_fee = f"{price / 100:.2f}"                # 虎皮椒要求「元」字符串
    # 商户唯一订单号：FP + 时间戳 + 8 位随机数（防枚举猜测）
    trade_order_id = f"FP{_now()}{os.urandom(4).hex().upper()}"

    # 1. 先查是否有可复用的 pending 订单：有则直接返回原二维码，既防刷单也不重复扣款
    with _LOCK:
        orders = _load("orders.json", "orders")
        hit = _find_reusable_pending(orders, machine, plan)
        if hit:
            old_id, old = hit
            return {"ok": True, "reused": True, "trade_order_id": old_id,
                    "url_qrcode": old.get("url_qrcode", ""),
                    "url": old.get("url", ""), "plan": plan,
                    "amount": old.get("amount", price) / 100,
                    "amount_fen": int(old.get("amount", price)),
                    "expire_in": max(0, PENDING_REUSE_SECONDS - _order_age(old))}
        # 2. 先落一条 creating 占位单，防止并发请求重复向网关下单
        orders[trade_order_id] = {
            "plan": plan,
            "days": PLAN_DAYS.get(plan, 365),
            "status": "creating",
            "machine_code": machine,
            "amount": price,                        # 分，便于回调金额比对
            "trade_order_id": trade_order_id,
            "trade_no": "",
            "license_key": "",
            "url_qrcode": "",
            "url": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "paid_at": "",
        }
        _save("orders.json", "orders", orders)

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

    # 3. 请求网关（网络调用不持锁）；任何失败都要把占位单清掉，避免永久卡住
    try:
        params["hash"] = xunhupay_hash(params)
        data = _http_post_form(XUNHUPAY_CREATE_URL, params)
        if int(data.get("errcode", -1)) != 0:
            raise RuntimeError(data.get("errmsg") or str(data))
        url_qrcode = data.get("url_qrcode") or data.get("url") or ""
        if not url_qrcode:
            raise RuntimeError(f"未返回支付二维码 {data}")
    except Exception as exc:  # 网络异常 / 网关返回异常 / 密钥未配置
        with _LOCK:
            orders = _load("orders.json", "orders")
            orders.pop(trade_order_id, None)
            _save("orders.json", "orders", orders)
        raise HTTPException(502, f"下单失败：{exc}")

    # 4. 回填二维码并转为待支付（若回调已抢先到账则保留 paid，不回退状态）
    with _LOCK:
        orders = _load("orders.json", "orders")
        order = orders.get(trade_order_id) or {}
        order["url_qrcode"] = url_qrcode
        order["url"] = data.get("url", "")
        if order.get("status") == "creating":
            order["status"] = "pending"
        orders[trade_order_id] = order
        _save("orders.json", "orders", orders)

    return {"ok": True, "reused": False, "trade_order_id": trade_order_id,
            "url_qrcode": url_qrcode, "url": data.get("url", ""),
            "plan": plan, "amount": price / 100, "amount_fen": price,
            "expire_in": PENDING_REUSE_SECONDS}


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

    # 2. 只读检查（持锁时间尽量短，网络/签名等耗时操作不持锁）
    with _LOCK:
        order = _load("orders.json", "orders").get(trade_order_id)
    if not order:
        return _callback_fail("订单不存在")

    # 已处理过则直接返回 success，保证虎皮椒重复通知不会重复发码（幂等）
    if order.get("status") in ("paid", "used") and order.get("license_key"):
        return PlainTextResponse("success")

    # 3. 只处理已支付（OD）状态
    if pay_status != "OD":
        return _callback_fail(f"订单未支付（{pay_status or 'unknown'}）")

    # 4. 金额校验 + 发码 + 落库统一交给 _finalize_paid（内部自行加锁）
    try:
        _finalize_paid(trade_order_id, data)
    except HTTPException as exc:
        return _callback_fail(str(exc.detail))

    # 5. 必须返回纯文本 success，否则虎皮椒会持续重发通知
    return PlainTextResponse("success")


def _query_gateway(trade_order_id: str) -> dict:
    """向虎皮椒主动查单，返回统一结构的订单信息（失败抛 HTTPException 502）。"""
    params = {
        "appid": XUNHUPAY_APPID,
        # 注意：查单接口的商户订单号字段是 out_trade_order（下单接口才叫 trade_order_id），
        # 传错会得到 "Invalid open_order_id or out_trade_order!"
        "out_trade_order": trade_order_id,
        "time": str(_now()),
        "nonce_str": _nonce(),
    }
    params["hash"] = xunhupay_hash(params)
    try:
        raw = _http_post_form(XUNHUPAY_QUERY_URL, params)
    except Exception as exc:
        raise HTTPException(502, f"查单请求失败：{exc}")
    if int(raw.get("errcode", -1)) != 0:
        raise HTTPException(502, f"查单失败：{raw.get('errmsg') or raw}")
    # 兼容网关把结果包在 data 里的返回结构
    return raw.get("data") if isinstance(raw.get("data"), dict) else raw


def _finalize_paid(trade_order_id: str, info: dict) -> dict:
    """已确认支付的订单 → 发码 → 落库（幂等：已发过码直接返回原订单）。"""
    with _LOCK:
        orders = _load("orders.json", "orders")
        order = orders.get(trade_order_id)
        if not order:
            raise HTTPException(404, "订单不存在")
        if order.get("status") in ("paid", "used") and order.get("license_key"):
            return order                     # 幂等：重复回调/轮询不重复发码
        try:
            paid_cents = int(round(float(info.get("total_fee") or 0) * 100))
        except (TypeError, ValueError):
            paid_cents = -1
        if paid_cents != int(order.get("amount", -1)):
            raise HTTPException(400, "支付金额与订单金额不一致")
        if order.get("status") not in ("pending", "creating"):
            raise HTTPException(400, f"订单状态异常（{order.get('status')}）")
        license_key, exp = _issue_license(order, trade_order_id)
        order.update({
            "status": "paid", "license_key": license_key,
            "trade_no": info.get("transaction_id") or info.get("open_order_id") or "",
            "paid_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "exp": exp,
        })
        orders[trade_order_id] = order
        _save("orders.json", "orders", orders)
        return order


def _created_ts(order: dict) -> int:
    """订单创建时间戳（秒）；解析失败返回 0。"""
    try:
        return int(time.mktime(
            time.strptime(order.get("created_at", ""), "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return 0


def _order_age(order: dict) -> int:
    """订单创建至今的秒数；无时间戳的脏数据视为"很老"，交给对账逻辑去查单确认。"""
    ts = _created_ts(order)
    return (_now() - ts) if ts else 10 ** 9


def _find_reusable_pending(orders: dict, machine: str, plan: str):
    """同机器 + 同套餐 + 仍在有效期内的 pending 订单 → 复用其二维码（防刷单）。"""
    for oid, order in orders.items():
        if (order.get("status") == "pending"
                and (order.get("machine_code") or "").strip() == machine
                and order.get("plan") == plan
                and order.get("url_qrcode")
                and _order_age(order) < PENDING_REUSE_SECONDS):
            return oid, order
    return None


def _reconcile_pending() -> int:
    """对账兜底：扫描超过 10 分钟仍 pending 的订单，主动查单并静默补发授权码。

    覆盖回调丢失、服务器重启等极端情况 —— 只要用户真的付了钱，最终一定能拿到授权。
    返回本次补发成功的订单数。
    """
    with _LOCK:
        orders = _load("orders.json", "orders")
    stale = [oid for oid, o in orders.items()
             if o.get("status") == "pending" and _order_age(o) >= PAYMENT_PENDING_TIMEOUT]
    issued = 0
    for oid in stale:
        try:
            info = _query_gateway(oid)
            if (info.get("status") or "").strip().upper() != "OD":
                continue
            _finalize_paid(oid, info)
            issued += 1
            logger.info("对账补发成功：%s", oid)
        except HTTPException as exc:
            logger.warning("对账查单失败 %s：%s", oid, exc.detail)
        except Exception as exc:                       # 单笔失败不影响其余订单
            logger.warning("对账异常 %s：%s", oid, exc)
    return issued


def _reconcile_loop() -> None:
    while not _STOP.is_set():
        try:
            n = _reconcile_pending()
            if n:
                logger.info("本轮对账补发 %d 笔", n)
        except Exception as exc:
            logger.warning("对账线程异常：%s", exc)
        _STOP.wait(PAYMENT_RECHECK_INTERVAL)


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

    info = _query_gateway(trade_order_id)
    if (info.get("status") or "").strip().upper() == "OD":
        order = _finalize_paid(trade_order_id, info)
        return {"ok": True, "status": order.get("status"),
                "license_key": order.get("license_key", ""),
                "trade_order_id": trade_order_id}

    # 未支付：返回当前状态供前端继续轮询
    return {"ok": True, "status": "pending", "license_key": "",
            "trade_order_id": trade_order_id}


class PayRecoverReq(BaseModel):
    order_suffix: str
    machine_code: str


@app.post("/api/payment/recover")
def payment_recover(req: PayRecoverReq, request: Request):
    """自助找回：凭订单号后 6 位 + 本机机器码取回已购授权码（付款后换机/关页均可找回）。"""
    _rate_guard(request.client.host if request.client else "unknown")
    suffix = (req.order_suffix or "").strip().upper()
    machine = (req.machine_code or "").strip()
    if len(suffix) < 6:
        raise HTTPException(400, "请输入订单号后 6 位")
    if not machine:
        raise HTTPException(400, "机器码不能为空")
    if _is_blacklisted(machine):
        raise HTTPException(403, "该设备已被列入黑名单")

    with _LOCK:
        orders = _load("orders.json", "orders")
    matches = [oid for oid in orders if oid.upper().endswith(suffix)]
    if not matches:
        raise HTTPException(404, "未找到该订单，请核对订单号后 6 位")
    if len(matches) > 1:
        raise HTTPException(409, "匹配到多个订单，请输入更多位数或联系客服")

    oid = matches[0]
    order = orders[oid]
    status = (order.get("status") or "").lower()

    # 未到账订单先主动查一次（回调可能刚丢失），仍未支付则明确告知
    if status == "pending":
        try:
            info = _query_gateway(oid)
            if (info.get("status") or "").strip().upper() == "OD":
                order = _finalize_paid(oid, info)
                status = "paid"
        except HTTPException as exc:
            logger.warning("找回时查单失败 %s：%s", oid, exc.detail)
    if status in ("pending", "unused"):
        raise HTTPException(403, "该订单尚未支付，请完成支付后再找回")
    if status == "refunded":
        raise HTTPException(403, "该订单已退款，无法激活")

    bound = (order.get("machine_code") or "").strip()
    if bound and bound != machine:
        raise HTTPException(409, "该订单已绑定其他设备，请联系客服换机")
    code = order.get("license_key") or ""
    if not code:
        # 历史手工订单没有预存授权码，现场签发一次并回写
        code, exp = _issue_license(order, oid)
        with _LOCK:
            orders = _load("orders.json", "orders")
            cur = orders.get(oid) or order
            cur.update({"license_key": code, "exp": exp,
                        "machine_code": bound or machine})
            orders[oid] = cur
            _save("orders.json", "orders", orders)
    return {"ok": True, "trade_order_id": oid, "license_key": code,
            "plan": order.get("plan"), "message": "授权已找回"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("CLOUD_PORT", "80")))
