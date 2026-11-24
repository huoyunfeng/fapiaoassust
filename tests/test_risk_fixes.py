"""本轮稳定性修复的回归测试（OCR 熔断、解析超时、事务边界、压缩包防护等）。

所有用例使用临时数据目录，不触碰正式 data/。
"""
import hashlib
import io
import json
import os
import threading
import time
import zipfile

import pytest

from app.config import settings
from app.domain.dto import ParseTask
from app.domain.parsers import (OfdInvoiceParser, ParseSkip, _call_with_timeout,
                                _parse_xml_bytes)
from app.repository import db
from app.service import match_service


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    db.init_schema()
    return tmp_path


# =====================================================================
# OCR：超时快速返回 + 不泄漏线程 + 工作线程可恢复
# =====================================================================
class TestOcrCircuitBreaker:
    def teardown_method(self):
        from app.infra import ocr as ocr_mod
        ocr_mod._reset_ocr_worker()

    def test_timeout_returns_fast_and_does_not_leak_threads(self, monkeypatch):
        from app.infra import ocr as ocr_mod

        monkeypatch.setattr(ocr_mod, "_OCR_TIMEOUT_SEC", 0.2)
        monkeypatch.setattr(ocr_mod, "_run_ocr_once",
                            lambda *a, **k: (time.sleep(2.0), ("dto", 1, None))[1])
        ocr_mod._reset_ocr_worker()

        base_threads = threading.active_count()
        start = time.time()
        with pytest.raises(ParseSkip) as ei:
            ocr_mod._run_ocr_with_timeout(b"x", 960)
        assert time.time() - start < 1.0
        assert "超时" in str(ei.value)

        # 等过熔断阈值后，后续请求在"上一张仍在跑"时快速失败，
        # 而不是再各等 0.2s 并新建线程
        time.sleep(0.25)
        for _ in range(10):
            t0 = time.time()
            with pytest.raises(ParseSkip):
                ocr_mod._run_ocr_with_timeout(b"x", 960)
            assert time.time() - t0 < 0.15

        # 线程数：只允许新增 1 个常驻 OCR 线程（不再是每次调用一个线程）
        assert threading.active_count() - base_threads <= 2

    def test_worker_recovers_after_slow_task(self, monkeypatch):
        from app.infra import ocr as ocr_mod

        state = {"calls": 0}

        def _flaky(content, max_side, rotate=0):
            state["calls"] += 1
            if state["calls"] == 1:
                time.sleep(0.8)
            return ("ok-dto", 2, None)

        monkeypatch.setattr(ocr_mod, "_run_ocr_once", _flaky)
        monkeypatch.setattr(ocr_mod, "_OCR_TIMEOUT_SEC", 0.15)
        ocr_mod._reset_ocr_worker()

        with pytest.raises(ParseSkip):
            ocr_mod._run_ocr_with_timeout(b"x", 960)
        time.sleep(1.0)   # 等慢任务结束，工作线程空出来
        assert ocr_mod._run_ocr_with_timeout(b"x", 960, timeout=1.0)[0] == "ok-dto"


# =====================================================================
# 解析超时 / XML 实体 / OFD 解压炸弹
# =====================================================================
class TestParserGuards:
    def test_call_with_timeout_returns_fast(self):
        start = time.time()
        with pytest.raises(TimeoutError):
            _call_with_timeout(time.sleep, 0.1, 1.5)
        assert time.time() - start < 0.8

    def test_call_with_timeout_normal_result(self):
        assert _call_with_timeout(lambda x: x + 1, 1.0, 41) == 42

    def test_xml_entity_expansion_rejected(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
                b'<r>&lol;</r>')
        with pytest.raises(ParseSkip) as ei:
            _parse_xml_bytes(bomb)
        assert "ENTITY" in str(ei.value).upper()

    def test_ofd_zip_size_guard(self, monkeypatch):
        monkeypatch.setattr(OfdInvoiceParser, "_MAX_UNCOMPRESSED", 10)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Doc_0/Attachs/original_invoice.xml", b"<x>" + b"a" * 200 + b"</x>")
        task = ParseTask(file_name="a.ofd", file_type="ofd", content=buf.getvalue())
        with pytest.raises(ParseSkip) as ei:
            OfdInvoiceParser().parse(task)
        assert "OFD" in str(ei.value)


