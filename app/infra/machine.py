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


# 额度物理防重置：除隐藏登记簿外，再把「机器码+月份」额度写入注册表，
# 重装软件 / 删除数据库 / 清理 AppData 都无法清零。
_REG_SUBKEY = r"Software\FapiaoAssistant"
_REG_VALUE = "quota"
# 防时间回拨锚点也写注册表：只删 guard.json / 清 AppData 无法把「最后运行时间」回退
_REG_VALUE_LAST = "last_run"
_REG_LAST_WRITTEN = 0


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


def _registry_quota() -> dict:
    if not _registry_enabled():
        return {}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            raw, _type = winreg.QueryValueEx(key, _REG_VALUE)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_registry_quota(quota: dict) -> None:
    if not _registry_enabled():
        return
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            winreg.SetValueEx(key, _REG_VALUE, 0, winreg.REG_SZ,
                              json.dumps(quota, ensure_ascii=False))
    except Exception:
        pass


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
    """同步防回拨锚点到注册表；每 5 分钟最多写一次，避免频繁写注册表。"""
    global _REG_LAST_WRITTEN
    if not _registry_enabled() or ts <= _REG_LAST_WRITTEN:
        return
    if ts - _REG_LAST_WRITTEN < 300:
        return
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            winreg.SetValueEx(key, _REG_VALUE_LAST, 0, winreg.REG_SZ, str(int(ts)))
        _REG_LAST_WRITTEN = int(ts)
    except Exception:
        pass


def load_guard() -> dict:
    try:
        data = json.loads(guard_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    # 合并注册表额度（取两者最大值）：删文件、删库都无法把已用额度清零
    reg = _registry_quota()
    if reg:
        quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
        for k, v in reg.items():
            try:
                quota[k] = max(int(v), int(quota.get(k, 0) or 0))
            except Exception:
                continue
        data["quota"] = quota
    # 合并注册表防回拨锚点：删文件也无法让系统时间「回到过去」
    last = _registry_last_run()
    if last > int(data.get("last_run_at", 0) or 0):
        data["last_run_at"] = last
    return data


def save_guard(data: dict) -> None:
    try:
        guard_file().parent.mkdir(parents=True, exist_ok=True)
        guard_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    if isinstance(data.get("quota"), dict):
        _save_registry_quota(data["quota"])
    if data.get("last_run_at"):
        _save_registry_last_run(int(data["last_run_at"]))


def month_key(machine: str, month: str) -> str:
    """「机器码 + 当月月份」哈希：登记簿键，无法直接反推机器码。"""
    raw = f"{machine}:{month}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]
