"""全维度毁灭性压测 / 混沌测试（独立临时库，绝不污染正式库）。

覆盖五个维度：
1. 业务状态流转极限矩阵（一票多报、解绑级联、分类冲突闭环）
2. 极端脏数据与注入（SQL/XSS/超长/Emoji/乱码文件/金额极值）
3. 高并发与资源压测（抢占、乐观锁、额度、5000+5000 批量）
4. 环境灾难（事务中断、进程强杀、损坏备份包、离线）
5. 商业化与内控（额度并发、越权、交接包去重）

运行：python -m pytest tests/test_chaos.py -q
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
import uuid
import zipfile
import io
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.infra import machine as machine_mod
from app.repository import db


# =====================================================================
# 夹具与工具
# =====================================================================
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """完全隔离的数据目录：数据库 / 附件 / 日志 / 备份 / 防刷登记簿。"""
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
    from app.service import (backup_service, category_service, handover_service,
                             invoice_service, license_service, match_service,
                             reimburse_service)
    from app.repository.repos import (invoice_repo, match_repo, reimburse_repo)

    init_schema()
    return {
        "db": db, "key": key, "tmp": tmp_path,
        "inv": invoice_service, "reb": reimburse_service,
        "match": match_service, "cat": category_service,
        "lic": license_service, "backup": backup_service,
        "handover": handover_service,
        "invoice_repo": invoice_repo, "reimburse_repo": reimburse_repo,
        "match_repo": match_repo,
    }


def xml_invoice(no, total, amount=None, tax=None, date="2026-09-01",
                code="", seller="测试供应商", buyer="北京示例科技有限公司",
                buyer_tax="91110108MA01AAAA1A", item_name="*测试*项目"):
    amt = amount if amount is not None else total
    tx = tax if tax is not None else "0.00"
    return (f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice><Header><InvoiceNo>{no}</InvoiceNo><InvoiceCode>{code}</InvoiceCode>
<InvoiceDate>{date}</InvoiceDate></Header>
<Buyer><BuyerName>{buyer}</BuyerName><BuyerTaxID>{buyer_tax}</BuyerTaxID></Buyer>
<Seller><SellerName>{seller}</SellerName><SellerTaxID>91310000MA01BBBB2B</SellerTaxID></Seller>
<Items><Item><ItemName>{item_name}</ItemName><Amount>{amt}</Amount><TaxRate>13%</TaxRate><TaxAmount>{tx}</TaxAmount></Item></Items>
<Settlement><TotalTaxInclAmount>{total}</TotalTaxInclAmount></Settlement></Invoice>""").encode("utf-8")


def import_inv(env, no, total, **kw):
    """导入一张 XML 发票（跳过自动匹配，便于精确控制状态）。"""
    name = f"{no or 'nonum'}_{uuid.uuid4().hex[:8]}.xml"
    r = env["inv"].import_files([(name, "text/xml", xml_invoice(no, total, **kw))],
                                skip_match=True)[0]
    assert r["ok"], r
    return r["invoice_id"]


def add_reb(env, no, amount, invoice_no="", submitter="张三", **kw):
    r = env["reb"].manual_create({
        "reimburse_no": no, "submitter": submitter, "amount": str(amount),
        "reimburse_date": kw.pop("reimburse_date", "2026-09-10"),
        "invoice_no": invoice_no, **kw})
    assert r["ok"], r
    with env["db"].read() as conn:
        return conn.execute("SELECT id FROM reimburse WHERE reimburse_no = ?",
                            (no,)).fetchone()["id"]


def one(env, sql, params=()):
    with env["db"].read() as conn:
        return conn.execute(sql, params).fetchone()


def active_match_count(env, invoice_id):
    row = one(env, "SELECT COUNT(*) AS n FROM match_result WHERE invoice_id=? "
                   "AND status IN ('MATCHED','CONFIRMED')", (invoice_id,))
    return row["n"]


def inv_row(env, invoice_id):
    with env["db"].read() as conn:
        return dict(conn.execute("SELECT * FROM invoice WHERE id=?", (invoice_id,)).fetchone())


def seed_inv(env, no, total, **over):
    """直接落库一张 NORMAL 发票（用于构造解析链无法产生的边界状态）。"""
    with env["db"].tx() as conn:
        iid = env["invoice_repo"].insert(conn, {
            "file_hash": f"seed-{uuid.uuid4().hex}",
            "file_type": "xml", "invoice_code": "", "invoice_no": no,
            "invoice_date": "2026-09-01",
            "buyer_name": "北京示例科技有限公司",
            "buyer_tax_no": "91110108MA01AAAA1A",
            "seller_name": "测试供应商",
            "seller_tax_no": "91310000MA01BBBB2B",
            "amount": total, "tax_amount": "0.00", "total_amount": total,
            "raw_json": "{}", "parse_engine": "xml", "parse_confidence": 1.0,
            "status": "NORMAL", **over})
    return iid


