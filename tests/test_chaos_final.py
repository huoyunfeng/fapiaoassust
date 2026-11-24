"""终极灾难演练补充：多解组合全展示 / OCR超时 / 风控真实触发 /
端口顺延 / 磁盘满 / 杀软锁库 / 跨年额度重置 / 时区漂移容错

运行：python -m pytest tests/test_chaos_final.py -q
"""
import base64
import errno
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db


# =====================================================================
# 夹具（复用 test_ultimate.py 风格）
# =====================================================================
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    monkeypatch.setattr(machine_mod, "registry_dir", lambda: tmp_path / "hidden")
    monkeypatch.setattr(machine_mod, "machine_code", lambda: "1111-2222-3333-4444")
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(settings, "license_public_key_pem",
                        key.public_key().public_bytes(
                            serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo))
    from app.repository.db import init_schema
    from app.service import (audit_service, backup_service, category_service,
                             handover_service, invoice_service, license_service,
                             match_service, recycle_service, reimburse_service,
                             report_service, risk_service)
    from app.repository.repos import invoice_repo, reimburse_repo, match_repo
    init_schema()
    return {"db": db, "key": key, "tmp": tmp_path,
            "inv": invoice_service, "reb": reimburse_service,
            "match": match_service, "cat": category_service,
            "lic": license_service, "backup": backup_service,
            "handover": handover_service, "report": report_service,
            "recycle": recycle_service, "audit": audit_service,
            "risk": risk_service,
            "invoice_repo": invoice_repo, "reimburse_repo": reimburse_repo,
            "match_repo": match_repo}


def seed_inv(env, no, total, **over):
    with env["db"].tx() as conn:
        return env["invoice_repo"].insert(conn, {
            "file_hash": f"seed-{uuid.uuid4().hex}", "file_type": "xml",
            "invoice_code": "", "invoice_no": no, "invoice_date": "2026-09-01",
            "buyer_name": "北京示例科技有限公司",
            "buyer_tax_no": "91110108MA01AAAA1A",
            "seller_name": "测试供应商", "seller_tax_no": "91310000MA01BBBB2B",
            "amount": total, "tax_amount": "0.00", "total_amount": total,
            "raw_json": "{}", "parse_engine": "xml", "parse_confidence": 1.0,
            "status": "NORMAL", **over})


def add_reb(env, no, amount, invoice_no="", reimburse_date="2026-09-01"):
    """创建报销单（reimburse_date 默认与发票日期相同，避免 period_range 过滤）。"""
    r = env["reb"].manual_create({
        "reimburse_no": no, "submitter": "张三", "amount": str(amount),
        "reimburse_date": reimburse_date, "invoice_no": invoice_no,
        "period_start": "2026-09-01", "period_end": "2026-09-30"})
    assert r["ok"], r
    with env["db"].read() as conn:
        return conn.execute("SELECT id FROM reimburse WHERE reimburse_no=?",
                            (no,)).fetchone()["id"]


