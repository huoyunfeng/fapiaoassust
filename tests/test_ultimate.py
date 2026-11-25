"""终极全链路鲁棒性与灾难演练（独立临时库，不触碰正式数据）。

六大测试域：
1. 商业化授权、账户与防绕过
2. 文件解析、系统级干扰与数据清洗
3. 匹配引擎精度、并发与组合
4. 灾备、存储与交接
5. 导出、注入与审计
6. 前端 E2E（见 tests/test_e2e_ultimate.py）

运行：python -m pytest tests/test_ultimate.py -q
"""
import io
import json
import os
import re
import sys
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
import uuid
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openpyxl import load_workbook

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db


# =====================================================================
# 夹具
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
                             report_service)
    from app.repository.repos import invoice_repo, reimburse_repo, match_repo
    init_schema()
    return {"db": db, "key": key, "tmp": tmp_path,
            "inv": invoice_service, "reb": reimburse_service,
            "match": match_service, "cat": category_service,
            "lic": license_service, "backup": backup_service,
            "handover": handover_service, "report": report_service,
            "recycle": recycle_service, "audit": audit_service,
            "invoice_repo": invoice_repo, "reimburse_repo": reimburse_repo,
            "match_repo": match_repo}


def xml_invoice(no, total, date="2026-09-01", seller="测试供应商"):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice><Header><InvoiceNo>{no}</InvoiceNo><InvoiceCode></InvoiceCode>
<InvoiceDate>{date}</InvoiceDate></Header>
<Buyer><BuyerName>北京示例科技有限公司</BuyerName>
<BuyerTaxID>91110108MA01AAAA1A</BuyerTaxID></Buyer>
<Seller><SellerName>{seller}</SellerName>
<SellerTaxID>91310000MA01BBBB2B</SellerTaxID></Seller>
<Items><Item><ItemName>*测试*项目</ItemName><Amount>{total}</Amount>
<TaxRate>13%</TaxRate><TaxAmount>0.00</TaxAmount></Item></Items>
<Settlement><TotalTaxInclAmount>{total}</TotalTaxInclAmount></Settlement></Invoice>""".encode("utf-8")


def import_inv(env, no, total, name=None, date="2026-09-01"):
    r = env["inv"].import_files(
        [(name or f"{no or 'nonum'}.xml", "text/xml",
          xml_invoice(no, total, date=date))],
        skip_match=True)[0]
    assert r["ok"], r
    return r["invoice_id"]


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


def add_reb(env, no, amount, invoice_no="", submitter="张三"):
    r = env["reb"].manual_create({
        "reimburse_no": no, "submitter": submitter, "amount": str(amount),
        "reimburse_date": "2026-09-10", "invoice_no": invoice_no})
    assert r["ok"], r
    with env["db"].read() as conn:
        return conn.execute("SELECT id FROM reimburse WHERE reimburse_no=?",
                            (no,)).fetchone()["id"]


def one(env, sql, params=()):
    with env["db"].read() as conn:
        return conn.execute(sql, params).fetchone()


def row(env, sql, params=()):
    with env["db"].read() as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def sign_code(key, **payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    import base64
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"{b64(raw)}.{b64(key.sign(raw))}"


def activate(env, machine="1111-2222-3333-4444", exp_days=365, plan="enterprise"):
    code = sign_code(env["key"], machine_code=machine, plan=plan, role="master",
                     exp=int(time.time()) + exp_days * 86400,
                     duration_days=exp_days, sub_limit=3, order_id="ORD-ULT")
    return env["lic"].apply_license_code(code)


# =====================================================================
# 测试域一：授权、账户与防绕过
# =====================================================================
class TestDomain1License:

    def test_payment_ignores_client_amount_and_plan_whitelist(self, env, monkeypatch):
        captured = {}

        def fake_post(path, payload):
            captured["path"] = path
            captured["payload"] = payload
            return {"ok": True, "trade_order_id": "T-1", "amount": 49900,
                    "qr_url": "https://pay.example/qr"}

        monkeypatch.setattr(env["lic"], "_post", fake_post)
        out = env["lic"].payment_create("enterprise")
        assert out["amount"] == 49900
        assert "amount" not in captured["payload"]
        # 只传套餐代码/机器码/核心指纹，金额与有效期由云端字典决定
        assert set(captured["payload"]) == {"plan_type", "machine_code", "core_fp", "sku"}
        assert captured["payload"]["sku"] == "ent_lifetime"

        for bad in ("free", "hacker", ""):
            with pytest.raises(env["lic"].CloudError):
                env["lic"].payment_create(bad)

    def test_license_bound_to_machine(self, env):
        with pytest.raises(env["lic"].LicenseError):
            activate(env, machine="9999-8888-7777-6666")
        assert env["lic"].status()["activated"] is False

    def test_clock_rollback_degrades_license(self, env):
        activate(env)
        assert env["lic"].status()["activated"] is True
        guard = machine_mod.load_guard()
        guard["last_run_at"] = int(time.time()) + 30 * 86400
        machine_mod.save_guard(guard)
        st = env["lic"].status()
        assert st["activated"] is False
        assert "时间" in (st.get("error") or "")
        assert env["lic"].features()["matching"] is False

    def test_expired_license_is_readonly_not_free(self, env):
        # 模拟「曾激活、现已到期」：先把付费历史写入登记簿，再写入过期但仍合法的年费码
        env["lic"]._mark_paid({"plan": "enterprise", "lifetime": False})
        expired = sign_code(env["key"], machine_code="1111-2222-3333-4444",
                            plan="enterprise", role="master",
                            exp=int(time.time()) - 3600, duration_days=365,
                            sub_limit=3, order_id="ORD-EXP")
        env["lic"]._save_code(expired)
        env["lic"]._READONLY_CACHE.update({"at": 0.0, "value": False})
        assert env["lic"].readonly_mode() is True
        st = env["lic"].status()
        assert st["expired"] is True
        # 历史数据仍可读
        inv = import_inv(env, "RO-1", "10.00")
        assert env["inv"].invoice_detail(inv) is not None

    def test_free_plan_quota_and_enterprise_unlimited(self, env):
        assert env["lic"].quota_status()["limit"] == int(settings.trial_invoice_quota)
        activate(env)
        assert env["lic"].quota_status()["limit"] is None


# =====================================================================
# 测试域二：文件解析、系统干扰与清洗
# =====================================================================
class TestDomain2Files:

    def test_path_traversal_and_special_chars_are_sanitized(self, env):
        bad_name = "../../../etc/passwd.xml"
        iid = import_inv(env, "TRAV-1", "10.00", name=bad_name)
        r = row(env, "SELECT source_path FROM invoice WHERE id=?", (iid,))
        saved = (settings.upload_dir / r["source_path"]).resolve() \
            if not Path(r["source_path"]).is_absolute() else Path(r["source_path"])
        root = settings.upload_dir.resolve()
        assert root == saved or root in saved.parents
        assert ".." not in saved.parts

        iid2 = import_inv(env, "SPECIAL-1", "11.00", name="[#&*].xml")
        r2 = row(env, "SELECT source_path FROM invoice WHERE id=?", (iid2,))
        assert Path(r2["source_path"]).name.endswith(".xml")
        assert (settings.upload_dir / r2["source_path"]).exists()

    def test_locked_file_is_friendly_and_batch_continues(self, env, monkeypatch):
        real_save = env["inv"]._file_store.save
        calls = {"n": 0}

        def flaky_save(name, content):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("[WinError 32] 另一个程序正在使用此文件")
            return real_save(name, content)

        monkeypatch.setattr(env["inv"]._file_store, "save", flaky_save)
        results = env["inv"].import_files([
            ("locked.xml", "text/xml", xml_invoice("LOCK-1", "10.00")),
            ("ok.xml", "text/xml", xml_invoice("LOCK-2", "20.00")),
        ], skip_match=True)
        assert results[0]["ok"] is False
        assert "占用" in results[0]["message"]
        assert results[1]["ok"] is True
        assert row(env, "SELECT 1 FROM invoice WHERE invoice_no='LOCK-2'") is not None

    def test_50mb_image_is_rejected_before_ocr(self, env):
        """50MB 黑图在 API 大小闸门处被立即拒绝，不进入 OCR。"""
        import asyncio
        from types import SimpleNamespace
        from starlette.datastructures import Headers, UploadFile
        from app.web import api as api_mod

        blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (50 * 1024 * 1024)
        req = SimpleNamespace(state=SimpleNamespace(
            user={"id": 1, "username": "admin", "role": "admin"}))
        upload = UploadFile(io.BytesIO(blob), filename="black.png",
                            headers=Headers({"content-type": "image/png"}))
        t0 = time.perf_counter()
        resp = asyncio.run(api_mod.upload_invoices(req, [upload], 1))
        cost = time.perf_counter() - t0
        if isinstance(resp, dict):
            body = resp["data"]
        else:
            body = json.loads(resp.body)
        assert body[0]["ok"] is False
        assert "20MB" in body[0]["message"] or "上限" in body[0]["message"]
        assert cost < 3, f"50MB 图片处理耗时 {cost:.1f}s，疑似进入解析"
        assert not [p for p in settings.upload_dir.rglob("*") if p.is_file()] \
            if settings.upload_dir.exists() else True

    def test_file_hash_and_invoice_number_dedup(self, env):
        blob = xml_invoice("DEDUP-1", "100.00")
        r1 = env["inv"].import_files([("a.xml", "", blob)], skip_match=True)[0]
        assert r1["ok"] and not r1.get("skipped")
        r2 = env["inv"].import_files([("b.xml", "", blob)], skip_match=True)[0]
        assert r2["ok"] and r2.get("skipped") is True
        assert r2["invoice_id"] == r1["invoice_id"]

        # 内容 99% 相同、单号相同但金额变化 -> 行级去重标记 DUPLICATE
        altered = xml_invoice("DEDUP-1", "99.00")
        r3 = env["inv"].import_files([("c.xml", "", altered)], skip_match=True)[0]
        assert r3["ok"] is True
        assert r3["status"] == "DUPLICATE"
        with env["db"].read() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, status, invoice_no, raw_json FROM invoice")]
        assert len(rows) == 2, rows
        assert sum(1 for r in rows if r["status"] == "NORMAL") == 1
        dup = [r for r in rows if r["status"] == "DUPLICATE"]
        assert len(dup) == 1
        # 重复票的号码只保留在 raw_json，避免撞唯一索引（既定设计）
        assert "DEDUP-1" in dup[0]["raw_json"]


# =====================================================================
# 测试域三：匹配精度、并发与组合
# =====================================================================
class TestDomain3Match:

    def test_decimal_precision_no_float_noise(self, env):
        inv = import_inv(env, "DEC-1", "0.30", )  # 0.1 + 0.2 的十进制结果
        reb = add_reb(env, "BX-DEC-1", "0.30")
        r = env["match"].manual_match(inv, reb)
        assert r["ok"], r
        rep = env["report"].build_report()
        text = json.dumps(rep, ensure_ascii=False, default=str)
        assert "0.30000000000000004" not in text
        assert one(env, "SELECT matched_amount FROM invoice WHERE id=?",
                   (inv,))["matched_amount"] == "0.30"

    def test_concurrent_bind_only_one_success(self, env):
        inv = import_inv(env, "RACE-ULT", "88.00")
        rebs = [add_reb(env, f"BX-RACE-ULT-{i}", "88.00") for i in range(10)]
        barrier = threading.Barrier(10)
        out = [None] * 10

        def run(i):
            barrier.wait()
            out[i] = env["match"].manual_match(inv, rebs[i])

        ts = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert sum(1 for r in out if isinstance(r, dict) and r.get("ok")) == 1
        assert one(env, "SELECT COUNT(*) AS n FROM match_result WHERE invoice_id=? "
                        "AND status IN ('MATCHED','CONFIRMED')", (inv,))["n"] == 1

    def test_negative_red_invoice_does_not_flip_ledger(self, env):
        inv = seed_inv(env, "RED-1", "-100.00")
        reb = add_reb(env, "BX-RED-1", "100.00")
        env["match"].run_matching()
        assert one(env, "SELECT status FROM invoice WHERE id=?", (inv,))["status"] == "NORMAL"
        assert one(env, "SELECT COUNT(*) AS n FROM match_result WHERE invoice_id=? "
                        "AND status IN ('MATCHED','CONFIRMED')", (inv,))["n"] == 0
        assert env["report"].build_report() is not None

    def _combo_pair_sum(self, env, reimburse_id):
        from decimal import Decimal
        with env["db"].read() as conn:
            pairs = conn.execute(
                "SELECT i.total_amount FROM match_pair p "
                "JOIN match_result m ON m.id=p.match_id "
                "JOIN invoice i ON i.id=p.invoice_id "
                "WHERE p.reimburse_id=? AND m.status='SUSPICIOUS'",
                (reimburse_id,)).fetchall()
        return len(pairs), sum((Decimal(str(p["total_amount"])) for p in pairs),
                               Decimal("0"))

    def test_many_to_one_picks_exact_combination_suspicious(self, env):
        for i, a in enumerate([30, 40, 50, 70]):
            import_inv(env, f"CMB-{i}", f"{a}.00", date="2026-09-10")
        reb = add_reb(env, "BX-CMB-1", "100.00")
        env["match"].run_matching()
        count, total = self._combo_pair_sum(env, reb)
        assert count >= 2, "未生成汇总匹配记录"
        assert total == 100, f"组合金额不等于报销金额: {total}"
        with env["db"].read() as conn:
            statuses = [r["status"] for r in conn.execute(
                "SELECT DISTINCT m.status FROM match_result m "
                "JOIN match_pair p ON p.match_id=m.id WHERE p.reimburse_id=?",
                (reb,)).fetchall()]
        assert statuses and all(s == "SUSPICIOUS" for s in statuses)

    def test_many_to_one_multi_solution_requires_manual_confirm(self, env):
        for i, a in enumerate([20, 30, 50, 70, 80]):
            import_inv(env, f"MS-{i}", f"{a}.00", date="2026-09-10")
        reb = add_reb(env, "BX-MS-1", "100.00")
        env["match"].run_matching()
        with env["db"].read() as conn:
            statuses = [r["status"] for r in conn.execute(
                "SELECT DISTINCT m.status FROM match_result m "
                "JOIN match_pair p ON p.match_id=m.id WHERE p.reimburse_id=?",
                (reb,)).fetchall()]
        assert statuses, "多解组合未进入人工池"
        assert "MATCHED" not in statuses, "多解被静默自动通过"
        assert all(s == "SUSPICIOUS" for s in statuses)

    def test_one_to_many_category_change_only_creates_conflict(self, env):
        a = import_inv(env, "M2O-A", "60.00")
        b = import_inv(env, "M2O-B", "40.00")
        reb = add_reb(env, "BX-M2O", "100.00")
        assert env["match"].manual_batch_bind([a, b], reb)["ok"]
        before = row(env, "SELECT amount, submitter FROM reimburse WHERE id=?", (reb,))
        with env["db"].tx() as conn:
            conn.execute("INSERT OR REPLACE INTO reimburse_category"
                         "(reimburse_id, category, source, version) VALUES(?,?,?,0)",
                         (reb, "办公费", "manual"))
        assert env["inv"].set_invoice_category(a, "差旅费", sync_reimburse=False)["ok"]
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is not None
        after = row(env, "SELECT amount, submitter FROM reimburse WHERE id=?", (reb,))
        assert before == after, "修改单张发票分类改动了报销单总账"
        assert one(env, "SELECT COUNT(DISTINCT m.id) AS n FROM match_result m "
                        "JOIN match_pair p ON p.match_id=m.id WHERE p.reimburse_id=? "
                        "AND m.status IN ('MATCHED','CONFIRMED')", (reb,))["n"] == 2


# =====================================================================
# 测试域四：灾备、存储与交接
# =====================================================================
class TestDomain4Disaster:

    def test_zip_bomb_declared_size_and_member_count(self, env, monkeypatch):
        payload = {"kind": "full-backup", "app_version": "0.0.1"}
        big = b"\x00" * (100 * 1024 * 1024)   # 100MB 零，压缩后极小
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(payload))
            zf.writestr("fapiao.db", big)
        monkeypatch.setattr(env["backup"], "MAX_ZIP_UNCOMPRESSED_BYTES", 50 * 1024 ** 2)
        r = env["backup"].restore_backup(buf.getvalue())
        assert r.get("ok") is False and "解压" in r.get("message", "")

        monkeypatch.setattr(env["backup"], "MAX_ZIP_UNCOMPRESSED_BYTES", 20 * 1024 ** 3)
        monkeypatch.setattr(env["backup"], "MAX_ZIP_MEMBERS", 50)
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as zf:
            zf.writestr("manifest.json", json.dumps(payload))
            for i in range(100):
                zf.writestr(f"uploads/{i}.txt", b"x")
        r2 = env["backup"].restore_backup(buf2.getvalue())
        assert r2.get("ok") is False and "条目" in r2.get("message", "")

    def test_restore_interrupted_midway_rolls_back(self, env, monkeypatch):
        inv = import_inv(env, "ROLLBACK-1", "10.00")
        pack = env["backup"].create_backup()
        content = Path(pack["zip_path"]).read_bytes()

        real_copy = env["backup"].shutil.copyfileobj
        calls = {"n": 0}

        def boom(src, dst, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("模拟磁盘写满 / 断电")
            return real_copy(src, dst, *a, **kw)

        monkeypatch.setattr(env["backup"].shutil, "copyfileobj", boom)
        r = env["backup"].restore_backup(content)
        assert r.get("ok") is False and "回滚" in r.get("message", "")
        assert env["inv"].invoice_detail(inv) is not None

    def test_recycle_auto_purge_removes_record_and_file(self, env):
        iid = import_inv(env, "PURGE-1", "12.00")
        src = row(env, "SELECT source_path FROM invoice WHERE id=?", (iid,))["source_path"]
        path = (settings.upload_dir / src) if not Path(src).is_absolute() else Path(src)
        assert path.exists()
        assert env["inv"].delete_invoice(iid)["ok"]
        with env["db"].tx() as conn:
            conn.execute("UPDATE invoice SET deleted_at=datetime('now','-40 day') "
                         "WHERE id=?", (iid,))
        env["recycle"].set_auto_clean_days(30)
        r = env["recycle"].auto_cleanup()
        assert r["purged"] >= 1
        assert one(env, "SELECT 1 FROM invoice WHERE id=?", (iid,)) is None
        assert not path.exists(), "回收站清理后附件仍占用磁盘"

    def test_handover_downgrade_and_idempotent_import(self, env):
        import_inv(env, "HV-OLD-1", "15.00")
        add_reb(env, "BX-HV-OLD-1", "15.00", invoice_no="HV-OLD-1")
        exported = env["handover"].export_handover()
        content = Path(exported["zip_path"]).read_bytes()

        # 模拟旧版本：去掉后来新增的可选列
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            payload = json.loads(zf.read("handover.json"))
        for r in payload["invoices"]:
            for k in ("owner_user_id", "risk_note", "risk_flag",
                      "buyer_tax_no", "seller_tax_no", "raw_json"):
                r.pop(k, None)
        for r in payload["reimburses"]:
            for k in ("owner_user_id", "batch_id", "remark"):
                r.pop(k, None)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("handover.json", json.dumps(payload, ensure_ascii=False))
        old_content = buf.getvalue()

        with env["db"].tx() as conn:
            conn.execute("DELETE FROM match_result")
            conn.execute("DELETE FROM invoice_category")
            conn.execute("DELETE FROM invoice")
            conn.execute("DELETE FROM reimburse_category")
            conn.execute("DELETE FROM category_conflict")
            conn.execute("DELETE FROM reimburse")
        first = env["handover"].import_handover(old_content)
        assert first["ok"], first
        assert first["invoices_added"] == 1
        second = env["handover"].import_handover(old_content)
        assert second["ok"] and second["invoices_added"] == 0


# =====================================================================
# 测试域五：导出、注入与审计
# =====================================================================
class TestDomain5ExportAudit:

    def test_excel_formula_injection_is_neutralized(self, env):
        payload = "=cmd|' /C calc'!A0"
        env["inv"].manual_create({
            "invoice_no": "XLS-INJ-1", "invoice_date": "2026-09-01",
            "buyer_name": payload, "seller_name": payload,
            "amount": "10.00", "tax_amount": "0.00", "total_amount": "10.00",
            "item_summary": payload,
        })
        chunks = list(env["inv"].export_excel_stream())
        wb = load_workbook(io.BytesIO(b"".join(chunks)), read_only=True)
        ws = wb.active
        values = [c for r in ws.iter_rows(min_row=2, max_row=2) for c in r
                  if isinstance(c.value, str) and "cmd" in c.value]
        wb.close()
        assert values, "导出未包含注入载荷"
        assert all(v.value.startswith("'") for v in values), \
            f"公式注入未转义: {[v.value for v in values]}"

    def test_export_50k_streaming_memory(self, env):
        n = 50000
        with env["db"].tx() as conn:
            conn.executemany(
                "INSERT INTO invoice (file_hash, file_type, invoice_no, invoice_date,"
                " amount, tax_amount, total_amount, status, parse_engine,"
                " parse_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(f"h{i}", "xml", f"EX{i:08d}", "2026-09-01", "1.00", "0.00",
                  "1.00", "NORMAL", "xml", 1.0) for i in range(n)])

        tracemalloc.start()
        stream = env["inv"].export_excel_stream()
        total = 0
        first = b""
        for chunk in stream:
            if not first:
                first = chunk
            total += len(chunk)
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert first.startswith(b"PK"), "导出结果不是有效 xlsx"
        assert total > 1024 * 1024
        assert peak < 800 * 1024 * 1024, f"5 万条导出峰值 {peak / 1024 / 1024:.0f}MB"
        assert not list(settings.data_dir.glob("*.xlsx")), "导出临时文件未清理"
        print(f"\n[export] 5 万条 峰值 {peak / 1024 / 1024:.0f}MB / {total / 1024:.0f}KB")

    def test_audit_100k_filtered_query_under_1s(self, env):
        n = 100000
        with env["db"].tx() as conn:
            conn.executemany(
                "INSERT INTO audit_log (action, target_type, target_label, operator, detail)"
                " VALUES (?,?,?,?,?)",
                [("category_change" if i % 3 else "purge", "invoice",
                  f"发票{i}", f"user{i % 10}", "批量写入测试") for i in range(n)])
        t0 = time.perf_counter()
        res = env["audit"].list_recent(action="category_change", keyword="发票1",
                                       date_from="2020-01-01", page=1, page_size=20)
        cost = time.perf_counter() - t0
        assert res["total"] > 0 and len(res["items"]) == 20
        assert cost < 1.0, f"10 万审计日志查询耗时 {cost:.2f}s"
        print(f"\n[audit] 10 万条联合筛选查询 {cost * 1000:.0f}ms")

    def test_500_import_match_delete_cycles_leave_no_garbage(self, env):
        tracemalloc.start()
        for i in range(500):
            iid = import_inv(env, f"LOOP-{i}", "9.00")
            add_reb(env, f"BX-LOOP-{i}", "9.00")
            env["match"].run_matching()
            assert env["inv"].delete_invoice(iid)["ok"]
            rid = one(env, "SELECT id FROM reimburse WHERE reimburse_no=?",
                      (f"BX-LOOP-{i}",))["id"]
            assert env["reb"].delete_reimburse(rid)["ok"]
            env["inv"].purge_invoices([iid])
            env["reb"].purge_reimburses([rid])
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        leftovers = [p for p in settings.upload_dir.rglob("*") if p.is_file()]
        assert not leftovers, f"500 次循环后残留附件 {len(leftovers)} 个"
        assert peak < 300 * 1024 * 1024, f"500 次循环峰值 {peak / 1024 / 1024:.0f}MB"
        print(f"\n[loop] 500 次导入-匹配-删除 峰值 {peak / 1024 / 1024:.0f}MB")
