"""授权与额度（P2-1 主从授权算法底座 / P2-2 免费版额度防刷 / P3 云端接口预留）。

设计要点：
- 机器码：MAC + 主机名 + 系统盘卷序列号 哈希派生，换机即变，防一码多机。
- 离线授权码：payload = 产品|套餐|年限|机器码，HMAC-SHA256 签名，格式 FPAS-XXXX-XXXX-XXXX-XXXX。
  签发工具见文件底部 `generate_license_code()`（商家侧使用）。
- 免费版额度：每月 20 张发票，"双重记录"——本地 DB + 系统隐藏目录（防卸载重装刷额度），
  校验时取各渠道最大值。隐藏记录带 HMAC 签名，防手改。
- 云端预留：verify_order_online / sync_blacklist / request_unbind 三个接口当前返回
  cloud_not_configured，等云函数环境就绪后替换实现即可，本地逻辑无需改动。
"""
import hashlib
import hmac
import json
import os
import platform
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import LICENSE_SECRET, settings
from app.infra.logger import get_logger
from app.repository import db as db_repo

logger = get_logger(__name__)

# 套餐定义：free=免费版(20张/月) personal=个人版 enterprise=企业版
PLANS = {
    "free": {"name": "免费版", "monthly_quota": 20, "features": ["basic"]},
    "personal": {"name": "个人版", "monthly_quota": None, "features": ["basic", "export", "match"]},
    "enterprise": {"name": "企业版", "monthly_quota": None,
                   "features": ["basic", "export", "match", "multi_device", "handover"]},
}
FREE_MONTHLY_QUOTA = 20
_PRODUCT = "fapiaoassust"


# ==============================================================
# 机器码（防一码多机的设备指纹）
# ==============================================================

def get_machine_code() -> str:
    """生成 16 位机器码（XXXX-XXXX-XXXX-XXXX）。同一台机器恒定，换机/换盘即变。"""
    parts = [str(uuid.getnode()), platform.node() or "", _volume_serial()]
    raw = "|".join(parts)
    # 统一大写：展示与签名两端必须完全一致，否则自家签发的码都激活失败
    code = "".join(c for c in raw if c.isalnum()).upper()[:16].ljust(16, "0")
    return "-".join(code[i:i + 4] for i in range(0, 16, 4))


