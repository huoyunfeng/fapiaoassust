"""请求级上下文：当前操作人。

中间件在鉴权成功后写入真实登录用户名；审计日志与状态日志优先读取这里，
不再信任前端表单里的 operator 字段（避免所有操作都被记成 admin）。
"""
from contextvars import ContextVar

_current_operator: ContextVar[str] = ContextVar("current_operator", default="")


def set_current_operator(name: str) -> None:
    _current_operator.set((name or "").strip())


def current_operator(default: str = "") -> str:
    return _current_operator.get() or default