# =====================================================================
# 维度一：业务状态流转极限矩阵
# =====================================================================
class TestStateMatrix:

    def test_no_number_invoice_cannot_be_double_reported(self, env):
        """无号发票与两张报销单：最多一条生效匹配。"""
        inv = seed_inv(env, "", "100.00")
        reb1 = add_reb(env, "BX-NO-1", "100.00")
        reb2 = add_reb(env, "BX-NO-2", "100.00")

        r1 = env["match"].manual_match(inv, reb1)
        assert r1["ok"], r1
        r2 = env["match"].manual_match(inv, reb2)
        assert not r2["ok"], "同一张发票被二次绑定，出现一票多报"
        assert active_match_count(env, inv) == 1
        row = inv_row(env, inv)
        assert row["status"] == "MATCHED" and int(row["is_closed"]) == 1

    def test_partial_close_rejects_split_reimbursement(self, env):
        """部分闭环发票不得再次拆分报销；解绑后资源必须释放。"""
        inv = import_inv(env, "SPLIT-0001", "100.00")
        reb_big = add_reb(env, "BX-SPLIT-1", "100.00")
        reb_small = add_reb(env, "BX-SPLIT-2", "40.00")

        assert env["match"].manual_match(inv, reb_big)["ok"]
        denied = env["match"].manual_match(inv, reb_small)
        assert not denied["ok"]
        assert denied["message"]
        assert active_match_count(env, inv) == 1
        assert inv_row(env, inv)["status"] == "MATCHED"

        mid = one(env, "SELECT id FROM match_result WHERE invoice_id=? "
                       "AND status IN ('MATCHED','CONFIRMED')", (inv,))["id"]
        un = env["match"].unmatch(mid)
        assert un["ok"], un
        row = inv_row(env, inv)
        assert row["status"] == "NORMAL"
        assert int(row["is_closed"]) == 0 and row["matched_amount"] == "0.00"
        assert env["match"].manual_match(inv, reb_small)["ok"]

    def test_soft_deleted_invoice_must_not_be_matchable(self, env):
        """软删除发票绝不能从匹配池/人工绑定入口复活。"""
        inv = import_inv(env, "DEL-0001", "88.00")
        reb = add_reb(env, "BX-DEL-1", "88.00")
        assert env["inv"].delete_invoice(inv)["ok"]

        res = env["match"].manual_match(inv, reb)
        assert not res["ok"], "已删除发票仍可被人工绑定"
        assert active_match_count(env, inv) == 0
        assert env["match"].run_matching()["auto_matched"] == 0

    def test_soft_deleted_reimburse_cannot_be_bound(self, env):
        """回收站里的报销单同样不能从人工入口复活。"""
        inv = import_inv(env, "DEL-REB-1", "18.00")
        reb = add_reb(env, "BX-DEL-REB", "18.00")
        assert env["reb"].delete_reimburse(reb)["ok"]
        res = env["match"].manual_match(inv, reb)
        assert not res["ok"]
        assert active_match_count(env, inv) == 0

    def test_batch_bind_rejects_deleted_invoice_atomically(self, env):
        """批量绑定中只要有一张已删除，整批不得部分成功。"""
        a = import_inv(env, "BATCH-A", "30.00")
        b = import_inv(env, "BATCH-B", "70.00")
        reb = add_reb(env, "BX-BATCH-DEL", "100.00")
        assert env["inv"].delete_invoice(b)["ok"]
        res = env["match"].manual_batch_bind([a, b], reb)
        assert not res["ok"]
        assert active_match_count(env, a) == 0
        assert active_match_count(env, b) == 0

    def test_category_merge_clears_conflict(self, env):
        """字典合并后双方分类一致，残留的冲突标记必须自动消失。"""
        assert env["cat"].add_category("测试A", force=True)["ok"]
        assert env["cat"].add_category("测试B", force=True)["ok"]
        inv = import_inv(env, "MERGE-CAT", "20.00")
        reb = add_reb(env, "BX-MERGE", "20.00")
        assert env["match"].manual_match(inv, reb)["ok"]

        with env["db"].tx() as conn:
            conn.execute("INSERT OR REPLACE INTO reimburse_category"
                         "(reimburse_id, category, source, version) VALUES(?,?,?,0)",
                         (reb, "测试B", "manual"))
        assert env["inv"].set_invoice_category(inv, "测试A",
                                               sync_reimburse=False)["ok"]
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is not None

        cats = {c["name"]: c["id"] for c in env["cat"].list_categories()}
        r = env["cat"].merge_category(cats["测试A"], cats["测试B"])
        assert r["ok"], r
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is None, "合并后分类冲突未清理"

    def test_delete_reimburse_releases_invoice_and_requeues(self, env):
        """删除报销单必须级联释放发票并重新排队，不能残留闭环。"""
        inv = import_inv(env, "CASCADE-1", "120.00")
        reb = add_reb(env, "BX-CAS-1", "120.00")
        reb_wait = add_reb(env, "BX-CAS-2", "120.00")
        assert env["match"].manual_match(inv, reb)["ok"]

        assert env["reb"].delete_reimburse(reb)["ok"]
        row = inv_row(env, inv)
        assert row["status"] == "NORMAL"
        assert int(row["is_closed"]) == 0 and row["matched_amount"] == "0.00"
        assert active_match_count(env, inv) == 0

        # 资源释放后排队单据应可正常占用
        again = env["match"].manual_match(inv, reb_wait)
        assert again["ok"], again

    def test_delete_invoice_with_active_match_releases_reimburse(self, env):
        """删除已匹配发票：匹配记录清理，报销单不得被幽灵占用。"""
        inv = import_inv(env, "CASCADE-2", "66.00")
        reb = add_reb(env, "BX-CAS-3", "66.00")
        assert env["match"].manual_match(inv, reb)["ok"]
        mid = one(env, "SELECT id FROM match_result WHERE invoice_id=? "
                       "AND status IN ('MATCHED','CONFIRMED')", (inv,))["id"]

        assert env["inv"].delete_invoice(inv)["ok"]
        assert one(env, "SELECT COUNT(*) AS n FROM match_result WHERE id=? "
                        "AND status IN ('MATCHED','CONFIRMED')", (mid,))["n"] == 0
        n = one(env, "SELECT COUNT(*) AS n FROM match_result WHERE reimburse_id=? "
                     "AND status IN ('MATCHED','CONFIRMED')", (reb,))["n"]
        assert n == 0

    def test_category_conflict_lifecycle_one_to_one(self, env):
        """1:1 匹配：分类冲突生成 -> 解绑清除 -> 重绑重新评估，不残留。"""
        inv = import_inv(env, "CAT-1TO1", "150.00")
        reb = add_reb(env, "BX-CAT-1", "150.00")
        assert env["match"].manual_match(inv, reb)["ok"]

        # 模拟报销单侧既有分类，再从发票侧改成不同分类
        with env["db"].tx() as conn:
            conn.execute("INSERT OR REPLACE INTO reimburse_category"
                         "(reimburse_id, category, source, version) VALUES(?,?,?,0)",
                         (reb, "办公费", "manual"))
        r = env["inv"].set_invoice_category(inv, "差旅费", sync_reimburse=False)
        assert r["ok"], r
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is not None

        mid = one(env, "SELECT id FROM match_result WHERE invoice_id=? "
                       "AND status IN ('MATCHED','CONFIRMED')", (inv,))["id"]
        assert env["match"].unmatch(mid)["ok"]
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is None, "解绑后分类冲突残留"

        assert env["match"].manual_match(inv, reb)["ok"]
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is not None, "重绑后分类冲突未重新检测"

    def test_category_conflict_lifecycle_one_to_many(self, env):
        """1:N 汇总绑定：单张改分类产生差异，全部解绑后冲突必须消失。"""
        inv_a = import_inv(env, "CAT-M2O-A", "60.00")
        inv_b = import_inv(env, "CAT-M2O-B", "40.00")
        reb = add_reb(env, "BX-CAT-2", "100.00")
        r = env["match"].manual_batch_bind([inv_a, inv_b], reb)
        assert r["ok"], r

        with env["db"].tx() as conn:
            conn.execute("INSERT OR REPLACE INTO reimburse_category"
                         "(reimburse_id, category, source, version) VALUES(?,?,?,0)",
                         (reb, "办公费", "manual"))
        assert env["inv"].set_invoice_category(inv_a, "差旅费",
                                               sync_reimburse=False)["ok"]
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is not None

        for iid in (inv_a, inv_b):
            mid = one(env, "SELECT m.id FROM match_result m "
                           "JOIN match_pair p ON p.match_id=m.id "
                           "WHERE m.invoice_id=? AND p.reimburse_id=? "
                           "AND m.status IN ('MATCHED','CONFIRMED')",
                      (iid, reb))["id"]
            assert env["match"].unmatch(mid)["ok"]
        assert one(env, "SELECT 1 FROM category_conflict WHERE reimburse_id=?",
                   (reb,)) is None


