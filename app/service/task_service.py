"""后台耗时任务管理器（真实进度条机制）。

为「备份、恢复、附件迁移、清理」这四类涉及大量文件读写的长耗时操作
提供统一的异步任务 + 进度查询能力。所有业务函数保持原签名不变；
仅新增带 progress callback 的包装版本。

设计要点：
- 任务存储在进程内 dict（单用户桌面应用，无需持久化）。
- 任务生命周期：pending → running → succeeded / failed
- 清理：完成后 10 分钟内仍可查询，之后自动清除。
- 线程安全：所有对 _tasks dict 的读写均经 _lock 保护。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.infra.logger import get_logger

logger = get_logger(__name__)

_MAX_TASKS = 32               # 最多保留的历史任务数（防止内存无限膨胀）
_KEEP_SECONDS = 10 * 60        # 完成后保留查询窗口


@dataclass
class Task:
    task_id: str
    kind: str                       # backup / restore / migrate / clean / silent-backup / diagnose
    state: str = "pending"          # pending / running / succeeded / failed
    progress: int = 0               # 0-100
    message: str = ""               # 人类可读的当前步骤描述
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    result: Optional[dict] = None   # 成功后的返回值
    error: Optional[str] = None     # 失败原因
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- 外部 API ----
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }

    def update(self, progress: int | None = None, message: str | None = None) -> None:
        """运行中更新进度/状态描述（业务线程调用）。线程安全。"""
        with self._lock:
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
            if message is not None:
                self.message = message

    def succeed(self, result: dict | None = None) -> None:
        with self._lock:
            self.state = "succeeded"
            self.progress = 100
            self.result = result
            self.finished_at = time.time()

    def fail(self, error: str) -> None:
        with self._lock:
            self.state = "failed"
            self.error = str(error)
            self.finished_at = time.time()


# ---------------------------------------------------------------------------
# 全局注册表
# ---------------------------------------------------------------------------

_tasks: "OrderedDict[str, Task]" = OrderedDict()
_tasks_lock = threading.Lock()


def create_task(kind: str, fn: Callable[..., Any], *args, **kwargs) -> str:
    """创建并启动一个后台任务，返回 task_id。

    fn 可通过 kwargs 中的 ``on_progress`` 回调来上报进度：
        def my_work(*args, on_progress=None, **kwargs):
            on_progress(30, "正在处理 300/1000 个文件")
            ...

    这里我们用一个适配器把 Task.update 包成回调传下去。
    """
    task_id = uuid.uuid4().hex[:12]
    task = Task(task_id=task_id, kind=kind)
    # 预注册
    with _tasks_lock:
        _tasks[task_id] = task
        _evict_locked()

    def _runner():
        task.state = "running"
        try:
            def _cb(pct: int, msg: str = ""):
                task.update(pct, msg)
            # 把进度回调注入 kwargs（若业务函数声明了该参数）
            new_kwargs = dict(kwargs)
            new_kwargs.setdefault("on_progress", _cb)
            # 先上报 0%
            task.update(0, "准备中…")
            result = fn(*args, **new_kwargs)
            task.succeed(result if isinstance(result, dict) else {"ok": True})
            logger.info("任务完成: %s (%s)", task_id, kind)
        except Exception as exc:
            logger.exception("任务失败: %s (%s)", task_id, kind)
            task.fail(str(exc))

    t = threading.Thread(target=_runner, name=f"task-{kind}-{task_id}", daemon=True)
    t.start()
    return task_id


def get_task(task_id: str) -> Optional[dict]:
    """查询任务状态；任务完成后 10 分钟内仍可查。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
        _evict_locked()
        return task.to_dict() if task else None


def _evict_locked() -> None:
    """清理已完成且超期的任务；同时限制总量。调用方需持有 _tasks_lock。"""
    now = time.time()
    outdated = [tid for tid, t in _tasks.items()
                if t.finished_at is not None and now - t.finished_at > _KEEP_SECONDS]
    for tid in outdated:
        _tasks.pop(tid, None)
    while len(_tasks) > _MAX_TASKS:
        _tasks.popitem(last=False)


def list_recent(limit: int = 8) -> list[dict]:
    """列出最近 N 个任务（静默备份状态展示用）。"""
    with _tasks_lock:
        items = list(reversed(list(_tasks.values())))[:limit]
        return [t.to_dict() for t in items]
