"""设计文档 §11 测试方案：不变量 / 边界 / 集成测试。

运行: python -m pytest tests/test_plan.py -v -s
"""
import os
import sys
import threading
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.config import settings


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每个用例独立的临时数据库与上传目录（不污染正式数据）。"""
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    from app.repository.db import init_schema
    init_schema()
    from app.service import invoice_service, match_service, reimburse_service, report_service
    from app.repository import db
    from app.repository.repos import (invoice_repo, log_repo, match_repo,
                                      reimburse_repo)
    return dict(db=db, invoice_repo=invoice_repo, match_repo=match_repo,
                log_repo=log_repo, reimburse_repo=reimburse_repo,
                inv=invoice_service, match=match_service, reb=reimburse_service,
                report=report_service)


def xml_invoice(no, total, amount=None, tax=None, date="2026-09-01",
                code="", seller="测试供应商", buyer_tax="91110108MA01AAAA1A"):
    amt = amount if amount is not None else total
    tx = tax if tax is not None else "0.00"
    return (f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice><Header><InvoiceNo>{no}</InvoiceNo><InvoiceCode>{code}</InvoiceCode>
<InvoiceDate>{date}</InvoiceDate></Header>
<Buyer><BuyerName>北京示例科技有限公司</BuyerName><BuyerTaxID>{buyer_tax}</BuyerTaxID></Buyer>
<Seller><SellerName>{seller}</SellerName><SellerTaxID>91310000MA01BBBB2B</SellerTaxID></Seller>
<Items><Item><ItemName>*测试*项目</ItemName><Amount>{amt}</Amount><TaxRate>13%</TaxRate><TaxAmount>{tx}</TaxAmount></Item></Items>
<Settlement><TotalTaxInclAmount>{total}</TotalTaxInclAmount></Settlement></Invoice>""").encode("utf-8")


def make_excel(rows, path=None):
    import io
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["报销单号", "提交人", "报销日期", "期间起", "期间止", "费用类型", "金额", "发票号", "发票日期", "备注"])
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


