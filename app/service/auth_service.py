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
import threading
import time
from typing import Optional

from app.config import settings
from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import kv_repo, user_repo
from app.service import audit_service

logger = get_logger(__name__)

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
# 登录失败节流：同一用户名连续失败 5 次锁定 60 秒（进程内，重启即清空）
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECONDS = 60
_LOGIN_FAILS = {}   # username -> [连续失败次数, 锁定到期时间戳]
_LOGIN_FAILS_MAX = 5000   # 上限：防止构造随机用户名撑大内存
# 并发保护：多线程同时登录失败时，遍历/插入同一字典会抛
# "dictionary changed size during iteration"，必须串行化。
_LOGIN_FAILS_LOCK = threading.RLock()


def _prune_login_fails_locked(now: float) -> None:
    """清理过期/超量记录（调用方必须已持有 _LOGIN_FAILS_LOCK）。"""
    expired = [k for k, v in _LOGIN_FAILS.items() if v[1] and v[1] <= now]
    for key in expired:
        _LOGIN_FAILS.pop(key, None)
    while len(_LOGIN_FAILS) > _LOGIN_FAILS_MAX:
        _LOGIN_FAILS.pop(next(iter(_LOGIN_FAILS)))


def _prune_login_fails(now: float) -> None:
    """定期清理过期/超量记录（登录失败节流字典的内存兜底）。"""
    with _LOGIN_FAILS_LOCK:
        _prune_login_fails_locked(now)


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
        # 席位强拦截：停用账号同样占席位，删除后立即释放
        from app.service import license_service      # 惰性导入，避免循环依赖
        limit = license_service.seat_limit()
        used = len(user_repo.list(conn))
        if used >= limit:
            raise AuthError(
                f"当前设备席位已满（{used}/{limit}）。"
                "请停用或删除已有账号释放席位，或升级企业版扩容")
        return user_repo.insert(conn, {
            "username": name,
            "display_name": (display_name or "").strip() or name,
            "role": role,
            "password_hash": hash_password(pwd),
            "recovery_hash": "",
            "is_active": 1,
        })


def authenticate(username: str, password: str) -> dict:
    name = (username or "").strip()
    now = time.time()
    with _LOGIN_FAILS_LOCK:
        _prune_login_fails_locked(now)
        state = _LOGIN_FAILS.get(name)
        if state and state[1] > now:
            raise AuthError(f"登录失败次数过多，请 {int(state[1] - now) + 1} 秒后重试")
    row = get_by_username(name)
    if not row or not verify_password(password or "", row.get("password_hash", "")):
        with _LOGIN_FAILS_LOCK:
            state = _LOGIN_FAILS.get(name)
            if state and state[1] > now:
                raise AuthError(f"登录失败次数过多，请 {int(state[1] - now) + 1} 秒后重试")
            fails = (state[0] if state else 0) + 1
            if fails >= _LOGIN_MAX_FAILS:
                _LOGIN_FAILS[name] = [0, now + _LOGIN_LOCK_SECONDS]
                raise AuthError("登录失败次数过多，账号已临时锁定 60 秒")
            _LOGIN_FAILS[name] = [fails, 0]
        raise AuthError("用户名或密码错误")
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.pop(name, None)
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


def set_role(user_id: int, role: str) -> None:
    """变更角色（管理员保护：不能把最后一个有效管理员降级）。"""
    if role not in ROLE_CN:
        raise AuthError("无效的角色")
    with db.tx() as conn:
        user = user_repo.get(conn, user_id)
        if user is None:
            raise AuthError("账号不存在")
        if user.get("role") == role:
            return
        if user.get("role") == "admin" and user.get("is_active") \
                and user_repo.count_admins(conn) <= 1:
            raise AuthError("不能降级唯一的管理员账号")
        user_repo.update_fields(conn, user_id, {"role": role})


def delete_user(user_id: int, operator: str = None) -> dict:
    """删除账号并物理删除其名下数据，立即释放 1 个席位。

    与业务删除保持一致：先清理匹配记录与闭环标记，删除后重新复核指向这些
    发票的 DUPLICATE 记录，事务提交后再清理附件文件（避免孤儿文件）。
    """
    from app.infra.filestore import FileStore
    from app.repository.repos import invoice_repo, log_repo, match_repo
    from app.service import invoice_service      # 惰性导入，避免循环依赖

    operator = operator or settings.default_operator
    paths = []
    with db.tx() as conn:
        user = user_repo.get(conn, user_id)
        if user is None:
            raise AuthError("账号不存在")
        if user.get("role") == "admin" and user_repo.count_admins(conn) <= 1:
            raise AuthError("不能删除唯一的管理员账号")
        invs = conn.execute(
            "SELECT id, source_path FROM invoice WHERE owner_user_id=?", (user_id,)
        ).fetchall()
        rmb_count = conn.execute(
            "SELECT COUNT(*) FROM reimburse WHERE owner_user_id=?", (user_id,)
        ).fetchone()[0]
        inv_ids = [r["id"] for r in invs]
        paths = [r["source_path"] for r in invs if r["source_path"]]
        for row in invs:
            match_repo.delete_for_invoice(conn, row["id"])
            log_repo.delete_for_invoice(conn, row["id"])
            invoice_repo.delete(conn, row["id"])
        # 指向被删发票的重复标记必须重新复核，否则会永久停留在 DUPLICATE
        if inv_ids:
            dead = set(inv_ids)
            for dup_id, raw_json in conn.execute(
                    "SELECT id, raw_json FROM invoice "
                    "WHERE status='DUPLICATE' AND deleted_at IS NULL").fetchall():
                try:
                    raw = json.loads(raw_json) if raw_json else {}
                except (json.JSONDecodeError, TypeError):
                    raw = {}
                if raw.get("dup_of") in dead:
                    try:
                        invoice_service._validate_and_dedup(conn, dup_id, operator)
                    except Exception:       # 复核失败不应阻断账号删除
                        logger.warning("删除账号后复核重复发票 #%s 失败", dup_id, exc_info=True)
        conn.execute("DELETE FROM reimburse WHERE owner_user_id=?", (user_id,))
        audit_service.log(conn, "purge", "user", user_id, user.get("username") or "",
                          "正常", "已删除", operator,
                          f"删除账号并清理其名下 {len(inv_ids)} 张发票、{rmb_count} 条报销单")
        user_repo.delete(conn, user_id)
    for p in paths:
        FileStore().delete(p)
    return {"deleted_invoices": len(inv_ids), "deleted_reimburses": int(rmb_count)}


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
