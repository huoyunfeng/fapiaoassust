"""桌面端启动入口（生产环境 / PyInstaller 打包入口）。

职责（对应「桌面端架构防坑方案」）：
  1. 单实例检测：临时目录 filelock 锁文件，重复打开立即弹窗并退出；
  2. 数据目录：由 app.config 统一解析到 %APPDATA%\\FapiaoAssistant（见配置层）；
  3. 固定端口：默认 8000（可用环境变量 FAPIAO_PORT 覆盖），被占用时按 8001~8009 顺延；
  4. 后台线程跑 FastAPI，主线程探测 /health（最长 10 秒）通过后再开原生窗口；
  5. 优雅退出：窗口关闭 → 调用关停 API 释放数据库连接 → 等待 1.5 秒 → os._exit(0)。

开发调试仍可用 `python run.py`（固定 8000 端口、浏览器访问）。
"""
import ctypes
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
APP_TITLE = "发票报销核对助手"
# 固定端口：桌面端始终使用 8000，方便书签 / 排查；被占用时按 8001~8009 顺延
PREFERRED_PORT = int(os.environ.get("FAPIAO_PORT", "8000"))
PORT_SPAN = 10            # 固定端口被占用时的候选范围（8000~8009）
HEALTH_TIMEOUT = 10.0     # 启动探测上限（秒）
START_RETRIES = 2         # 单个端口的启动尝试次数
SHUTDOWN_SETTLE = 1.5     # 关停后等待后台彻底收尾的时间

# 打包为 --noconsole 后 stdout/stderr 为 None，日志库写入会抛异常；
# 统一重定向到空设备，并持有引用防止被 GC 提前回收。
_NULL_STREAMS = []


def _ensure_std_streams() -> None:
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            stream = open(os.devnull, "w", encoding="utf-8")
            _NULL_STREAMS.append(stream)
            setattr(sys, name, stream)


def _alert(message: str) -> None:
    """Windows 原生弹窗（不依赖任何 GUI 库；非 Windows 退化为 stderr）。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
    except Exception:
        print(f"{APP_TITLE}: {message}", file=sys.stderr)


class SingleInstance:
    """基于 filelock 的单实例检测（锁文件放临时目录）。

    锁随进程退出由操作系统自动释放，崩溃后不会残留死锁。
    """

    def __init__(self) -> None:
        from filelock import FileLock

        self.path = Path(tempfile.gettempdir()) / "FapiaoAssistant.lock"
        self._lock = FileLock(str(self.path))

    def acquire(self) -> bool:
        from filelock import Timeout

        try:
            # timeout=0：已被其他进程持锁时立刻失败，不等待
            self._lock.acquire(timeout=0)
        except Timeout:
            return False
        # 写入当前 PID，便于排查（不影响锁本身）
        try:
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        return True

    def release(self) -> None:
        try:
            self._lock.release()
        except Exception:
            pass


def _port_free(port: int) -> bool:
    """端口是否可用：能 bind 说明没有其他进程在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def _is_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/health", timeout=0.8) as resp:
            return resp.status == 200
    except Exception:
        return False


class Backend:
    """在后台线程中运行 uvicorn，主线程可观测启动状态与健康度。"""

    def __init__(self, port: int) -> None:
        import uvicorn

        from app.main import app

        self.port = port
        self.crashed = False
        self.config = uvicorn.Config(
            app, host=HOST, port=port, log_level="info", access_log=False)
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self._run, name="uvicorn", daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException:
            # 端口占用时 uvicorn 内部会 sys.exit(1)，线程内表现为 SystemExit
            self.crashed = True

    def start(self) -> None:
        self.thread.start()

    def wait_healthy(self, timeout: float = HEALTH_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _is_healthy(self.port):
                return True
            if self.crashed or not self.thread.is_alive():
                return False
            time.sleep(0.2)
        return False


def _start_backend() -> Backend:
    """固定端口优先（默认 8000），被占用时按序顺延，确保服务一定拉得起来。"""
    tried = []
    for port in range(PREFERRED_PORT, PREFERRED_PORT + PORT_SPAN):
        if not _port_free(port):
            tried.append(f"{port} 被占用")
            continue
        for attempt in range(1, START_RETRIES + 1):
            backend = Backend(port)
            backend.start()
            if backend.wait_healthy():
                return backend
            tried.append(f"{port} 第 {attempt} 次启动失败")
            try:
                backend.server.should_exit = True
            except Exception:
                pass
            backend.thread.join(timeout=2)
    raise RuntimeError("后台服务启动失败：" + "；".join(tried[-5:]))


def _graceful_exit(backend: Backend, token: str) -> None:
    """优雅退出：HTTP 通知后台释放数据库连接 → 停 uvicorn → 等待 → 强制结束。"""
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{backend.port}/api/_shutdown",
            method="POST",
            headers={"X-Shutdown-Token": token},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
    except Exception:
        pass
    try:
        backend.server.should_exit = True
    except Exception:
        pass
    backend.thread.join(timeout=5)
    time.sleep(SHUTDOWN_SETTLE)
    os._exit(0)


def _run_window(backend: Backend, token: str) -> None:
    url = f"http://{HOST}:{backend.port}/"
    try:
        import webview
    except ImportError:
        webview = None

    if webview is None:
        # 未安装 pywebview（如开发机）时退化为系统浏览器，保证可用性
        import webbrowser
        webbrowser.open(url)
        try:
            while backend.thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        _graceful_exit(backend, token)
        return

    window = webview.create_window(
        APP_TITLE, url, width=1280, height=820, min_size=(1024, 720))
    # 拦截窗口关闭：先安全关停后台，再结束进程，避免 SQLite 被强杀导致损坏
    window.events.closing += lambda: _graceful_exit(backend, token)
    webview.start()
    _graceful_exit(backend, token)   # 兜底（事件未触发时）


def main() -> int:
    _ensure_std_streams()

    lock = SingleInstance()
    if not lock.acquire():
        _alert("软件已在运行中，请勿重复打开。")
        return 0

    try:
        # 关停令牌：仅本次启动有效，只有启动器知道，防止本机其他程序随意关停服务
        token = secrets.token_hex(16)
        os.environ["FAPIAO_SHUTDOWN_TOKEN"] = token

        try:
            backend = _start_backend()
        except Exception as exc:
            _alert(f"后台服务启动失败：{exc}")
            return 1

        _run_window(backend, token)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
