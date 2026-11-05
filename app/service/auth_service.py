"""本地账号与登录会话。

- 密码/恢复码：PBKDF2-HMAC-SHA256 加盐哈希，明文绝不落库。
- 会话：HMAC 签名的 Cookie（秘钥首次启动生成后存 kv），无需额外依赖。
- 角色：admin（全部）/ finance（业务数据，无团队管理）/ employee（仅有自己数据）。
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from app.config import settings
from app.repository import db
from app.repository.repos import kv_repo, user_repo

COOKIE_NAME = "fapiao_session"
ROLE_CN = {"admin": "管理员", "finance": "财务", "employee": "员工"}
ROLE_LEVEL = {"admin": 3, "finance": 2, "employee": 1}
_SESSION_SECRET_KEY = "auth_session_secret"
_RECOVERY_PENDING_KEY = "auth_recovery_pending"
_PBKDF2_ITER = 120_000
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# 进程内缓存：账号一旦存在恒为真；会话密钥读一次即可（避免每个请求查库）
_HAS_USERS_CACHE = False
_SESSION_SECRET_CACHE = ""


class AuthError(Exception):
    """账号/登录类业务错误（message 可直接展示给用户）。"""


# ---------------------------------------------------------------- 密码
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(got.hex(), digest_hex)
    except Exception:
        return False


def _password_fingerprint(password_hash: str) -> str:
    """密码哈希的短摘要：写进会话 token，密码一旦变更旧会话立即失效。"""
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:16]


def gen_recovery_code() -> str:
    """16 位应急恢复码（4 组，去除易混字符）。"""
    chars = [secrets.choice(_RECOVERY_ALPHABET) for _ in range(16)]
    return "-".join("".join(chars[i:i + 4]) for i in range(0, 16, 4))


def _normalize_code(code: str) -> str:
    return "".join(c for c in (code or "").upper() if c.isalnum())


def verify_recovery_code(code: str, stored: str) -> bool:
    return verify_password(_normalize_code(code), stored)


# ---------------------------------------------------------------- 账号
def has_users() -> bool:
    global _HAS_USERS_CACHE
    if _HAS_USERS_CACHE:
        return True
    with db.read() as conn:
        exists = user_repo.count(conn) > 0
    if exists:
        _HAS_USERS_CACHE = True
    return exists


def get_user(user_id: int) -> Optional[dict]:
    with db.read() as conn:
        return user_repo.get(conn, user_id)


def get_by_username(username: str) -> Optional[dict]:
    with db.read() as conn:
        return user_repo.get_by_username(conn, (username or "").strip())


def list_users() -> list:
    with db.read() as conn:
        rows = user_repo.list(conn)
    for r in rows:
        r.pop("password_hash", None)
        r.pop("recovery_hash", None)
        r["role_cn"] = ROLE_CN.get(r.get("role"), r.get("role"))
    return rows


def _check_username(username: str) -> str:
    name = (username or "").strip()
    if len(name) < 2 or len(name) > 32:
        raise AuthError("用户名长度需为 2~32 个字符")
    if not all(c.isalnum() or c in "._-" for c in name):
        raise AuthError("用户名只能包含字母、数字、点、下划线或短横线")
    return name


def _check_password(password: str) -> str:
    pwd = password or ""
    if len(pwd) < 8:
        raise AuthError("密码至少 8 位")
    if len(pwd) > 128:
        raise AuthError("密码过长")
    return pwd


def create_first_admin(username: str, password: str, display_name: str = "") -> dict:
    """首次创建管理员：返回一次性明文恢复码（仅此一次）。"""
    name = _check_username(username)
    pwd = _check_password(password)
    recovery = gen_recovery_code()
    with db.tx() as conn:
        if user_repo.count(conn) > 0:
            raise AuthError("系统已存在账号，禁止重复初始化")
        uid = user_repo.insert(conn, {
            "username": name,
            "display_name": (display_name or "").strip() or "管理员",
            "role": "admin",
            "password_hash": hash_password(pwd),
            "recovery_hash": hash_password(_normalize_code(recovery)),
            "is_active": 1,
        })
        kv_repo.set(conn, _RECOVERY_PENDING_KEY, "1")
    return {"user_id": uid, "username": name, "recovery_code": recovery}


def create_user(username: str, password: str, role: str = "employee",
                display_name: str = "") -> int:
    name = _check_username(username)
    pwd = _check_password(password)
    role = role if role in ROLE_CN else "employee"
    with db.tx() as conn:
        if user_repo.get_by_username(conn, name) is not None:
            raise AuthError("用户名已存在")
        return user_repo.insert(conn, {
            "username": name,
            "display_name": (display_name or "").strip() or name,
            "role": role,
            "password_hash": hash_password(pwd),
            "recovery_hash": "",
            "is_active": 1,
        })


def authenticate(username: str, password: str) -> dict:
    row = get_by_username(username)
    if not row or not verify_password(password or "", row.get("password_hash", "")):
        raise AuthError("用户名或密码错误")
    if not row.get("is_active"):
        raise AuthError("该账号已被停用，请联系管理员")
    with db.tx() as conn:
        user_repo.update_fields(conn, row["id"], {
            "last_login_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return row


def set_active(user_id: int, active: bool) -> None:
    with db.tx() as conn:
        user = user_repo.get(conn, user_id)
        if user is None:
            raise AuthError("账号不存在")
        if user.get("role") == "admin" and not active and user_repo.count_admins(conn) <= 1:
            raise AuthError("不能停用唯一的管理员账号")
        user_repo.update_fields(conn, user_id, {"is_active": 1 if active else 0})


def reset_password(user_id: int, new_password: str) -> None:
    pwd = _check_password(new_password)
    with db.tx() as conn:
        if user_repo.get(conn, user_id) is None:
            raise AuthError("账号不存在")
        # 重置密码后强制用户下次改密（防止临时密码被长期使用）
        user_repo.update_fields(conn, user_id, {
            "password_hash": hash_password(pwd), "must_change_pwd": 1})


def change_password(user_id: int, old_password: str, new_password: str) -> dict:
    """用户自助改密：校验原密码 -> 更新哈希 -> 清除强制改密标记，返回新用户行。"""
    pwd = _check_password(new_password)
    with db.tx() as conn:
        user = user_repo.get(conn, user_id)
        if user is None:
            raise AuthError("账号不存在")
        if not verify_password(old_password or "", user.get("password_hash", "")):
            raise AuthError("原密码不正确")
        user_repo.update_fields(conn, user_id, {
            "password_hash": hash_password(pwd), "must_change_pwd": 0})
        return user_repo.get(conn, user_id)


def delete_user(user_id: int) -> None:
    with db.tx() as conn:
        user = user_repo.get(conn, user_id)
        if user is None:
            raise AuthError("账号不存在")
        if user.get("role") == "admin" and user_repo.count_admins(conn) <= 1:
            raise AuthError("不能删除唯一的管理员账号")
        user_repo.delete(conn, user_id)


def regenerate_recovery(user_id: int) -> str:
    """重新生成应急恢复码（旧码立即失效），返回明文。"""
    code = gen_recovery_code()
    with db.tx() as conn:
        user_repo.update_fields(conn, user_id, {
            "recovery_hash": hash_password(_normalize_code(code))})
    return code


def reset_by_recovery(username: str, recovery_code: str, new_password: str) -> None:
    row = get_by_username(username)
    if not row or not row.get("recovery_hash") \
            or not verify_recovery_code(recovery_code, row["recovery_hash"]):
        raise AuthError("用户名或应急恢复码不正确")
    reset_password(row["id"], new_password)


def reset_admin_to_initial() -> None:
    """超管解锁码校验通过后：所有管理员密码重置为初始密码。"""
    with db.tx() as conn:
        admins = [r for r in user_repo.list(conn) if r.get("role") == "admin"]
        for a in admins:
            user_repo.update_fields(conn, a["id"], {
                "password_hash": hash_password(settings.initial_password),
                "is_active": 1,
                "must_change_pwd": 1,
            })
    if not admins:
        raise AuthError("系统尚未创建管理员账号")


# ---------------------------------------------------------------- 会话
def _session_secret() -> str:
    global _SESSION_SECRET_CACHE
    if _SESSION_SECRET_CACHE:
        return _SESSION_SECRET_CACHE
    with db.read() as conn:
        val = kv_repo.get(conn, _SESSION_SECRET_KEY)
    if val:
        _SESSION_SECRET_CACHE = val
        return val
    val = secrets.token_hex(32)
    with db.tx() as conn:
        kv_repo.set(conn, _SESSION_SECRET_KEY, val)
    _SESSION_SECRET_CACHE = val
    return val


def make_session_token(user_id: int, days: int = 0, password_hash: str = "") -> str:
    exp = int(time.time()) + (days * 86400 if days else 12 * 3600)
    payload = _b64e(json.dumps({
        "uid": user_id, "exp": exp,
        "pv": _password_fingerprint(password_hash),
    }, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def _session_payload(token: str) -> Optional[dict]:
    """校验签名与有效期，返回 token 数据（含 uid / pv）。"""
    try:
        payload, sig = (token or "").split(".", 1)
        expect = _b64e(hmac.new(_session_secret().encode(), payload.encode(),
                                hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_b64d(payload))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return data
    except Exception:
        return None


def parse_session_token(token: str) -> Optional[int]:
    data = _session_payload(token)
    try:
        return int(data["uid"]) if data else None
    except (KeyError, TypeError, ValueError):
        return None


def user_from_request(request) -> Optional[dict]:
    """从 Cookie 解析当前登录用户（未登录/失效/密码已变更返回 None）。"""
    try:
        token = request.cookies.get(COOKIE_NAME)
        data = _session_payload(token) if token else None
        if not data or not data.get("uid"):
            return None
        user = get_user(int(data["uid"]))
        if not user or not user.get("is_active"):
            return None
        # 密码变更（重置/改密）后，旧会话的 pv 与新密码哈希不符 -> 立即失效
        if str(data.get("pv") or "") != _password_fingerprint(user.get("password_hash")):
            return None
        user.pop("password_hash", None)
        user.pop("recovery_hash", None)
        user["role_cn"] = ROLE_CN.get(user.get("role"), user.get("role"))
        return user
    except Exception:
        return None


def set_session_cookie(response, user: dict, remember: bool) -> None:
    """登录/初始化/改密成功后签发会话（pv 绑定当前密码哈希）。"""
    response.set_cookie(
        COOKIE_NAME, make_session_token(user["id"], 30 if remember else 0,
                                        user.get("password_hash", "")),
        max_age=30 * 86400 if remember else None,
        httponly=True, samesite="lax",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)


# ---------------------------------------------------------------- 初始化确认
def recovery_pending() -> bool:
    with db.read() as conn:
        return kv_repo.get(conn, _RECOVERY_PENDING_KEY) == "1"


def ack_recovery() -> None:
    with db.tx() as conn:
        kv_repo.set(conn, _RECOVERY_PENDING_KEY, "0")


def can_manage_team(user: Optional[dict]) -> bool:
    return bool(user) and user.get("role") == "admin"


def is_staff(user: Optional[dict]) -> bool:
    """财务/管理员可见全部业务数据；员工只能看自己的。"""
    return bool(user) and ROLE_LEVEL.get(user.get("role"), 0) >= ROLE_LEVEL["finance"]