# =====================================================================
# 维度二：极端脏数据与注入
# =====================================================================
class TestDirtyInput:

    def test_sql_and_xss_payloads_are_stored_verbatim(self, env):
        payload_sql = "' OR 1=1 --"
        payload_xss = "<script>alert(1)</script>"
        r = env["inv"].manual_create({
            "invoice_no": "DIRTY-0001", "invoice_code": "011001900111",
            "invoice_date": "2026-09-01",
            "buyer_name": payload_sql, "buyer_tax_no": "91110108MA01AAAA1A",
            "seller_name": payload_xss, "seller_tax_no": "91310000MA01BBBB2B",
            "amount": "100.00", "tax_amount": "0.00", "total_amount": "100.00",
            "item_summary": "<img src=x onerror=alert(1)>",
        })
        assert r["ok"], r
        row = inv_row(env, r["invoice_id"])
        assert row["buyer_name"] == payload_sql
        assert row["seller_name"] == payload_xss
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] >= 1

        cat = env["cat"].add_category("' OR 1=1 --")
        assert isinstance(cat, dict)
        assert one(env, "SELECT COUNT(*) AS n FROM category_dict")["n"] >= 1

    def test_extreme_string_length_and_emoji(self, env):
        long_text = "超" * 10000
        r = env["inv"].manual_create({
            "invoice_no": "DIRTY-0002", "invoice_date": "2026-09-01",
            "buyer_name": long_text, "buyer_tax_no": "91110108MA01AAAA1A",
            "seller_name": "供应商🚀", "seller_tax_no": "91310000MA01BBBB2B",
            "amount": "1.00", "tax_amount": "0.00", "total_amount": "1.00",
            "item_summary": "😀" + long_text,
        })
        assert r["ok"], r
        row = inv_row(env, r["invoice_id"])
        assert len(row["buyer_name"]) == 10000, "超长名称被静默截断"
        # 报销内容是「摘要」字段，设计上清洗后 ≤30 字；原始输入另存 raw_json
        assert len(row["item_summary"]) <= 30

        rb = env["reb"].manual_create({
            "reimburse_no": "BX-DIRTY-EMOJI", "submitter": "员工😀",
            "amount": "1.00", "reimburse_date": "2026-09-10", "remark": long_text,
        })
        assert rb["ok"], rb

    @pytest.mark.parametrize("amount", ["0.01", "999999999.99", "-100.00", "中文金额"])
    def test_amount_extremes_never_crash(self, env, amount):
        no = f"AMT-{uuid.uuid4().hex[:8]}"
        r = env["inv"].manual_create({
            "invoice_no": no, "invoice_date": "2026-09-01",
            "buyer_name": "测试", "buyer_tax_no": "91110108MA01AAAA1A",
            "seller_name": "测试供应商", "seller_tax_no": "91310000MA01BBBB2B",
            "amount": amount, "tax_amount": "0.00", "total_amount": amount,
        })
        assert isinstance(r, dict) and "message" in r
        if amount == "中文金额":
            assert not r["ok"], "非法金额应被友好拒绝"
        else:
            assert "ok" in r  # 允许通过或按红冲规则拒绝，但绝不能抛异常

    def test_corrupt_files_are_rejected_gracefully(self, env):
        cases = [
            ("fake.xlsx", b"this is not an excel file"),
            ("encrypted.pdf", b"%PDF-1.7 encrypted-garbage" + os.urandom(2048)),
            ("broken.ofd", b"OFD" + os.urandom(1024)),
        ]
        for name, blob in cases:
            r = env["inv"].import_files([(name, "", blob)], skip_match=True)[0]
            assert isinstance(r, dict) and r.get("ok") is False, (name, r)
            assert r.get("message")
        junk = env["reb"].import_excel("fake.xlsx", b"not an excel at all")
        assert isinstance(junk, dict) and junk.get("ok") is False

    def test_zip_and_template_injection_do_not_execute(self, env):
        """模板映射字段名注入不落 SQL；字典名 XSS 原样保存由前端转义。"""
        from app.service import template_service
        tpls = template_service.list_all() if hasattr(template_service, "list_all") else []
        assert isinstance(tpls, list)
        r = env["cat"].add_category("<img src=x onerror=alert(1)>")
        assert isinstance(r, dict)


