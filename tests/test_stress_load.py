"""压力与并发测试：额度防超发、并发删改、匹配串行、大数据量性能。

设计原则：
1. 全部跑在临时数据目录，绝不触碰正式库；
2. 关闭 fsync（db_synchronous=OFF）以测量 CPU/SQL 侧真实开销，
   避免 E: 盘单次 fsync 400ms 干扰吞吐指标；
3. 阈值取宽松上限（本机正常耗时通常只占 20%~40%），
   目的是拦截「算法退化一个数量级」这类回归，而不是做微基准。

运行：
    python -m pytest tests/test_stress_load.py -q
"""
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.infra import machine as machine_mod
from tests import matrix_support as sup


@pytest.fixture()
def env(tmp_path, monkeypatch):
    sup.reset_caches()
    sup.configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "db_synchronous", "OFF")
    sup.init_and_seed_admin()
    yield tmp_path
    sup.reset_caches()


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", True)
    from app.main import create_app
    c = TestClient(create_app())
    sup.login(c)
    return c


def bulk_invoices(n, status="NORMAL", prefix="BULK"):
    """批量落发票（直接走仓储层，避免逐条 HTTP 开销）。"""
    from app.repository import db
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO invoice (file_hash, file_type, invoice_no, invoice_date,"
            " buyer_name, seller_name, amount, tax_amount, total_amount, status,"
            " parse_engine, parse_confidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"{prefix}-{uuid.uuid4().hex}", "xml", f"{prefix}{i:07d}",
              "2026-09-01", "北京示例科技有限公司", "测试供应商",
              "10.00", "0.00", "10.00", status, "xml", 1.0)
             for i in range(n)])


def bulk_reimburses(n, prefix="BX-STRESS"):
    from app.repository import db
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO reimburse (batch_id, reimburse_no, submitter,"
            " reimburse_date, biz_date, amount, row_index, template_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(f"stress-{prefix}", f"{prefix}-{i:05d}", "张三", "2026-09-01",
              "2026-09-01", "10.00", 1, None) for i in range(n)])


# =====================================================================
# 一、并发正确性（防超发 / 防重复生效）
# =====================================================================
class TestConcurrencyCorrectness:
    def test_trial_quota_never_oversold_under_concurrency(self, env, monkeypatch):
        """60 个并发扣减只能成功 50 次，绝不能超发（免费额度是收入底线）。"""
        monkeypatch.setattr(settings, "dev_mode", False)
        from app.service import license_service as lic

        # 已锁过云端：跳过首次联网，专测本地并发原子性
        machine_mod.save_guard({"trial_used": 0, "trial_locked": True})
        total = int(settings.trial_invoice_quota)
        workers = total + 10
        ok = []
        fail = []
        barrier = threading.Barrier(workers)

        def worker():
            barrier.wait()
            try:
                lic.consume_invoice_quota(1)
                ok.append(1)
            except lic.QuotaExceeded:
                fail.append(1)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert len(ok) == total, f"成功 {len(ok)} 次，应为 {total}"
        assert len(fail) == 10
        assert lic.quota_status()["used"] == total

    def test_concurrent_manual_invoice_inserts(self, env, monkeypatch):
        """30 个并发人工录入：全部落库、互不覆盖、无唯一键冲突。"""
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.repository.db import init_schema  # noqa: F401
        from app.service import invoice_service

        results = []
        lock = threading.Lock()

        def worker(i):
            r = invoice_service.manual_create({
                "invoice_no": f"CC-{i:04d}", "invoice_date": sup.today_str(),
                "amount": "10.00", "tax_amount": "0.00", "total_amount": "10.00",
                "buyer_name": "并发测试", "seller_name": "并发供应商",
            })
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)

        assert len(results) == 30
        assert all(r.get("ok") for r in results), [r for r in results if not r.get("ok")]
        assert sup.count_rows("invoice", "invoice_no LIKE 'CC-%'") == 30

    def test_concurrent_delete_same_invoice_only_once_succeeds(self, env, monkeypatch):
        """同一张发票被 10 个请求并发删除：只允许成功 1 次，其余安全返回。"""
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.service import invoice_service
        iid = sup.seed_raw_invoice("ONCE-1", status="NORMAL", tax="0.00")

        ok = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            r = invoice_service.delete_invoice(iid)
            if r.get("ok"):
                with lock:
                    ok.append(1)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert len(ok) == 1, f"重复删除成功 {len(ok)} 次"

    def test_matching_is_globally_serialized(self, env, monkeypatch):
        """匹配引擎全局串行：并发 8 个请求只有 1 个拿到锁，其余立即得到 MatchBusy。"""
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.service import match_service
        bulk_invoices(20, status="NORMAL", prefix="MS")
        bulk_reimburses(20, prefix="MS-BX")

        success, busy = [], []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                match_service.run_matching(wait_seconds=0)
                success.append(1)
            except match_service.MatchBusy:
                busy.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(success) == 1, f"同时进入匹配的线程数 {len(success)}"
        assert len(busy) == 7

    def test_concurrent_page_reads_stay_healthy(self, client):
        """16 个并发读列表页：全部 200，无 5xx / 无锁超时。"""
        errors = []

        def worker():
            try:
                for _ in range(2):
                    r = client.get("/invoices?range=all&page_size=50")
                    if r.status_code != 200:
                        errors.append(r.status_code)
            except Exception as e:                     # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not errors, errors[:3]