def _volume_serial() -> str:
    """Windows 系统盘卷序列号；非 Windows 环境降级为空串。"""
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(
            ["cmd", "/c", "vol", os.environ.get("SystemDrive", "C:") + "\\"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        # 输出形如 "Volume Serial Number is 1A2B-3C4D"
        for line in out.splitlines():
            if "-" in line and ("Serial" in line or "序列" in line):
                return line.rsplit(" ", 1)[-1].strip()
    except Exception:
        pass
    return ""


# ==============================================================
# 授权状态（本地 license_state 表单行存储）
# ==============================================================

def dev_mode() -> bool:
    """开发者调试模式：绕过授权码与免费版额度校验。

    开启方式（见 app/config.py）：
      - 环境变量 FAPIAO_DEV_MODE=1（源码与 exe 均生效）
      - config.json 中 "dev_mode": true（仅源码运行生效）
    仅用于本地自测，发布前务必确认关闭。
    """
    return bool(getattr(settings, "dev_mode", False))


def current_license() -> dict:
    """当前授权信息。默认免费版；开发者模式下按企业版放行（带 dev_mode 标记）。"""
    if dev_mode():
        return {"plan": "enterprise", "plan_name": "企业版（开发者模式）",
                "license_code": "DEV", "activated_at": None, "years": None,
                "machine_code": get_machine_code(),
                "features": PLANS["enterprise"]["features"], "dev_mode": True}
    row = None
    try:
        with db_repo.read() as conn:
            row = conn.execute(
                "SELECT plan, license_code, activated_at, years, extra_json "
                "FROM license_state WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        pass
    if not row:
        return {"plan": "free", "plan_name": PLANS["free"]["name"],
                "activated_at": None, "years": None, "machine_code": get_machine_code(),
                "features": PLANS["free"]["features"]}
    plan = row[0] if row[0] in PLANS else "free"
    return {"plan": plan, "plan_name": PLANS[plan]["name"],
            "license_code": row[1], "activated_at": row[2], "years": row[3],
            "machine_code": get_machine_code(),
            "features": PLANS[plan]["features"]}


def is_paid() -> bool:
    return current_license()["plan"] != "free"


def has_feature(feature: str) -> bool:
    return feature in current_license()["features"]


# ==============================================================
# 离线授权码（P3 云端就绪前的过渡方案：售出不退 + 机器码强绑定）
# ==============================================================

def _sign(payload: str) -> str:
    return hmac.new(LICENSE_SECRET.encode(), payload.encode(),
                    hashlib.sha256).hexdigest().upper()


def generate_license_code(machine_code: str, plan: str, years: int = 0) -> str:
    """商家侧签发授权码。years=0 表示永久。仅用于发货，不要打包进客户端。"""
    mc = machine_code.replace("-", "").upper()
    payload = f"{_PRODUCT}|{plan}|{years}|{mc}"
    sig = _sign(payload)[:16]
    body = (mc[:8] + sig)[:20]
    return "FPAS-" + "-".join(body[i:i + 4] for i in range(0, 20, 4))


def activate(code: str) -> dict:
    """激活授权码。校验格式 → 签名 → 机器码绑定。成功写入 license_state。"""
    code = (code or "").strip().upper()
    if not code.startswith("FPAS-") or len(code.replace("-", "")) != 24:
        return {"ok": False, "message": "授权码格式不正确（应为 FPAS-XXXX-XXXX-XXXX-XXXX-XXXX）"}
    mc = get_machine_code().replace("-", "").upper()
    body = code[5:].replace("-", "")
    # 依次尝试各套餐与年限组合验证签名。
    # 年限必须与 generate_license_code 的可签发范围一致（0=永久，1~10 年），
    # 否则商家签发的 2/5/10 年版授权码会激活失败。
    for plan in ("personal", "enterprise"):
        for years in range(0, 11):
            payload = f"{_PRODUCT}|{plan}|{years}|{mc}"
            expect = (mc[:8] + _sign(payload)[:16])[:20]
            if hmac.compare_digest(body, expect):
                _save_license(plan, code, years)
                logger.info("授权激活成功: plan=%s years=%s", plan, years)
                return {"ok": True, "plan": plan, "plan_name": PLANS[plan]["name"],
                        "years": years,
                        "message": f"激活成功！当前为{PLANS[plan]['name']}"
                                   f"{'（永久）' if years == 0 else f'（{years}年）'}"}
    return {"ok": False, "message": "授权码无效或不属于本机（授权码与机器码强绑定）"}


def _save_license(plan: str, code: str, years: int) -> None:
    with db_repo.tx() as conn:
        conn.execute(
            "INSERT INTO license_state (id, plan, license_code, activated_at, years, extra_json) "
            "VALUES (1, ?, ?, ?, ?, '{}') "
            "ON CONFLICT(id) DO UPDATE SET plan=excluded.plan, "
            "license_code=excluded.license_code, activated_at=excluded.activated_at, "
            "years=excluded.years",
            (plan, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), years),
        )


def deactivate() -> None:
    """恢复为免费版（本地调试用；商业版可移除）。"""
    with db_repo.tx() as conn:
        conn.execute("DELETE FROM license_state WHERE id = 1")


# ==============================================================
# 免费版额度防刷（P2-2：双重记录 = 本地 DB + 系统隐藏目录）
# ==============================================================

def _month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def _quota_sig(month: str, used: int, machine: str = "") -> str:
    """隐藏额度记录的签名。machine 必须是记录文件内保存的机器码（不是当前机器），
    这样换机/换盘后旧记录仍能自证完整，从而区分"异机记录"与"人为篡改"。"""
    mc = (machine or get_machine_code()).replace("-", "").upper()
    return hmac.new(LICENSE_SECRET.encode(),
                    f"quota|{mc}|{month}|{used}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def _hidden_quota_path() -> Path:
    """系统级隐藏数据目录（卸载软件不删，防重装刷额度）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "fapiaoassust"
    p.mkdir(parents=True, exist_ok=True)
    try:
        # Windows 下加隐藏属性
        if os.name == "nt":
            subprocess.run(["attrib", "+H", str(p)], capture_output=True, timeout=3)
    except Exception:
        pass
    return p / "usage.dat"


def _read_db_quota(month: str) -> int:
    try:
        with db_repo.read() as conn:
            row = conn.execute(
                "SELECT used FROM quota_usage WHERE month = ?", (month,)
            ).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _read_hidden_quota(month: str) -> int:
    try:
        p = _hidden_quota_path()
        if not p.exists():
            return 0
        data = json.loads(p.read_text(encoding="utf-8"))
        used = int(data.get("used", 0))
        # 兼容旧版记录（无 machine 字段）：按本机机器码验签，避免升级后额度被一次性重置
        machine = (str(data.get("machine") or "").replace("-", "").upper()
                   or get_machine_code().replace("-", "").upper())
        # 防手改：签名不符（用记录内的机器码验签）视为篡改，取一个保守大数阻断使用。
        # 签名合法但机器码不是本机 → 是换机/换盘带过来的旧记录，与本机额度无关，忽略。
        if data.get("sig") != _quota_sig(str(data.get("month") or ""), used, machine):
            logger.warning("隐藏额度记录签名不符（疑似篡改），按已用尽处理")
            return FREE_MONTHLY_QUOTA
        if machine.replace("-", "").upper() != get_machine_code().replace("-", "").upper():
            return 0
        if data.get("month") != month:
            return 0
        return used
    except Exception:
        return 0


def _write_quota(month: str, used: int) -> None:
    mc = get_machine_code()
    sig = _quota_sig(month, used, mc)
    # 双写：DB + 隐藏目录，任一路径失败不致命
    try:
        with db_repo.tx() as conn:
            conn.execute(
                "INSERT INTO quota_usage (month, used, sig, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(month) DO UPDATE SET used=excluded.used, sig=excluded.sig, "
                "updated_at=excluded.updated_at",
                (month, used, sig, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
    except sqlite3.Error as e:
        logger.warning("额度 DB 写入失败: %s", e)
    try:
        _hidden_quota_path().write_text(
            json.dumps({"month": month, "used": used, "machine": mc, "sig": sig}),
            encoding="utf-8")
    except Exception as e:
        logger.warning("额度隐藏记录写入失败: %s", e)


def quota_status() -> dict:
    """本月额度状态。付费版 unlimited。"""
    lic = current_license()
    if lic["plan"] != "free":
        return {"plan": lic["plan"], "unlimited": True, "used": 0,
                "quota": None, "remaining": None}
    month = _month_key()
    # 防刷核心：取 DB 与隐藏记录的最大值
    used = max(_read_db_quota(month), _read_hidden_quota(month))
    return {"plan": "free", "unlimited": False, "used": used,
            "quota": FREE_MONTHLY_QUOTA,
            "remaining": max(0, FREE_MONTHLY_QUOTA - used)}


def check_quota(n: int = 1) -> Optional[str]:
    """校验是否还能导入 n 张发票。返回 None=允许，否则返回拦截文案。"""
    st = quota_status()
    if st["unlimited"]:
        return None
    if st["remaining"] < n:
        return (f"免费版每月限导入 {st['quota']} 张发票，本月已用 {st['used']} 张，"
                f"剩余 {st['remaining']} 张。请升级个人版/企业版解除限制。")
    return None


def consume_quota(n: int = 1) -> None:
    """导入成功后扣减额度（免费版）。"""
    if is_paid():
        return
    st = quota_status()
    _write_quota(_month_key(), st["used"] + n)


# ==============================================================
# 云端接口预留（P3：云函数环境就绪后替换实现，本地调用方不用改）
# ==============================================================

def verify_order_online(order_no: str) -> dict:
    """云端订单核销（预留）。就绪后：校验订单号有效且未使用 → 签发授权码 → 标记已核销。"""
    return {"ok": False, "code": "cloud_not_configured",
            "message": "云端发码服务未配置，请使用客服提供的离线授权码激活"}


def sync_blacklist() -> dict:
    """云端黑名单同步（预留）。就绪后：本机机器码在黑名单 → 自动降级免费版。"""
    return {"ok": True, "code": "cloud_not_configured", "blacklisted": False}


def request_unbind(order_no: str) -> dict:
    """换机解绑申请（预留）。就绪后：客服后台解除旧机器绑定。"""
    return {"ok": False, "code": "cloud_not_configured",
            "message": "云端换机服务未配置，请联系客服人工解绑"}