# =====================================================================
# 解析不占用数据库写事务
# =====================================================================
class TestParseOutsideTransaction:
    def test_slow_parse_does_not_block_other_writes(self, env, monkeypatch):
        from app.service import invoice_service

        def _slow_parse(task):
            time.sleep(2.0)
            return invoice_service.parsers_mod.ParseResult(
                success=False, error="slow-test")

        monkeypatch.setattr(invoice_service._parser_engine, "parse", _slow_parse)
        xml = (b'<?xml version="1.0"?><Invoice><InvoiceNo>24412000</InvoiceNo>'
               b'<IssueDate>2026-01-01</IssueDate>'
               b'<TaxInclusiveTotalAmount>1.00</TaxInclusiveTotalAmount></Invoice>')

        worker = threading.Thread(
            target=lambda: invoice_service.import_files([("slow.xml", "", xml)], None, True))
        worker.start()
        time.sleep(0.3)      # 确保解析已开始
        t0 = time.time()
        with db.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES ('probe', '1')")
        elapsed = time.time() - t0
        worker.join(timeout=5)
        assert elapsed < 1.0, f"解析期间写库被阻塞 {elapsed:.2f}s"


# =====================================================================
# 匹配任务串行
# =====================================================================
class TestMatchSerialization:
    def test_busy_when_another_match_running(self, env):
        acquired = match_service._MATCH_LOCK.acquire(blocking=False)
        assert acquired
        try:
            with pytest.raises(match_service.MatchBusy):
                match_service.run_matching(wait_seconds=0)
        finally:
            match_service._MATCH_LOCK.release()
        # 锁释放后可以正常执行（空库返回摘要）
        summary = match_service.run_matching(wait_seconds=1)
        assert "auto_matched" in summary


# =====================================================================
# 备份 / 交接包：文件接口、MD5 分块、压缩包防护
# =====================================================================
class TestArchiveSafety:
    def test_payload_md5_stream_matches_manual_hash(self):
        from app.service import backup_service

        payload = os.urandom(3 * 1024 * 1024)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("uploads/a.bin", payload)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            digest = backup_service.compute_payload_md5(zf)

        manual = hashlib.md5()
        manual.update(b"uploads/a.bin")
        manual.update(b"\0")
        manual.update(payload)
        manual.update(b"\0")
        assert digest == manual.hexdigest()

    def test_restore_from_file_rejects_invalid_zip(self, env, tmp_path):
        from app.service import backup_service

        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        result = backup_service.restore_backup_file(bad)
        assert result["ok"] is False

    def test_handover_import_from_file_rejects_invalid_zip(self, env, tmp_path):
        from app.service import handover_service

        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        result = handover_service.import_handover_file(bad)
        assert result["ok"] is False

    def test_restore_from_file_roundtrip(self, env):
        """生产路径（落盘 zip）与字节接口结果一致。"""
        from app.service import backup_service

        info = backup_service.create_backup()
        zip_path = info["zip_path"]
        assert zip_path.exists()
        result = backup_service.restore_backup_file(zip_path)
        assert result["ok"] is True


# =====================================================================
# 其他防护：登录失败字典上限、附件路径兜底
# =====================================================================
class TestMiscHardening:
    def test_login_fail_dict_is_bounded(self):
        from app.service import auth_service

        for i in range(auth_service._LOGIN_FAILS_MAX + 500):
            auth_service._LOGIN_FAILS[f"u{i}"] = [1, 0]
        auth_service._prune_login_fails(time.time())
        assert len(auth_service._LOGIN_FAILS) <= auth_service._LOGIN_FAILS_MAX
        auth_service._LOGIN_FAILS.clear()

    def test_safe_upload_path_flat_and_month_layout(self, env):
        from app.service import invoice_service

        upload_dir = settings.upload_dir
        upload_dir.mkdir(parents=True, exist_ok=True)
        flat = upload_dir / "flat.pdf"
        flat.write_bytes(b"%PDF-1.4")
        month = upload_dir / "202611"
        month.mkdir()
        nested = month / "abcd_invoice.pdf"
        nested.write_bytes(b"%PDF-1.4")

        assert invoice_service._safe_upload_path("D:/old/path/flat.pdf") == flat.resolve()
        assert invoice_service._safe_upload_path("D:/old/path/abcd_invoice.pdf") == nested.resolve()
        assert invoice_service._safe_upload_path("D:/old/path/missing.pdf") is None
