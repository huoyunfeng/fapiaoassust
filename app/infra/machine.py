"""机器码与本地隐藏登记簿。

- 机器码：只读（用户不可修改），由硬件/系统指纹派生，首次生成后落盘复用。
- 核心硬件指纹 core_fp：只取主板/CPU 等不易更换的硬件，用于“换网卡/硬盘”宽容度。
- 隐藏登记簿：免费体验额度与时间锚点防刷（删除数据库重装也无法清零）。
"""
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from app.config import LICENSE_SECRET

# 隐藏登记簿是“整文件读改写”：额度扣减、时间锚点刷新必须共用同一把锁，
# 否则并发请求会互相覆盖（额度丢更新 / 锚点被旧值盖回）。
GUARD_LOCK = threading.RLock()


def registry_dir() -> Path:
    """系统级隐藏目录（不进用户数据目录，删库不影响）。"""
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "FapiaoAssist"


def _machine_guid() -> str:
    """Windows 安装唯一标识（重装系统才会变化）。"""
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Cryptography") as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except Exception:
            pass
    return ""


def _fingerprint() -> str:
    return "|".join([
        _machine_guid(),
        platform.node(),
        platform.machine(),
        platform.processor(),
        str(uuid.getnode()),
    ])


def machine_code() -> str:
    """16 位机器码（XXXX-XXXX-XXXX-XXXX），首次生成后缓存复用。"""
    cache = registry_dir() / "machine"
    try:
        code = cache.read_text(encoding="utf-8").strip()
        if len(code) >= 16:
            return code
    except Exception:
        pass
    digest = hashlib.sha256(_fingerprint().encode("utf-8")).hexdigest().upper()[:16]
    code = "-".join(digest[i:i + 4] for i in range(0, 16, 4))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(code, encoding="utf-8")
    except Exception:
        pass
    return code


# ---------------------------------------------------------------- 核心硬件指纹
_CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


def _run_quiet(cmd: list) -> str:
    """执行系统命令取硬件信息；失败/超时/不存在都返回空串（绝不抛异常）。"""
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
        )
        if out.returncode != 0:
            return ""
        text = out.stdout.decode("utf-8", "ignore") or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # wmic 输出首行是表头，取第二行真实值
        return lines[1] if len(lines) >= 2 else ""
    except Exception:
        return ""


def _core_hardware_raw() -> str:
    """主板序列号 + BIOS UUID + CPU ID：换网卡/硬盘不会变，换主板或 CPU 才会变。"""
    if not sys.platform.startswith("win"):
        return ""
    parts = [
        _run_quiet(["wmic", "csproduct", "get", "uuid"]),
        _run_quiet(["wmic", "baseboard", "get", "serialnumber"]),
        _run_quiet(["wmic", "cpu", "get", "processorid"]),
    ]
    # Win11 24H2 起 wmic 可能被移除：退回 PowerShell CIM 取 UUID
    if not any(parts):
        ps = ("(Get-CimInstance Win32_ComputerSystemProduct).UUID")
        parts.append(_run_quiet(["powershell", "-NoProfile", "-Command", ps]))
    raw = "|".join(p for p in parts if p)
    return raw


def core_fp() -> str:
    """核心硬件指纹（16 位）；取不到硬件信息时返回空串（此时只允许完全匹配）。"""
    cache = registry_dir() / "corefp"
    try:
        code = cache.read_text(encoding="utf-8").strip()
        if len(code) >= 16:
            return code
    except Exception:
        pass
    raw = _core_hardware_raw()
    if not raw:
        return ""
    digest = hashlib.sha256(("core|" + raw).encode("utf-8")).hexdigest().upper()[:16]
    code = "-".join(digest[i:i + 4] for i in range(0, 16, 4))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(code, encoding="utf-8")
    except Exception:
        pass
    return code


# ---------------------------------------------------------------- 注册表加密
def _reg_key() -> bytes:
    return hashlib.sha256((LICENSE_SECRET + "|fapiao-reg-v1").encode("utf-8")).digest()


def seal(text: str) -> str:
    """AES-GCM 加密后再写注册表/隐藏文件：防止用户用 regedit 直接改成合法小数。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(_reg_key()).encrypt(nonce, text.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def unseal(blob: str):
    """解密；任何失败返回 None（视为数据被篡改或来自旧版本）。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        return AESGCM(_reg_key()).decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except Exception:
        return None


def guard_file() -> Path:
    return registry_dir() / "guard.json"


