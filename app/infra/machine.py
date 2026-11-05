"""机器码与本地隐藏登记簿。

- 机器码：只读（用户不可修改），由硬件/系统指纹派生，首次生成后落盘复用。
- 隐藏登记簿：免费版月度额度防刷（删除数据库重装也无法清零）。
"""
import hashlib
import json
import os
import platform
import sys
import uuid
from pathlib import Path


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


def guard_file() -> Path:
    return registry_dir() / "guard.json"


def load_guard() -> dict:
    try:
        return json.loads(guard_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_guard(data: dict) -> None:
    try:
        guard_file().parent.mkdir(parents=True, exist_ok=True)
        guard_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def month_key(machine: str, month: str) -> str:
    """「机器码 + 当月月份」哈希：登记簿键，无法直接反推机器码。"""
    raw = f"{machine}:{month}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]