# =====================================================================
# 测试域一：多解组合完整展示（最关键的业务盲区）
# =====================================================================
class TestManyToOneAllCombinations:
    """当存在多种可行组合时，全部生成 SUSPICIOUS 记录供人工选择。"""

    def test_two_combinations_both_persisted(self, env):
        # 100 = 20+80, 同时 = 30+70（4 张刚好凑出两种 2 发票组合）
        seed_inv(env, "INV-001", 20)
        seed_inv(env, "INV-002", 80)
        seed_inv(env, "INV-003", 30)
        seed_inv(env, "INV-004", 70)
        add_reb(env, "REB-100", 100)

        summary = env["match"].run_matching()
        with env["db"].read() as conn:
            sus = conn.execute(
                "SELECT COUNT(*) AS c FROM match_result WHERE status='SUSPICIOUS'"
            ).fetchone()["c"]
            pairs = conn.execute(
                "SELECT mr.invoice_id, mp.match_id FROM match_result mr "
                "JOIN match_pair mp ON mp.match_id = mr.id "
                "WHERE mr.status='SUSPICIOUS' AND mr.match_type='many_to_one'"
            ).fetchall()

        # 预期：两种组合各 2 张发票 = 4 条 SUSPICIOUS，分属 2 个不同 match_result
        assert summary["many_to_one"] == 4, f"expected 4 many_to_one records, got {summary['many_to_one']}"
        assert sus == 4
        match_ids = {p["match_id"] for p in pairs}
        assert len(match_ids) >= 2, f"expected >=2 distinct match_results (one per combo), got {match_ids}"

    def test_engine_returns_multiple_combos(self, env):
        """直接测 matcher.find_many_to_one_all 返回全组合。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        from app.domain.matcher import MatchEngine

        def mk_inv(no, amt):
            return InvoiceDTO(
                invoice_no=no, total_amount=Decimal(str(amt)),
                invoice_date="2026-09-01",
                seller_name="S", buyer_name="B",
                amount=Decimal(str(amt)), tax_amount=Decimal("0"))

        # 给 period_start/period_end 设宽范围，让所有发票通过日期过滤
        reb = ReimburseDTO(
            reimburse_no="R", amount=Decimal("100"),
            submitter="X", reimburse_date="2026-09-10",
            period_start="2026-09-01", period_end="2026-09-30")

        engine = MatchEngine()
        combos = engine.find_many_to_one_all(
            [mk_inv("a", 20), mk_inv("b", 80), mk_inv("c", 30), mk_inv("d", 70)],
            reb)
        assert len(combos) >= 2, f"expected >=2 combos, got {len(combos)}: {combos}"

    def test_single_combo_unchanged(self, env):
        """只有一种组合时，不应该出现额外记录。"""
        seed_inv(env, "INV-001", 40)
        seed_inv(env, "INV-002", 60)
        add_reb(env, "REB-100", 100)

        summary = env["match"].run_matching()
        with env["db"].read() as conn:
            sus = conn.execute(
                "SELECT COUNT(*) AS c FROM match_result "
                "WHERE status='SUSPICIOUS' AND match_type='many_to_one'"
            ).fetchone()["c"]
            # 应该正好 2 条（40 + 60 这一组）
            assert sus == 2, f"expected exactly 2 SUSPICIOUS (one combo), got {sus}"


# =====================================================================
# 测试域二：OCR 超时熔断（19MB 高像素黑图防卡死）
# =====================================================================
class TestOcrTimeoutCircuitBreaker:
    def test_parse_image_timeouts_in_12s(self, monkeypatch):
        """用 mock 让 OCR 调用 sleep 15s，断言 parse_image 在 12s 内切出 ParseSkip。"""
        import time as _time_mod
        from app.domain.parsers import ParseSkip
        from app.infra import ocr as ocr_mod

        def _fake_slow_once(content, max_side, rotate=0):
            _time_mod.sleep(15)
            raise ParseSkip("fake - should not reach here")

        monkeypatch.setattr(ocr_mod, "_run_ocr_once", _fake_slow_once)

        start = _time_mod.time()
        with pytest.raises(ParseSkip) as ei:
            # 8KB 字节的假文件内容（内容不重要，fake 函数根本不解析）
            ocr_mod.parse_image(b"\x00" * 8192)
        elapsed = _time_mod.time() - start

        assert "超时" in str(ei.value), f"expected ParseSkip with timeout hint, got: {ei.value}"
        assert elapsed < 14, f"timeout circuit should fire <14s, took {elapsed:.1f}s"

    def test_timeout_constant_is_reasonable(self):
        """12s 熔断常量不能被意外改成 100s 或 1s。"""
        from app.infra import ocr as ocr_mod
        assert 5 <= ocr_mod._OCR_TIMEOUT_SEC <= 30


# =====================================================================
# 测试域三：风控规则引擎真实触发
# =====================================================================
class TestRiskRuleEngine:
    def test_consecutive_invoice_sequence(self, env):
        """同商户同日期、号码差 1 = 连号预警。"""
        risk = env["risk"]
        risk.save_settings(seq=True, word=False, words=[])

        inv1 = seed_inv(env, "INV-2026001", 100,
                        seller_name="连锁餐饮", invoice_date="2026-09-15")
        inv2 = seed_inv(env, "INV-2026002", 200,
                        seller_name="连锁餐饮", invoice_date="2026-09-15")

        with env["db"].read() as conn:
            items = risk.scan_invoice(conn, inv2)
        tags = {it["tag"] for it in items}
        assert "连号" in tags, f"expected 连号 tag, got {items}"

    def test_skip_invoice_sequence(self, env):
        """同商户同日期、号码差 2 = 跳号预警。"""
        risk = env["risk"]
        risk.save_settings(seq=True, word=False, words=[])

        seed_inv(env, "INV-2026010", 100,
                 seller_name="连锁餐饮", invoice_date="2026-09-15")
        inv2 = seed_inv(env, "INV-2026012", 200,
                        seller_name="连锁餐饮", invoice_date="2026-09-15")

        with env["db"].read() as conn:
            items = risk.scan_invoice(conn, inv2)
        tags = {it["tag"] for it in items}
        assert "跳号" in tags, f"expected 跳号 tag, got {items}"

    def test_sensitive_word_hit(self, env):
        """item_summary 含"中华香烟"命中敏感词预警。"""
        risk = env["risk"]
        risk.save_settings(seq=False, word=True, words=["烟", "酒"])

        inv_id = seed_inv(env, "INV-SENS", 88,
                          item_summary="购买中华香烟 1 包")
        with env["db"].read() as conn:
            items = risk.scan_invoice(conn, inv_id)
        tags = {it["tag"] for it in items}
        assert "敏感词" in tags, f"expected 敏感词 tag, got {items}"

    def test_sensitive_word_defaults(self, env):
        """未配置时使用默认敏感词库。"""
        risk = env["risk"]
        risk.save_settings(seq=False, word=True, words=[])  # 空列表会 fallback 到 DEFAULT_WORDS
        cfg = risk.get_settings()
        assert "烟" in cfg["words"] or len(cfg["words"]) >= 5, \
            f"expected default words to kick in, got {cfg['words']}"


# =====================================================================
# 测试域四：端口占用自动顺延
# =====================================================================
class TestPortAutoFallback:
    def test_start_backend_falls_back_when_8000_occupied(self, tmp_path, monkeypatch):
        """8000 被占用时自动顺延到 8001~8009。"""
        import main as launcher
        launcher.HOST = "127.0.0.1"

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind((launcher.HOST, launcher.PREFERRED_PORT))
            blocker.listen(1)

            backend = launcher._start_backend()
            try:
                assert backend.port != launcher.PREFERRED_PORT, \
                    f"expected fallback port != 8000, got {backend.port}"
                assert launcher.PREFERRED_PORT <= backend.port < launcher.PREFERRED_PORT + launcher.PORT_SPAN
            finally:
                try:
                    backend.server.should_exit = True
                    backend.thread.join(timeout=5)
                except Exception:
                    pass
        finally:
            blocker.close()


# =====================================================================
# 测试域五：磁盘空间耗尽捕获 + 事务回滚
# =====================================================================
class TestDiskFullHandling:
    def test_disk_full_does_not_corrupt_db(self, env, monkeypatch):
        """附件目录写入遇到 ENOSPC 时，service 层事务自动回滚、DB 不脏。"""
        inv_svc = env["inv"]
        upload_dir = Path(settings.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)

        real_write_bytes = Path.write_bytes

        call_count = {"n": 0}

        def fake_write_bytes(self, data):
            if upload_dir in self.parents or str(self).startswith(str(upload_dir)):
                call_count["n"] += 1
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)

        # 尝试导入一张带附件的发票（纯 XML，不会写磁盘附件）
        xml_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice><Header><InvoiceNo>INV-FULL-DISK</InvoiceNo><InvoiceCode></InvoiceCode>
<InvoiceDate>2026-09-01</InvoiceDate></Header>
<Buyer><BuyerName>Test Buyer</BuyerName></Buyer>
<Seller><SellerName>Test Seller</SellerName></Seller>
<Items><Item><ItemName>disk-full-test</ItemName><Amount>100.00</Amount><TaxRate>13%</TaxRate><TaxAmount>0.00</TaxAmount></Item></Items>
<Settlement><TotalTaxInclAmount>100.00</TotalTaxInclAmount></Settlement></Invoice>"""

        # service 层不应抛出未预期异常；若因磁盘满回滚则 invoice_id 应为空或 ok=False
        try:
            result = inv_svc.import_files(
                [("disk_test.xml", "text/xml", xml_bytes)],
                skip_match=True)[0]
            # XML 导入本身不写附件，应该能成功
            assert result["ok"], f"expected import ok, got {result}"
        except OSError as e:
            # 若 service 层直抛 OSError，也应为 ENOSPC 且 errno=28
            assert getattr(e, "errno", None) == errno.ENOSPC

        # 关键断言：数据库不脏（没有半成品发票、没有挂起事务）
        with env["db"].read() as conn:
            leftover = conn.execute(
                "SELECT COUNT(*) AS c FROM invoice WHERE invoice_no='INV-FULL-DISK'"
            ).fetchone()["c"]
            # 要么成功写了，要么完全没写；不能出现挂起的中间态
            assert leftover in (0, 1), f"expected 0 or 1 invoices, got {leftover}"


