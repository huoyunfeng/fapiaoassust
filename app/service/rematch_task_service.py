"""重算任务调度：全局单任务锁 + 后台线程执行 + 内存进度快照。

- 发起接口立即返回 task_id，实际匹配在 daemon 线程中执行，不阻塞 HTTP 响应；
- 同一时刻只允许一个重算任务，重复发起返回业务错误；
- 进度按「每 100 条或每 1 秒」节流写入内存，供前端 2s 轮询；
- 任务结束后结算「超期 365 天未匹配发票」数量，供结果弹窗展示。
"""
import threading
import time
import traceback
import uuid
from datetime import date, timedelta
from typing import Optional, Tuple

from app.infra.logger import get_logger
from app.repository import db
from app.repository.repos import invoice_repo, match_repo, reimburse_repo
from app.service import match_service

logger = get_logger(__name__)

STALE_DAYS = 365
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

_LOCK = threading.Lock()
_TASK: Optional[dict] = None   # 当前 / 最近一次任务快照
_PROGRESS_STEP = 100           # 每处理 100 条刷新一次
_PROGRESS_SECONDS = 1.0        # 或每 1 秒刷新一次


def start(operator: str = None) -> Tuple[Optional[str], str]:
    """发起重算。返回 (task_id, 错误消息)；已有任务在跑时 task_id 为 None。"""
    global _TASK
    with _LOCK:
        if _TASK is not None and _TASK["status"] == RUNNING:
            return None, "已有重算任务正在进行，请稍候"
        now = time.time()
        task = {
            "task_id": uuid.uuid4().hex,
            "status": RUNNING,
            "processed": 0,
            "total": 0,
            "percent": 0,
            "phase": "准备数据",
            "records": 0,
            "stale_count": 0,
            "error": "",
            "result": {},
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "last_update": now,
        }
        _TASK = task
        task_id = task["task_id"]
    threading.Thread(target=_run, args=(task_id, operator),
                     name=f"rematch-{task_id[:8]}", daemon=True).start()
    return task_id, ""


def get_current() -> dict:
    """当前任务快照；无任务时返回 idle（刷新页面后据此恢复进度条）。"""
    with _LOCK:
        if _TASK is None:
            return {"status": "idle"}
        return dict(_TASK)


def get_task(task_id: str) -> Optional[dict]:
    with _LOCK:
        if _TASK is not None and _TASK["task_id"] == task_id:
            return dict(_TASK)
        return None


def _run(task_id: str, operator: str) -> None:
    try:
        records = _record_count()
        _patch(task_id, records=records, phase="准备数据")
        summary = match_service.run_matching(
            operator,
            progress=lambda processed, total, phase: _on_progress(
                task_id, processed, total, phase),
        )
        stale_count = _stale_invoice_count()
        total = get_task(task_id)["total"] if get_task(task_id) else 0
        _patch(task_id, status=COMPLETED, phase="已完成", percent=100,
               processed=total, total=total, stale_count=stale_count,
               finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
               result={"summary": summary, "stale_count": stale_count,
                       "records": records})
        logger.info("重算任务完成: task=%s records=%s stale=%s",
                    task_id, records, stale_count)
    except Exception:   # 后台线程异常必须兜底，否则任务会永远停在 running
        logger.error("重算任务失败: task=%s\n%s", task_id, traceback.format_exc())
        _patch(task_id, status=FAILED, phase="已失败",
               error="重算任务意外中断，请检查数据完整性后重试。",
               finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))


def _patch(task_id: str, **fields) -> None:
    with _LOCK:
        if _TASK is not None and _TASK["task_id"] == task_id:
            _TASK.update(fields)


def _on_progress(task_id: str, processed: int, total: int, phase: str = "") -> None:
    """进度回调：每 100 条或每 1 秒写入一次，避免高频锁竞争。"""
    now = time.time()
    with _LOCK:
        task = _TASK
        if task is None or task["task_id"] != task_id:
            return
        total = max(int(total), 1)
        processed = max(0, min(int(processed), total))
        finished = processed >= total
        if (not finished
                and processed - task["processed"] < _PROGRESS_STEP
                and now - task["last_update"] < _PROGRESS_SECONDS):
            return
        task["processed"] = processed
        task["total"] = total
        task["percent"] = 100 if finished else min(99, int(processed * 100 / total))
        if phase:
            task["phase"] = phase
        task["last_update"] = now


def _record_count() -> int:
    with db.read() as conn:
        return invoice_repo.count(conn) + reimburse_repo.count(conn)


def _stale_invoice_count() -> int:
    """开票日期超过 365 天且仍未匹配成功的发票数量。"""
    cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    with db.read() as conn:
        active = match_repo.active_invoice_ids(conn)
        rows = conn.execute(
            "SELECT id FROM invoice WHERE deleted_at IS NULL "
            "AND status IN ('NORMAL','ABNORMAL') "
            "AND invoice_date IS NOT NULL AND invoice_date != '' "
            "AND invoice_date < ?", (cutoff,)).fetchall()
    return sum(1 for r in rows if r["id"] not in active)