# =====================================================================
# 二、大数据量性能
# =====================================================================
class TestLargeVolumePerformance:
    def test_invoice_list_5000_rows(self, client):
        bulk_invoices(5000, status="NORMAL", prefix="PERF")
        t0 = time.perf_counter()
        r = client.get("/invoices?range=all&page_size=50")
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200
        assert "共 5000 条" in r.text
        assert elapsed < 2.5, f"5000 条首屏 {elapsed:.2f}s"

    def test_custom_page_size_1000_rows(self, client):
        bulk_invoices(3000, status="NORMAL", prefix="PAGE")
        t0 = time.perf_counter()
        r = client.get("/invoices?range=all&page_size=1000")
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200
        assert "第 1 / 3 页" in r.text
        assert elapsed < 4.0, f"每页 1000 条 {elapsed:.2f}s"

    def test_report_payload_1000_records(self, env, monkeypatch):
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.service import report_service
        bulk_invoices(700, status="ABNORMAL", prefix="RPT")
        bulk_invoices(300, status="NORMAL", prefix="RPTN")
        bulk_reimburses(200, prefix="RPT-BX")

        t0 = time.perf_counter()
        payload = report_service.report_payload(page_size=20)
        elapsed = time.perf_counter() - t0
        assert payload["stats"]["invoice_total"] >= 1000
        assert elapsed < 3.5, f"差异报告数据包 {elapsed:.2f}s"

    def test_excel_export_2000_rows(self, client):
        bulk_invoices(2000, status="NORMAL", prefix="EXP")
        t0 = time.perf_counter()
        r = client.get("/api/invoices/export")
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200 and r.content[:2] == b"PK"
        assert elapsed < 15.0, f"导出 2000 行 {elapsed:.2f}s"

    def test_batch_delete_500(self, client):
        from app.repository import db
        bulk_invoices(500, status="NORMAL", prefix="DELB")
        with db.read() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM invoice WHERE invoice_no LIKE 'DELB%'")]
        t0 = time.perf_counter()
        r = client.post("/api/invoices/batch-delete",
                        data={"ids": ",".join(map(str, ids))}).json()
        elapsed = time.perf_counter() - t0
        assert r["data"]["ok"] is True
        assert elapsed < 15.0, f"批量删除 500 条 {elapsed:.2f}s"

    def test_purge_300_from_recycle_bin(self, client):
        from app.repository import db
        from app.service import invoice_service
        bulk_invoices(300, status="NORMAL", prefix="PURG")
        with db.read() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM invoice WHERE invoice_no LIKE 'PURG%'")]
        invoice_service.delete_invoices(ids)
        t0 = time.perf_counter()
        r = client.post("/api/recycle-bin/purge",
                        data={"kind": "invoice", "ids": ",".join(map(str, ids))}).json()
        elapsed = time.perf_counter() - t0
        assert r["data"]["ok"] is True
        assert sup.count_rows("invoice", "invoice_no LIKE 'PURG%'") == 0
        assert elapsed < 20.0, f"彻底删除 300 条 {elapsed:.2f}s"

    def test_audit_10k_pagination(self, client):
        from app.repository import db
        with db.tx() as conn:
            conn.executemany(
                "INSERT INTO audit_log (action, target_type, target_id, target_label,"
                " operator, detail) VALUES (?,?,?,?,?,?)",
                [("delete", "invoice", str(i), f"INV{i}", "admin", "压测数据")
                 for i in range(10000)])

        t0 = time.perf_counter()
        d1 = client.get("/api/audit?page=1&page_size=20").json()["data"]
        t1 = time.perf_counter()
        d2 = client.get("/api/audit?page=500&page_size=20").json()["data"]
        t2 = time.perf_counter()

        assert d1["total"] >= 10000
        assert len(d1["items"]) == 20 and len(d2["items"]) == 20
        assert t1 - t0 < 2.0, f"审计首页 {t1 - t0:.2f}s"
        assert t2 - t1 < 2.0, f"审计深翻页 {t2 - t1:.2f}s"

    def test_recycle_bin_deep_pagination(self, client):
        from app.service import invoice_service
        bulk_invoices(1200, status="NORMAL", prefix="RBSTR")
        from app.repository import db
        with db.read() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM invoice WHERE invoice_no LIKE 'RBSTR%'")]
        invoice_service.delete_invoices(ids)
        t0 = time.perf_counter()
        d = client.get("/api/recycle-bin?page=60&page_size=20").json()["data"]
        elapsed = time.perf_counter() - t0
        assert d["total"] >= 1200
        assert len(d["items"]) == 20
        assert elapsed < 5.0, f"回收站深翻页 {elapsed:.2f}s"

    def test_upload_rejects_60_bad_files_quickly(self, client):
        files = [("files", (f"bad{i}.txt", b"x", "text/plain")) for i in range(60)]
        t0 = time.perf_counter()
        r = client.post("/api/invoices/upload", files=files)
        elapsed = time.perf_counter() - t0
        data = r.json()["data"]
        assert len(data) == 60
        assert all(x["ok"] is False for x in data)
        assert elapsed < 5.0, f"60 个非法文件 {elapsed:.2f}s"