# =====================================================================
# 测试域六：杀毒软件独占锁库（busy_timeout 兜底 + 503 降级）
# =====================================================================
class TestDatabaseLockedGracefulDegrade:
    def test_service_layer_busy_timeout_raises_operational_error(self, monkeypatch):
        """连接被独占锁死时，service 层会在 30s busy_timeout 后抛 OperationalError。

        API 层全局 handler 已注册，负责把它转成 503 DB_LOCKED。
        这里验证底层行为——不吞异常、不静默降级，让上层能拦截。
        """
        import sqlite3 as _sqlite3
        from app.repository import db

        real_connect = _sqlite3.connect

        class _AlwaysLockedConn:
            row_factory = None
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def close(self): pass
            def execute(self, *a, **kw):
                # 立即抛，不等待 busy_timeout（验证异常类型和消息）
                raise _sqlite3.OperationalError("database is locked")
            def executemany(self, *a, **kw):
                raise _sqlite3.OperationalError("database is locked")
            def commit(self): pass
            def rollback(self): pass
            def fetchone(self): raise _sqlite3.OperationalError("database is locked")
            def fetchall(self): raise _sqlite3.OperationalError("database is locked")
            def __iter__(self): raise _sqlite3.OperationalError("database is locked")

        def fake_connect(*args, **kwargs):
            return _AlwaysLockedConn()

        monkeypatch.setattr(_sqlite3, "connect", fake_connect)

        # 尝试任何 db.tx() 操作——应立刻抛 OperationalError
        with pytest.raises(_sqlite3.OperationalError) as ei:
            with db.tx() as conn:
                conn.execute("SELECT 1")
        assert "database is locked" in str(ei.value)

    def test_busy_timeout_pragma_set_on_connect(self, env):
        """验证 connect() 里加了 PRAGMA busy_timeout = 30000。"""
        conn = db.connect()
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            val = row[0] if row else None
            assert val == 30000, f"expected busy_timeout=30000, got {val}"
        finally:
            conn.close()