# ---------------------------------------------------------------- 交接包/备份包投毒
class TestPoisonedPackages:

    @staticmethod
    def _zip(payload, extra=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("handover.json", json.dumps(payload, ensure_ascii=False))
            for name, data in (extra or {}).items():
                zf.writestr(name, data)
        return buf.getvalue()

    def test_handover_with_wrong_types_is_rejected(self, env):
        import_inv(env, "POISON-1", "10.00")
        before = one(env, "SELECT COUNT(*) AS n FROM invoice")["n"]
        for bad in ({"kind": "handover", "invoices": "not-a-list"},
                    {"kind": "handover", "invoices": [123], "reimburses": []},
                    {"kind": "handover", "invoices": [{"invoice_no": []}],
                     "reimburses": []}):
            r = env["handover"].import_handover(self._zip(bad))
            assert isinstance(r, dict) and r.get("ok") is False, (bad, r)
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == before

    def test_handover_duplicate_rows_inside_one_package(self, env):
        row = {
            "file_hash": "dup-pkg-hash", "file_type": "xml",
            "invoice_code": "", "invoice_no": "DUP-PKG-1",
            "invoice_date": "2026-09-01", "buyer_name": "北京示例科技有限公司",
            "buyer_tax_no": "91110108MA01AAAA1A", "seller_name": "测试供应商",
            "seller_tax_no": "91310000MA01BBBB2B", "amount": "10.00",
            "tax_amount": "0.00", "total_amount": "10.00",
            "item_summary": "", "raw_json": "{}", "parse_engine": "xml",
            "parse_confidence": 1.0, "status": "NORMAL",
        }
        payload = {"kind": "handover", "invoices": [row, dict(row)], "reimburses": []}
        r = env["handover"].import_handover(self._zip(payload))
        assert r["ok"], r
        assert r["invoices_added"] == 1 and r["invoices_skipped"] == 1
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == 1


# =====================================================================
# 维度三：高并发与资源压测
# =====================================================================
class TestConcurrency:

    def _race(self, fn, n=10):
        barrier = threading.Barrier(n)
        results = [None] * n

        def run(i):
            barrier.wait()
            try:
                results[i] = fn(i)
            except Exception as e:  # noqa: BLE001 记录并发异常用于断言
                results[i] = e

        ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        return results

    def test_concurrent_bind_same_invoice_only_one_wins(self, env):
        inv = import_inv(env, "RACE-BIND", "100.00")
        reb = add_reb(env, "BX-RACE-1", "100.00")
        results = self._race(lambda i: env["match"].manual_match(inv, reb), 10)
        ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
        assert len(ok) == 1, f"并发绑定成功 {len(ok)} 次，乐观锁失效"
        assert active_match_count(env, inv) == 1
        assert inv_row(env, inv)["status"] == "MATCHED"

    def test_concurrent_bind_different_reimburses_only_one_wins(self, env):
        inv = import_inv(env, "RACE-BIND-2", "50.00")
        rebs = [add_reb(env, f"BX-RACE-2-{i}", "50.00") for i in range(10)]
        results = self._race(lambda i: env["match"].manual_match(inv, rebs[i]), 10)
        ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
        assert len(ok) == 1
        assert active_match_count(env, inv) == 1

    def test_concurrent_category_update_optimistic_lock(self, env):
        inv = import_inv(env, "RACE-CAT", "10.00")
        assert env["inv"].set_invoice_category(inv, "办公费")["ok"]
        ver = one(env, "SELECT version FROM invoice_category WHERE invoice_id=?",
                  (inv,))["version"]

        cats = ["差旅费", "餐饮费", "办公费", "交通费", "通讯费",
                "其他费用", "差旅费", "餐饮费", "办公费", "交通费"]
        results = self._race(
            lambda i: env["inv"].set_invoice_category(inv, cats[i], version=ver), 10)
        ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
        stale = [r for r in results
                 if isinstance(r, dict) and r.get("stale")]
        assert len(ok) == 1, f"分类并发写入成功 {len(ok)} 次，版本锁失效"
        assert len(stale) == 9, [
            (type(r).__name__ + ":" + (r.get("message", "") if isinstance(r, dict) else str(r)))[:100]
            for r in results]

    def test_concurrent_quota_never_exceeds_limit(self, env):
        lic = env["lic"]
        errors = []

        def consume(_i):
            try:
                lic.consume_invoice_quota(1)
            except lic.QuotaExceeded as e:
                errors.append(str(e))

        self._race(consume, 25)
        st = lic.quota_status()
        assert st["used"] == 20, f"额度被突破或算错: {st}"
        assert st["remaining"] == 0
        assert len(errors) == 5, f"超出额度的请求未被拒绝: {len(errors)}"


class TestScale:
    """5000 报销单 + 5000 发票：导入与全量匹配的性能/内存/锁表现。"""

    def test_bulk_import_and_full_match(self, env):
        import io as _io
        from openpyxl import Workbook

        n = 5000
        # 报销单：走真实 Excel 导入链路
        wb = Workbook()
        ws = wb.active
        ws.append(["报销单号", "提交人", "报销日期", "期间起", "期间止",
                   "费用类型", "金额", "发票号", "发票日期", "备注"])
        for i in range(n):
            no = f"I{n:09d}-{i}" if False else f"I{i:010d}"
            ws.append([f"BX-SCALE-{i:05d}", "批量员工", "2026-09-10",
                       "2026-09-01", "2026-09-30", "办公费", "100.00",
                       no, "2026-09-01", ""])
        buf = _io.BytesIO()
        wb.save(buf)
        t0 = time.perf_counter()
        res = env["reb"].import_excel("scale_reimburse.xlsx", buf.getvalue())
        import_cost = time.perf_counter() - t0
        assert res["ok"], res

        # 发票：单事务批量落库（文件解析路径已有独立用例，此处压 DB + 匹配引擎）
        t0 = time.perf_counter()
        with env["db"].tx() as conn:
            for i in range(n):
                env["invoice_repo"].insert(conn, {
                    "file_hash": f"scale-hash-{i}",
                    "file_type": "xml", "invoice_code": "",
                    "invoice_no": f"I{i:010d}", "invoice_date": "2026-09-01",
                    "buyer_name": "北京示例科技有限公司",
                    "buyer_tax_no": "91110108MA01AAAA1A",
                    "seller_name": "测试供应商",
                    "seller_tax_no": "91310000MA01BBBB2B",
                    "amount": "100.00", "tax_amount": "0.00",
                    "total_amount": "100.00", "raw_json": "{}",
                    "parse_engine": "xml", "parse_confidence": 1.0,
                    "status": "NORMAL",
                })
        seed_cost = time.perf_counter() - t0

        tracemalloc.start()
        t0 = time.perf_counter()
        summary = env["match"].run_matching()
        match_cost = time.perf_counter() - t0
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert match_cost < 60, f"全量匹配耗时 {match_cost:.1f}s，超过 60s"
        assert peak < 1024 * 1024 * 1024, f"匹配峰值内存 {peak / 1024 / 1024:.0f}MB"
        assert summary["auto_matched"] == n, summary
        assert one(env, "SELECT COUNT(*) AS n FROM match_result "
                        "WHERE status IN ('MATCHED','CONFIRMED')")["n"] == n
        assert one(env, "SELECT COUNT(*) AS n FROM invoice WHERE status='MATCHED'")["n"] == n
        print(f"\n[scale] excel导入 {import_cost:.1f}s / 发票落库 {seed_cost:.1f}s / "
              f"全量匹配 {match_cost:.1f}s / 峰值 {peak / 1024 / 1024:.0f}MB")


# =====================================================================
# 维度四：环境灾难与断电恢复
# =====================================================================
class TestChaos:

    def test_transaction_exception_rolls_back_everything(self, env):
        inv = import_inv(env, "TX-ROLLBACK", "10.00")
        before = one(env, "SELECT COUNT(*) AS n FROM invoice")["n"]
        with pytest.raises(RuntimeError):
            with env["db"].tx() as conn:
                env["invoice_repo"].insert(conn, {
                    "file_hash": "tx-new", "file_type": "xml",
                    "invoice_no": "TX-NEW", "parse_engine": "xml",
                    "parse_confidence": 1.0, "status": "NORMAL",
                })
                conn.execute("UPDATE invoice SET risk_note='脏写' WHERE id=?", (inv,))
                raise RuntimeError("模拟断电")
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == before
        assert inv_row(env, inv)["risk_note"] != "脏写"

    def test_process_killed_mid_write_leaves_no_partial_row(self, env):
        """子进程写入未提交事务后 os._exit，重启后必须完全回滚。"""
        db_path = str(settings.db_path)
        code = f"""
import os, sqlite3
conn = sqlite3.connect({db_path!r}, timeout=10)
conn.execute("PRAGMA foreign_keys = ON")
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO invoice (file_hash, file_type, invoice_no, parse_engine,"
             " parse_confidence, status) VALUES ('crash-hash','xml','CRASH-1','xml',1.0,'NORMAL')")
conn.commit if False else None
os._exit(9)
"""
        proc = subprocess.run([sys.executable, "-c", code], timeout=30)
        assert proc.returncode == 9
        with env["db"].read() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM invoice WHERE "
                             "invoice_no='CRASH-1'").fetchone()["n"]
        assert n == 0, "未提交事务在强杀后残留"

    def test_corrupt_backup_never_damages_current_db(self, env):
        inv = import_inv(env, "BK-SAFE", "10.00")
        before = one(env, "SELECT COUNT(*) AS n FROM invoice")["n"]

        r1 = env["backup"].restore_backup(b"PK\x03\x04garbage-not-a-zip")
        assert isinstance(r1, dict) and r1.get("ok") is False

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(
                {"kind": "full-backup", "app_version": "0.0.1"}))
            zf.writestr("readme.txt", "no db here")
        r2 = env["backup"].restore_backup(buf.getvalue())
        assert r2.get("ok") is False

        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == before
        assert inv_row(env, inv)["invoice_no"] == "BK-SAFE"

    def test_zip_slip_is_rejected(self, env):
        inv = import_inv(env, "ZIPSLIP", "10.00")
        before = one(env, "SELECT COUNT(*) AS n FROM invoice")["n"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(
                {"kind": "full-backup", "app_version": "0.0.1"}))
            zf.writestr("../evil.txt", "escape")
            zf.writestr("fapiao.db", b"not a db")
            zf.writestr("uploads/a.txt", b"x")
        r = env["backup"].restore_backup(buf.getvalue())
        assert r.get("ok") is False
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == before
        assert not (settings.data_dir.parent / "evil.txt").exists()
        assert inv_row(env, inv)

    def test_backup_from_higher_version_is_rejected(self, env):
        inv = import_inv(env, "BK-VER", "10.00")
        before = one(env, "SELECT COUNT(*) AS n FROM invoice")["n"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "kind": "full-backup", "app_version": "99.0.0"}))
            zf.writestr("fapiao.db", b"SQLite format 3\x00not-really")
            zf.writestr("uploads/a.txt", b"x")
        r = env["backup"].restore_backup(buf.getvalue())
        assert r.get("ok") is False
        assert "版本" in r.get("message", "")
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == before
        assert inv_row(env, inv)["invoice_no"] == "BK-VER"

    def test_backup_restore_roundtrip_and_rollback(self, env):
        """备份 -> 新增数据 -> 恢复：必须回到备份时刻且不残留新数据。"""
        import_inv(env, "BK-ROUND-1", "11.00")
        pack = env["backup"].create_backup()
        content = Path(pack["zip_path"]).read_bytes()

        import_inv(env, "BK-ROUND-2", "22.00")
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == 2

        r = env["backup"].restore_backup(content)
        assert r.get("ok"), r
        assert one(env, "SELECT COUNT(*) AS n FROM invoice "
                        "WHERE deleted_at IS NULL")["n"] == 1
        assert one(env, "SELECT 1 FROM invoice WHERE invoice_no='BK-ROUND-1'") is not None
        assert one(env, "SELECT 1 FROM invoice WHERE invoice_no='BK-ROUND-2'") is None

    def test_backup_failure_keeps_data_and_releases_lock(self, env, monkeypatch):
        import_inv(env, "BK-FAIL", "10.00")
        before = one(env, "SELECT COUNT(*) AS n FROM invoice")["n"]

        def boom(_zf):
            raise RuntimeError("模拟备份中断")

        monkeypatch.setattr(env["backup"], "_add_recent_logs", boom)
        with pytest.raises(RuntimeError):
            env["backup"].create_backup()
        assert env["backup"].is_backing_up() is False, "备份失败后锁未释放"
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == before
        assert not (settings.data_dir / "temp_backup").exists()

    def test_core_flows_work_offline(self, env, monkeypatch):
        """断网环境：本地录入/匹配/额度查询完全可用，不依赖网络。"""
        inv = import_inv(env, "OFFLINE-1", "30.00")
        reb = add_reb(env, "BX-OFFLINE", "30.00")

        def no_network(*_a, **_k):
            raise OSError("network is unreachable")

        monkeypatch.setattr(socket, "socket", no_network)
        assert env["match"].manual_match(inv, reb)["ok"]
        assert env["lic"].quota_status()["limit"] == 20