# 额度物理防重置：除隐藏登记簿外，再把「机器码+已用张数」加密写入注册表，
# 重装软件 / 删除数据库 / 清理 AppData 都无法清零。
_REG_SUBKEY = r"Software\FapiaoAssistant"
# 防时间回拨锚点也写注册表：只删 guard.json / 清 AppData 无法把「最后运行时间」回退
_REG_VALUE_LAST = "last_run"
# 免费体验累计已用张数：与机器码一起加密后写入；删库/清 AppData/改注册表都无法洗白
_REG_VALUE_TRIAL = "trial"
# 体验额度密文缓存：值未变化就复用上次密文，避免每次状态判定都做一次 AES + 注册表写
_REG_TRIAL_LAST = {"blob": ""}
_TRIAL_CACHE = {"value": None, "blob": "", "m": ""}


def _registry_enabled() -> bool:
    """只有使用默认登记簿目录（真实运行）时才读写注册表。

    测试会 monkeypatch registry_dir 到临时目录，此时自动跳过注册表，
    避免污染当前用户的真实额度记录。
    """
    if not sys.platform.startswith("win"):
        return False
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    if not base:
        return False
    try:
        return Path(registry_dir()).resolve() == (Path(base) / "FapiaoAssist").resolve()
    except Exception:
        return False


def _registry_last_run() -> int:
    if not _registry_enabled():
        return 0
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            raw, _type = winreg.QueryValueEx(key, _REG_VALUE_LAST)
        return int(raw)
    except Exception:
        return 0


def _save_registry_last_run(ts: int) -> None:
    """同步防回拨锚点到注册表。

    正常前进时 5 分钟内最多写一次（避免频繁写注册表）；
    **下调（服务器时间校正）必须立即落盘**，否则注册表里的未来时间
    会在下次 load_guard 时把基准重新污染成“未来”，导致永久误判回拨。
    """
    if not _registry_enabled() or int(ts) <= 0:
        return
    cur = _registry_last_run()
    if cur and 0 <= int(ts) - cur < 300:
        return
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            winreg.SetValueEx(key, _REG_VALUE_LAST, 0, winreg.REG_SZ, str(int(ts)))
    except Exception:
        pass


def _registry_trial() -> int:
    """读注册表里加密的体验已用张数；解密失败按 0 处理（不阻断开箱即用）。"""
    if not _registry_enabled():
        return 0
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            raw, _type = winreg.QueryValueEx(key, _REG_VALUE_TRIAL)
        return _decode_trial(raw)
    except Exception:
        return 0


def _save_registry_trial(sealed: str) -> None:
    if not _registry_enabled() or not sealed:
        return
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            winreg.SetValueEx(key, _REG_VALUE_TRIAL, 0, winreg.REG_SZ, sealed)
    except Exception:
        pass


def load_guard() -> dict:
    try:
        data = json.loads(guard_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    # 合并注册表防回拨锚点：删文件也无法让系统时间「回到过去」
    last = _registry_last_run()
    if last > int(data.get("last_run_at", 0) or 0):
        data["last_run_at"] = last
    # 合并体验额度：文件与注册表都解密后取最大值；任一被篡改都不影响判定
    data["trial_used"] = max(_decode_trial(data.get("trial")), _registry_trial())
    return data


def _seal_trial(n: int) -> str:
    """同一数值复用上次密文：额度只在真正消耗/回退时才重新加密并写注册表。"""
    if _TRIAL_CACHE["value"] == n and _TRIAL_CACHE["blob"] \
            and _TRIAL_CACHE["m"] == machine_code():
        return _TRIAL_CACHE["blob"]
    body = json.dumps({"m": machine_code(), "n": int(n)}, ensure_ascii=False)
    blob = seal(body)
    _TRIAL_CACHE.update({"value": int(n), "blob": blob, "m": machine_code()})
    return blob


def _decode_trial(blob) -> int:
    """解密额度密文；只采信本机机器码写入的记录。

    兼容早期“纯数字”密文（升级前已落盘的数据），避免老用户额度被重置。
    """
    if not blob:
        return 0
    raw = unseal(blob) if isinstance(blob, str) else None
    if not raw:
        return 0
    text = str(raw).strip()
    try:
        if text.startswith("{"):
            obj = json.loads(text)
            if str(obj.get("m", "")) != machine_code():
                return 0  # 非本机写入（拷贝注册表 / 换机），不采信
            return max(0, int(obj.get("n", 0)))
        return max(0, int(text))
    except Exception:
        return 0


def save_guard(data: dict) -> None:
    payload = dict(data)
    if isinstance(payload.get("trial_used"), int):
        sealed = _seal_trial(int(payload["trial_used"]))
        payload["trial"] = sealed
        if _REG_TRIAL_LAST["blob"] != sealed:
            _save_registry_trial(sealed)
            _REG_TRIAL_LAST["blob"] = sealed
    payload.pop("trial_used", None)
    target = guard_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：避免并发读（如时间篡改检测）读到半截 JSON
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        pass
    if payload.get("last_run_at"):
        _save_registry_last_run(int(payload["last_run_at"]))