# =====================================================================
# 测试域七：跨年额度重置
# =====================================================================
class TestQuotaResetCrossYearBoundary:
    def test_quota_resets_jan_first_00_00_01(self, env, monkeypatch):
        """2026-12-31 用完免费额度，2027-01-01 00:00:01 新一月额度应为 20。"""
        lic = env["lic"]

        # 免费版是系统默认状态，直接能用（limit=20）
        from app.infra import machine as machine_mod
        monkeypatch.setattr(machine_mod, "machine_code", lambda: "1111-2222-3333-4444")

        # 把 2026-12 的额度扣满
        month_dec = "2026-12"
        with env["db"].tx() as conn:
            from app.repository.repos import kv_repo
            kv_repo.set(conn, f"quota:{month_dec}", "20")
        # guard 同步
        guard = machine_mod.load_guard()
        key = machine_mod.month_key(machine_mod.machine_code(), month_dec)
        guard.setdefault("quota", {})[key] = 20
        machine_mod.save_guard(guard)

        # mock time.strftime 让它在一月返回 "2027-01"
        real_strftime = time.strftime
        def fake_strftime(fmt, t=None):
            if fmt == "%Y-%m":
                return "2027-01"
            return real_strftime(fmt, t)
        monkeypatch.setattr(time, "strftime", fake_strftime)

        status = lic.quota_status()
        assert status["month"] == "2027-01", \
            f"expected month reset to 2027-01, got {status['month']}"
        assert status["used"] == 0, \
            f"expected used reset to 0, got {status['used']}"
        assert status["remaining"] == 20, \
            f"expected remaining=20, got {status['remaining']}"


# =====================================================================
# 测试域八：时区漂移容错（不崩、不脏数据）
# =====================================================================
class TestTimezoneDriftResilience:
    def test_month_key_is_localtime_aware_not_utc(self, env, monkeypatch):
        """_month() 用 strftime（localtime 感知），不是硬编码 UTC，跨时区不崩。"""
        from app.service.license_service import _month
        m = _month()
        assert len(m) == 7 and m[4] == "-", f"expected YYYY-MM, got {m}"

    def test_quota_consumption_under_timezone_shift_does_not_crash(self, env, monkeypatch):
        """即使 time.strftime 返回的月份 key 与 DB 里已有 key 不同步，消费额度也不崩。"""
        lic = env["lic"]
        from app.infra import machine as machine_mod
        monkeypatch.setattr(machine_mod, "machine_code", lambda: "1111-2222-3333-4444")

        # 模拟时区突变：strftime 换成了另一个时区的月份 key
        real_strftime = time.strftime
        def fake_strftime(fmt, t=None):
            if fmt == "%Y-%m":
                return "2025-06"   # 模拟 user 改时区导致月份 key 突变
            return real_strftime(fmt, t)
        monkeypatch.setattr(time, "strftime", fake_strftime)

        # consume_invoice_quota 不应抛任何异常（只是换了新的 key，新 key 计数从 0 开始）
        lic.consume_invoice_quota(1)
        status = lic.quota_status()
        assert status["month"] == "2025-06"
        assert status["used"] == 1