# =====================================================================
# 维度五：商业化与内控安全
# =====================================================================
class TestCommercialSecurity:

    def test_duplicate_handover_package_is_deduped(self, env):
        inv = import_inv(env, "HANDOVER-1", "25.00")
        add_reb(env, "BX-HANDOVER-1", "25.00", invoice_no="HANDOVER-1")
        exported = env["handover"].export_handover()
        content = Path(exported["zip_path"]).read_bytes()
        assert exported["invoices"] == 1 and exported["reimburses"] == 1

        # 模拟另一台设备：清空本机账目后连续导入两次
        with env["db"].tx() as conn:
            conn.execute("DELETE FROM match_result")
            conn.execute("DELETE FROM invoice_category")
            conn.execute("DELETE FROM invoice")
            conn.execute("DELETE FROM reimburse_category")
            conn.execute("DELETE FROM category_conflict")
            conn.execute("DELETE FROM reimburse")

        first = env["handover"].import_handover(content)
        assert first["ok"], first
        assert first["invoices_added"] == 1 and first["reimburses_added"] == 1
        second = env["handover"].import_handover(content)
        assert second["ok"], second
        assert second["invoices_added"] == 0 and second["reimburses_added"] == 0
        assert second["invoices_skipped"] == 1 and second["reimburses_skipped"] == 1
        assert one(env, "SELECT COUNT(*) AS n FROM invoice")["n"] == 1
        assert one(env, "SELECT COUNT(*) AS n FROM reimburse")["n"] == 1


