"""前端 E2E 渲染灾难与交互防御（Playwright / Chromium）。

覆盖：
1. 1 万条数据下发票列表首屏渲染 < 2 秒、分页不爆 DOM
2. “执行匹配”防狂点：1 秒内 20 次调用只发出 1 个请求
3. XSS 纯文本渲染：列表与详情页不弹窗
"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db

pytest.importorskip("playwright")


@pytest.fixture(scope="module")
def browser():
    """直接复用本机已安装的 Chromium，避免 E2E 依赖在线下载浏览器。"""
    from playwright.sync_api import sync_playwright
    exe = os.environ.get("PW_CHROME_EXE")
    if not exe:
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
        cands = sorted(root.glob("chromium-*/chrome-win/chrome.exe"), reverse=True)
        if not cands:
            pytest.skip("未找到本机 Chromium，可设置 PW_CHROME_EXE")
        exe = str(cands[0])
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe, headless=True)
        yield b
        b.close()


@pytest.fixture(scope="module")
def e2e_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    saved = {k: getattr(settings, k) for k in
             ("data_dir", "db_path", "upload_dir", "log_dir", "license_public_key_pem")}
    saved_registry = machine_mod.registry_dir
    saved_machine = machine_mod.machine_code
    settings.data_dir = tmp
    settings.db_path = tmp / "t.db"
    settings.upload_dir = tmp / "uploads"
    settings.log_dir = tmp / "logs"
    machine_mod.registry_dir = lambda: tmp / "hidden"
    machine_mod.machine_code = lambda: "1111-2222-3333-4444"

    key = Ed25519PrivateKey.generate()
    settings.license_public_key_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)

    from app.repository.db import init_schema
    from app.service import auth_service, license_service
    init_schema()
    auth_service.create_first_admin("admin", "Admin@123456", "管理员")
    auth_service.ack_recovery()

    raw = json.dumps({"machine_code": "1111-2222-3333-4444", "plan": "enterprise",
                      "role": "master", "exp": int(time.time()) + 86400 * 365,
                      "duration_days": 365, "sub_limit": 3,
                      "order_id": "E2E"}, separators=(",", ":")).encode()
    import base64
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    license_service.apply_license_code(f"{b64(raw)}.{b64(key.sign(raw))}")

    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO invoice (file_hash, file_type, invoice_no, invoice_date,"
            " buyer_name, seller_name, amount, tax_amount, total_amount, status,"
            " parse_engine, parse_confidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"e2e-{i}", "xml", f"E2E{i:07d}", "2026-09-01",
              "北京示例科技有限公司", "测试供应商", "10.00", "0.00", "10.00",
              "NORMAL", "xml", 1.0) for i in range(10000)])
        conn.execute(
            "INSERT INTO invoice (file_hash, file_type, invoice_no, invoice_date,"
            " buyer_name, seller_name, amount, tax_amount, total_amount, status,"
            " parse_engine, parse_confidence, item_summary)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e2e-xss", "xml", "XSSPAY001", "2026-09-01",
             "<script>alert('xss')</script>", "测试供应商", "1.00", "0.00",
             "1.00", "NORMAL", "xml", 1.0, "<script>alert(2)</script>"))

    from app.main import create_app
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, lifespan="off",
        log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            import urllib.request
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("E2E 服务启动失败")

    yield base

    server.should_exit = True
    thread.join(timeout=5)
    for k, v in saved.items():
        setattr(settings, k, v)
    machine_mod.registry_dir = saved_registry
    machine_mod.machine_code = saved_machine


def login(page, base):
    page.goto(base + "/login")
    page.fill("#username", "admin")
    page.fill("#password", "Admin@123456")
    page.click("#loginBtn")
    page.wait_for_url("**/", timeout=15000)


def test_10k_rows_first_paint_under_2s(browser, e2e_server):
    page = browser.new_page()
    login(page, e2e_server)
    t0 = time.perf_counter()
    page.goto(e2e_server + "/invoices", wait_until="domcontentloaded")
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"1 万条数据首屏 {elapsed:.2f}s"
    # 分页生效：DOM 只渲染当前页，不把 1 万行一次性塞进浏览器
    rows = page.locator("table tr").count()
    assert rows <= 120, f"首屏渲染 {rows} 行，分页可能失效"
    assert page.get_by_text("共", exact=False).first.is_visible()
    page.mouse.wheel(0, 2000)
    assert page.locator("body").is_visible()
    print(f"\n[e2e] 1 万条首屏 {elapsed * 1000:.0f}ms / DOM {rows} 行")


def test_double_click_match_only_one_request(browser, e2e_server):
    page = browser.new_page()
    login(page, e2e_server)
    page.goto(e2e_server + "/matches")
    requests = []
    page.on("request", lambda r: requests.append(r.url)
            if "/api/match/tasks" in r.url and r.method == "POST" else None)
    page.evaluate("""() => {
        const b = document.querySelector('button[onclick*="RecalcTask.start"]');
        for (let i = 0; i < 20; i++) RecalcTask.start(b);
    }""")
    page.wait_for_timeout(900)
    assert len(requests) == 1, f"狂点发出 {len(requests)} 个请求"
    disabled = page.eval_on_selector(
        'button[onclick*="RecalcTask.start"]', "b => b.disabled")
    assert disabled is True
    print(f"\n[e2e] 20 次狂点实际请求 {len(requests)} 个")


def test_xss_renders_as_plain_text(browser, e2e_server):
    page = browser.new_page()
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    login(page, e2e_server)
    page.goto(e2e_server + "/invoices?q=XSSPAY001",
              wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    assert not dialogs, f"列表页触发弹窗: {dialogs}"
    text = page.locator("body").inner_text()
    assert "XSSPAY001" in text
    assert "alert('xss')" in text or "alert(2)" in text

    with db.read() as conn:
        iid = conn.execute("SELECT id FROM invoice WHERE invoice_no='XSSPAY001'"
                           ).fetchone()["id"]
    page.goto(e2e_server + f"/invoices/{iid}", wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    assert not dialogs, f"详情页触发弹窗: {dialogs}"