# =====================================================================
# 11.1 不变量测试
# =====================================================================
class TestInvariants:

    def test_i1_same_file_twice_skipped(self, env):
        content = xml_invoice("I1000000001", "100.00")
        r1 = env["inv"].import_files([("a.xml", "", content)])[0]
        r2 = env["inv"].import_files([("a.xml", "", content)])[0]
        assert r1["ok"] and r1["status"] == "NORMAL"
        assert not r2["ok"] and r2["invoice_id"] == r1["invoice_id"] and "已存在" in r2["message"]

    def test_i2_same_invoice_no_marked_duplicate(self, env):
        r1 = env["inv"].import_files([("a.xml", "", xml_invoice("I2000000001", "100.00"))])[0]
        # 同号不同内容（金额不同 -> 文件 hash 不同）
        r2 = env["inv"].import_files([("b.xml", "", xml_invoice("I2000000001", "100.00", seller="另一家供应商"))])[0]
        assert r1["status"] == "NORMAL"
        assert r2["status"] == "DUPLICATE"

    def test_i3_one_invoice_cannot_match_two_reimburses(self, env):
        env["inv"].import_files([("a.xml", "", xml_invoice("I3000000001", "100.00"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R3001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "100.00", "I3000000001", "", ""],
            ["R3002", "李四", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "100.00", "I3000000001", "", ""],
        ]))
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            rebs = env["reimburse_repo"].list(conn)
        r1 = env["match"].manual_match(inv["id"], rebs[0]["id"])
        r2 = env["match"].manual_match(inv["id"], rebs[1]["id"])
        assert r1["ok"] and not r2["ok"] and "已存在生效匹配" in r2["message"] or "仅 NORMAL" in r2["message"]

    def test_i4_one_reimburse_cannot_match_two_invoices(self, env):
        env["inv"].import_files([("a.xml", "", xml_invoice("I4000000001", "100.00")),
                                 ("b.xml", "", xml_invoice("I4000000002", "100.00"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R4001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "100.00", "", "", ""]]))
        with env["db"].read() as conn:
            invs = env["invoice_repo"].list(conn)
            reb = env["reimburse_repo"].list(conn)[0]
        r1 = env["match"].manual_match(invs[0]["id"], reb["id"])
        r2 = env["match"].manual_match(invs[1]["id"], reb["id"])
        assert r1["ok"] and not r2["ok"] and "报销单已存在生效匹配" in r2["message"]

    def test_i5_amount_mismatch_rejected(self, env):
        # 金额=100, 税额=13, 合计=114 -> 100+13=113 != 114, 校验失败
        r = env["inv"].import_files([("a.xml", "", xml_invoice("I5000000001", "114.00", amount="100.00", tax="13.00"))])[0]
        assert r["status"] == "ABNORMAL" and "价税合计" in r["message"]

    def test_i6_decimal_no_float_error(self, env):
        # 0.1 + 0.2 = 0.3, Decimal 精度校验通过
        r = env["inv"].import_files([("a.xml", "", xml_invoice("I6000000001", "0.30", amount="0.10", tax="0.20"))])[0]
        assert r["status"] == "NORMAL"

    def test_i7_status_change_logged(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("I7000000001", "100.00"))])[0]
        with env["db"].read() as conn:
            logs = env["log_repo"].list_for_invoice(conn, r["invoice_id"])
        transitions = [(l["from_status"], l["to_status"]) for l in logs]
        assert ("PARSING", "PARSED") in transitions and ("PARSED", "NORMAL") in transitions

    def test_i8_delete_invoice_cascades_match(self, env):
        env["inv"].import_files([("a.xml", "", xml_invoice("I8000000001", "100.00"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R8001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "100.00", "", "", ""]]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            row = conn.execute("SELECT COUNT(*) n FROM match_result WHERE invoice_id=?",
                               (inv["id"],)).fetchone()
            assert row["n"] == 1  # 自动匹配产出 SUSPICIOUS 记录
        env["inv"].delete_invoice(inv["id"])
        with env["db"].read() as conn:
            assert env["invoice_repo"].get(conn, inv["id"]) is None
            rows = conn.execute("SELECT COUNT(*) n FROM match_result WHERE invoice_id=?",
                                (inv["id"],)).fetchone()
            assert rows["n"] == 0  # 级联删除

    def test_i9_transaction_rollback_on_failure(self, env):
        from app.repository.repos import reimburse_repo
        before = None
        with env["db"].read() as conn:
            before = env["reimburse_repo"].count(conn)
        with pytest.raises(RuntimeError):
            with env["db"].tx() as conn:
                reimburse_repo.insert(conn, {
                    "batch_id": "b9", "reimburse_no": "R9001", "amount": "10.00", "row_index": 1})
                raise RuntimeError("模拟落库失败")
        with env["db"].read() as conn:
            assert env["reimburse_repo"].count(conn) == before  # 无脏数据

    def test_i10_score_within_01(self, env):
        from app.domain.matcher import MatchEngine
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        eng = MatchEngine()
        cases = [
            (InvoiceDTO(invoice_no="A", invoice_date="2026-09-01", total_amount=Decimal("100")),
             ReimburseDTO(amount=Decimal("100"), invoice_no="A", period_start="2026-09-01", period_end="2026-09-30")),
            (InvoiceDTO(invoice_no="X", invoice_date="2020-01-01", total_amount=Decimal("99999")),
             ReimburseDTO(amount=Decimal("1"), invoice_no="Y", period_start="2026-01-01", period_end="2026-01-31")),
            (InvoiceDTO(), ReimburseDTO()),
        ]
        for inv, reb in cases:
            ps = eng.score_pair(inv, reb)
            assert 0.0 <= ps.score <= 1.0
        # 阈值边界：0.7 -> SUSPICIOUS, 0.95 -> MATCHED
        from app.domain.matcher import PairScore
        assert eng.decide_status(PairScore(score=0.70, match_type="fuzzy")) == "SUSPICIOUS"
        assert eng.decide_status(PairScore(score=0.6999, match_type="fuzzy")) == "UNMATCHED"
        assert eng.decide_status(PairScore(score=0.95, match_type="exact")) == "MATCHED"
        assert eng.decide_status(PairScore(score=0.9499, match_type="fuzzy")) == "SUSPICIOUS"


# =====================================================================
# 11.2 边界测试
# =====================================================================
class TestBoundary:

    def test_amount_zero(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B1000000001", "0.00", amount="0.00"))])[0]
        assert r["status"] == "NORMAL"  # 0 元发票合法

    def test_amount_negative_red_flush(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B1000000002", "-100.00", amount="-100.00"))])[0]
        assert r["status"] == "NORMAL"
        with env["db"].read() as conn:
            inv = env["invoice_repo"].get(conn, r["invoice_id"])
            assert inv["is_red_flush"] == 1  # 红冲标记

    def test_amount_huge(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B1000000003", "99999999.99", amount="99999999.99"))])[0]
        assert r["status"] == "NORMAL"

    def test_date_future(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B2000000001", "100.00", date="2099-01-01"))])[0]
        assert r["status"] == "ABNORMAL" and "晚于今天" in r["message"]

    def test_date_ten_years_ago(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B2000000002", "100.00", date="2010-01-01"))])[0]
        assert r["status"] == "NORMAL" and "过旧" in r["message"]  # WARN 不阻断

    def test_date_cross_year(self, env):
        from app.domain.matcher import MatchEngine
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        ps = MatchEngine().score_pair(
            InvoiceDTO(invoice_no="C", invoice_date="2025-12-31", total_amount=Decimal("100")),
            ReimburseDTO(amount=Decimal("100"), invoice_no="C",
                         period_start="2026-01-01", period_end="2026-01-31"))
        assert 0.0 <= ps.score <= 1.0 and ps.score < 1.0  # 跨年期间外扣分但不报错

    def test_tax_no_lowercase_normalized(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B3000000001", "100.00", buyer_tax="91110108ma01aaaa1a"))])[0]
        with env["db"].read() as conn:
            inv = env["invoice_repo"].get(conn, r["invoice_id"])
            assert inv["buyer_tax_no"] == "91110108MA01AAAA1A"  # 标准化为大写

    def test_tax_no_with_spaces(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B3000000002", "100.00", buyer_tax="91110108 MA01 AAAA1A"))])[0]
        with env["db"].read() as conn:
            inv = env["invoice_repo"].get(conn, r["invoice_id"])
            assert " " not in inv["buyer_tax_no"]

    def test_tax_no_fullwidth_warns(self, env):
        r = env["inv"].import_files([("a.xml", "", xml_invoice("B3000000003", "100.00", buyer_tax="９１１１０１０８"))])[0]
        assert r["status"] == "NORMAL" and "税号格式异常" in r["message"]  # WARN

    @pytest.mark.parametrize("length", [15, 18, 20])
    def test_tax_no_valid_lengths(self, env, length):
        tax = ("91110108MA01AAAA1A" + "0" * 10)[:length]
        r = env["inv"].import_files([("a.xml", "", xml_invoice(f"B31{length:014d}", "100.00", buyer_tax=tax))])[0]
        assert r["status"] == "NORMAL" and "税号格式异常" not in r["message"]

    def test_empty_file(self, env):
        r = env["inv"].import_files([("empty.xml", "", b"")])[0]
        assert r["status"] == "ABNORMAL"

    def test_blurry_image_no_ocr(self, env):
        r = env["inv"].import_files([("blurry.jpg", "", b"\xff\xd8\xff" + os.urandom(2048))])[0]
        assert r["status"] == "ABNORMAL" and "OCR" in r["message"]

    def test_encrypted_or_corrupt_pdf(self, env):
        r = env["inv"].import_files([("encrypted.pdf", "", b"%PDF-1.7 encrypted-garbage" + os.urandom(1024))])[0]
        assert r["status"] == "ABNORMAL"  # 降级链走完仍失败，不阻塞

    def test_many_to_one_sum_match(self, env):
        env["inv"].import_files([("a.xml", "", xml_invoice("B4000000001", "60.00")),
                                 ("b.xml", "", xml_invoice("B4000000002", "40.00"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RB4001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "100.00", "", "", "汇总报销"]]))
        summary = env["match"].run_matching()
        assert summary["many_to_one"] == 2
        with env["db"].read() as conn:
            pairs = conn.execute("SELECT COUNT(*) n FROM match_pair").fetchone()
            assert pairs["n"] == 2

    def test_same_no_different_code_not_duplicate(self, env):
        # 同一发票号在不同发票代码下（旧版发票）-> 不算重复
        r1 = env["inv"].import_files([("a.xml", "", xml_invoice("B5000000001", "100.00", code="011001900111"))])[0]
        r2 = env["inv"].import_files([("b.xml", "", xml_invoice("B5000000001", "100.00", code="011001900222"))])[0]
        assert r1["status"] == "NORMAL" and r2["status"] == "NORMAL"

    def test_many_to_one_subset_boundary(self, env):
        from app.domain.matcher import MatchEngine
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        eng = MatchEngine()
        # 4 张子集（上限内）可命中
        pool4 = [InvoiceDTO(invoice_no=f"S{i}", invoice_date="2026-09-0%d" % (i + 1),
                            total_amount=Decimal("25.00")) for i in range(4)]
        reb = ReimburseDTO(amount=Decimal("100.00"),
                           period_start="2026-09-01", period_end="2026-09-30")
        assert eng.find_many_to_one(pool4, reb) is not None
        # 需要 5 张才凑齐（超出上限 many_to_one_max_invoices=4）-> 不命中
        pool5 = [InvoiceDTO(invoice_no=f"T{i}", invoice_date="2026-09-0%d" % (i + 1),
                            total_amount=Decimal("20.00")) for i in range(5)]
        assert eng.find_many_to_one(pool5, reb) is None


# =====================================================================
# 11.3 集成测试
# =====================================================================
class TestIntegration:

    def test_100_invoices_50_reimburses(self, env):
        files = []
        for i in range(100):
            no = f"N{i:012d}"
            total = f"{100 + i}.00"
            files.append((f"inv_{i}.xml", "", xml_invoice(no, total, date="2026-09-%02d" % (i % 28 + 1))))
        results = env["inv"].import_files(files)
        assert sum(1 for r in results if r["ok"]) == 100

        rows = []
        for i in range(50):
            # 前 40 张与报销单一一对应，后 60 张无候选
            no = f"N{i:012d}" if i < 40 else ""
            rows.append([f"RI{i:04d}", f"员工{i}", "2026-10-09", "2026-09-01", "2026-09-30",
                         "费用", f"{100 + i}.00", no, "", ""])
        r = env["reb"].import_excel("r.xlsx", make_excel(rows))
        assert r["imported"] == 50

        env["match"].run_matching()  # 导入时已自动匹配，显式调用验证幂等
        matched = env["match"].list_matches(["MATCHED"])
        assert len(matched) >= 40  # 前 40 对应精确/自动匹配
        report = env["report"].build_report()
        content = env["report"].export_excel(report)
        assert content[:2] == b"PK" and len(content) > 1000  # 有效 xlsx

    def test_1000_invoices_performance(self, env):
        files = [(f"p{i}.xml", "", xml_invoice(f"P{i:012d}", "100.00")) for i in range(1000)]
        t0 = time.time()
        results = env["inv"].import_files(files)
        import_ms = (time.time() - t0) * 1000
        assert sum(1 for r in results if r["ok"]) == 1000

        env["reb"].import_excel("r.xlsx", make_excel(
            [[f"RP{i:04d}", "员工", "2026-10-09", "2026-09-01", "2026-09-30", "费用", "100.00", f"P{i:012d}", "", ""]
             for i in range(50)]))
        t0 = time.time()
        summary = env["match"].run_matching()
        match_ms = (time.time() - t0) * 1000
        print(f"\n[性能] 1000 张导入 {import_ms:.0f}ms（{import_ms/1000:.2f}ms/张），"
              f"1000x50 匹配 {match_ms:.0f}ms")
        matched = env["match"].list_matches(["MATCHED"])
        assert len(matched) == 50  # 导入时已自动匹配，显式调用幂等
        assert import_ms < 120_000 and match_ms < 120_000  # 宽松上限，只防卡死

    def test_concurrent_import_and_match(self, env):
        errors = []

        def importer(offset):
            try:
                files = [(f"c{offset}_{i}.xml", "", xml_invoice(f"C{offset}{i:09d}", f"{50 + i}.00"))
                         for i in range(25)]
                env["inv"].import_files(files)
            except Exception as e:
                errors.append(e)

        def matcher():
            try:
                for _ in range(3):
                    env["match"].run_matching()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=importer, args=(i,)) for i in range(4)]
        threads.append(threading.Thread(target=matcher))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        assert not errors, f"并发执行出错: {errors}"
        with env["db"].read() as conn:
            # 不变量校验：每张发票至多一条生效匹配，每条报销单至多一条生效匹配
            dup_inv = conn.execute(
                "SELECT invoice_id, COUNT(*) n FROM match_result "
                "WHERE status IN ('MATCHED','CONFIRMED') GROUP BY invoice_id HAVING n > 1").fetchall()
            dup_reb = conn.execute(
                "SELECT reimburse_id, COUNT(*) n FROM match_result "
                "WHERE status IN ('MATCHED','CONFIRMED') AND reimburse_id IS NOT NULL "
                "GROUP BY reimburse_id HAVING n > 1").fetchall()
            total = conn.execute("SELECT COUNT(*) n FROM invoice").fetchone()["n"]
        assert not dup_inv and not dup_reb
        assert total == 100