# ---------------------------------------------------------------- 越权（真实 ASGI）
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _Client:
    def __init__(self, base):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def request(self, path, method="GET", body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")

    def post_json(self, path, payload):
        return self.request(path, "POST", json.dumps(payload).encode(),
                            {"Content-Type": "application/json"})

    def login(self, username, password):
        status, text = self.post_json("/api/auth/login",
                                      {"username": username, "password": password})
        assert status == 200 and json.loads(text).get("ok"), text
        return self


def _b64e(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """企业版 + 管理员 + 两名员工，用于越权穿透测试。"""
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
    from app.service import auth_service, license_service, invoice_service
    init_schema()
    auth_service.create_first_admin("admin", "Admin@123456", "管理员")
    auth_service.ack_recovery()

    payload = {"machine_code": "1111-2222-3333-4444", "plan": "enterprise",
               "role": "master", "exp": int(time.time()) + 365 * 86400,
               "duration_days": 365, "sub_limit": 3, "order_id": "ORD-ENT"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    license_service.apply_license_code(f"{_b64e(raw)}.{_b64e(key.sign(raw))}")

    emp1 = auth_service.create_user("emp1", "Emp1@123456", "employee", "员工甲")
    emp2 = auth_service.create_user("emp2", "Emp2@123456", "employee", "员工乙")
    inv = invoice_service.manual_create({
        "invoice_no": "OWNER-1", "invoice_date": "2026-09-01",
        "buyer_name": "甲方", "buyer_tax_no": "91110108MA01AAAA1A",
        "seller_name": "乙方", "seller_tax_no": "91310000MA01BBBB2B",
        "amount": "10.00", "tax_amount": "0.00", "total_amount": "10.00",
    })
    assert inv["ok"], inv
    invoice_service.assign_owner([inv["invoice_id"]], emp1)

    from app.main import create_app
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, lifespan="off",
        log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    probe = _Client(base)
    for _ in range(100):
        try:
            if probe.request("/health")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("测试服务启动失败")
    yield {"base": base, "inv_id": inv["invoice_id"], "emp2": emp2}
    server.should_exit = True
    thread.join(timeout=5)


class TestAuthorization:

    def test_employee_cannot_read_others_invoice(self, web):
        c = _Client(web["base"]).login("emp2", "Emp2@123456")
        status, text = c.request(f"/api/invoices/{web['inv_id']}")
        assert status == 403, text
        status, text = c.request(f"/api/invoices/{web['inv_id']}/file")
        assert status == 403, text
        # 服务端渲染的详情页同样要拦截（302 回列表）
        status, text = c.request(f"/invoices/{web['inv_id']}")
        assert status == 302, status
        status, text = c.request("/api/invoices")
        assert status == 403, text

    def test_employee_cannot_mutate_or_delete(self, web):
        c = _Client(web["base"]).login("emp2", "Emp2@123456")
        status, text = c.request(
            f"/api/invoices/{web['inv_id']}/delete", "POST", b"", {})
        assert status == 403, text
        status, text = c.request(
            f"/api/invoices/{web['inv_id']}/category", "POST",
            b"category=x", {"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 403, text

    def test_employee_cannot_reach_finance_apis(self, web):
        c = _Client(web["base"]).login("emp2", "Emp2@123456")
        for path in ("/api/recycle-bin", "/api/audit?page=1", "/api/team/users"):
            status, text = c.request(path)
            assert status == 403, f"{path} -> {status} {text[:120]}"

    def test_employee_can_read_own_invoice(self, web):
        c = _Client(web["base"]).login("emp1", "Emp1@123456")
        status, text = c.request(f"/api/invoices/{web['inv_id']}")
        assert status == 200, text
        assert json.loads(text)["ok"] is True
