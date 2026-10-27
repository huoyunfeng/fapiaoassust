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
                code="", seller="测试供应商", buyer_tax="91110108MA01AAAA1A",
                item_name="*测试*项目"):
    amt = amount if amount is not None else total
    tx = tax if tax is not None else "0.00"
    return (f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice><Header><InvoiceNo>{no}</InvoiceNo><InvoiceCode>{code}</InvoiceCode>
<InvoiceDate>{date}</InvoiceDate></Header>
<Buyer><BuyerName>北京示例科技有限公司</BuyerName><BuyerTaxID>{buyer_tax}</BuyerTaxID></Buyer>
<Seller><SellerName>{seller}</SellerName><SellerTaxID>91310000MA01BBBB2B</SellerTaxID></Seller>
<Items><Item><ItemName>{item_name}</ItemName><Amount>{amt}</Amount><TaxRate>13%</TaxRate><TaxAmount>{tx}</TaxAmount></Item></Items>
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
        assert r2["skipped"] and r2["invoice_id"] == r1["invoice_id"] and "已存在" in r2["message"]

    def test_i2_same_invoice_no_marked_duplicate(self, env):
        r1 = env["inv"].import_files([("a.xml", "", xml_invoice("I2000000001", "100.00"))])[0]
        # 同号不同内容（金额不同 -> 文件 hash 不同）-> 照常导入，状态标记为重复
        r2 = env["inv"].import_files([("b.xml", "", xml_invoice("I2000000001", "100.00", seller="另一家供应商"))])[0]
        assert r1["status"] == "NORMAL"
        assert r2["status"] == "DUPLICATE" and not r2.get("skipped")
        with env["db"].read() as conn:
            assert len(env["invoice_repo"].list(conn)) == 2

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
        # 用户侧只收到友好提示（不暴露 OCR/堆栈等技术细节）
        assert r["status"] == "ABNORMAL" and "图片无法识别" in r["message"]

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


# =====================================================================
# 报销内容提取规则 + 智能归类 + 词典记忆（商业化重构）
# =====================================================================
class TestItemSummaryRules:

    def test_clean_summary_prefers_detail(self):
        from app.domain.normalize import NormalizeEngine
        n = NormalizeEngine()
        # 规则1：提取第二个星号后的明细
        assert n._clean_summary("*生产生活服务*餐饮服务") == "餐饮服务"
        # 规则2：剔除括号与规格型号
        assert n._clean_summary("*家用清洁电器具*美的电热水器 F50-20A3Pro(HE)") == "美的电热水器"
        # 规则4 例外：只有大类没有明细时才用大类
        assert n._clean_summary("*办公用品*") == "办公用品"
        # 规则3 兜底：无星号用原文
        assert n._clean_summary("住宿费") == "住宿费"
        # 绝不因品名过长退化为大类
        assert "电器具" not in n._clean_summary("*家用清洁电器具*海尔双开门智能变频电冰箱家用大容量")
        # OCR 把表头并入商品行：剔除"金额"等表头词
        assert n._clean_summary("*餐饮服务*餐饮服务 金额") == "餐饮服务"
        assert n._clean_summary("餐饮服务 金额") == "餐饮服务"
        assert n._clean_summary("*运输服务*高速通行费 单价 金额") == "高速通行费"
        # 项目名称/规格型号双栏折行交叠：剔除重复碎片与孤立字母，只保留最完整名称
        assert n._clean_summary(
            "*家用清洁电器具*美的电 美的电热水器 F 热水器 F50-20A3Pro(HE) 50-20A3Pro(HE)"
        ) == "美的电热水器"
        # 无重复碎片时不受影响
        assert n._clean_summary("*餐饮服务*餐饮服务") == "餐饮服务"

    def test_categorize_item(self):
        from app.domain.categorize import categorize_item
        assert categorize_item("餐饮服务") == "餐饮费"
        assert categorize_item("奶茶茶饮") == "餐饮费"
        assert categorize_item("火车票") == "差旅费"      # 差旅优先于交通（含"车"）
        assert categorize_item("酒店住宿") == "差旅费"
        assert categorize_item("高速通行费") == "交通费"
        assert categorize_item("加油费") == "交通费"
        assert categorize_item("办公用品") == "办公费"
        assert categorize_item("电器设备") == "办公费"
        assert categorize_item("美的电热水器") == "办公费"  # 家电实体词（含"热水器"）
        assert categorize_item("电饭煲") == "办公费"      # 家电优先，不被"饭"吞进餐饮费
        assert categorize_item("格力空调") == "办公费"
        # 实体品名词典：具体品名不含类目词也能归类
        assert categorize_item("海底捞火锅") == "餐饮费"   # 不含"餐/饭/食品/茶饮"
        assert categorize_item("珍珠奶茶") == "餐饮费"
        assert categorize_item("高铁票") == "差旅费"       # 不含"火车"
        assert categorize_item("如家宾馆") == "差旅费"
        assert categorize_item("动车票") == "差旅费"       # 差旅优先，不被"车"吞进交通费
        assert categorize_item("地铁出行") == "交通费"
        assert categorize_item("ETC通行费") == "交通费"
        assert categorize_item("手机流量包") == "通讯费"
        assert categorize_item("宽带费") == "通讯费"
        assert categorize_item("鲜花") == "其他费用"
        assert categorize_item("") == "其他费用"

    def test_category_drill_filter(self, env):
        """仪表盘下钻：发票列表按分类 + 日期范围过滤（分类口径与仪表盘一致）。"""
        env["inv"].import_files([("c2.xml", "", xml_invoice("CAT0000002", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["inv"].import_files([("c3.xml", "", xml_invoice("CAT0000003", "200.00", date="2026-10-05", item_name="*家用电器*美的电热水器"))])
        rows = env["inv"].list_invoices(category="餐饮费")
        assert len(rows) == 1 and rows[0]["invoice_no"] == "CAT0000002"
        rows = env["inv"].list_invoices(category="办公费", date_from="2026-10-01", date_to="2026-10-31")
        assert len(rows) == 1 and rows[0]["invoice_no"] == "CAT0000003"
        # 日期范围排除：9 月的餐饮发票不在 10 月范围内
        assert env["inv"].count_invoices(category="餐饮费", date_from="2026-10-01", date_to="2026-10-31") == 0
        assert env["inv"].count_invoices(category="办公费") == 1
        # focus 定位同样支持分类过滤
        with env["db"].read() as conn:
            inv = [r for r in env["invoice_repo"].list(conn) if r["invoice_no"] == "CAT0000003"][0]
        assert env["inv"].page_of_invoice(inv["id"], category="办公费") == 1

    def test_invoice_preview(self, env):
        """抽屉详情：结构化字段 + 无匹配记录 + 文件类型。"""
        r = env["inv"].import_files([("c4.xml", "", xml_invoice("CAT0000004", "100.00"))])[0]
        d = env["inv"].invoice_preview(r["invoice_id"])
        assert d and d["invoice"]["invoice_no"] == "CAT0000004"
        assert d["match"] is None and d["file_type"] == "xml"
        # has_file 依赖真实文件路径（模块级 FileStore 不随测试隔离的 upload_dir 变化），此处不断言
        assert env["inv"].invoice_preview(999999) is None

    def test_alias_memory_on_import(self, env):
        """用户手动改名后，再次导入同类发票自动应用新名字。"""
        item = "*家用清洁电器具*美的电热水器 F50-20A3Pro(HE)"
        r1 = env["inv"].import_files([("w1.xml", "", xml_invoice("AL10000001", "100.00", item_name=item))])[0]
        with env["db"].read() as conn:
            row1 = env["invoice_repo"].get(conn, r1["invoice_id"])
        assert row1["item_summary"] == "美的电热水器"

        # 用户双击修改为"办公设备" -> 记录词典
        res = env["inv"].update_item_summary(r1["invoice_id"], "办公设备")
        assert res["ok"] and "已记住" in res["message"]

        # 再次导入同内容发票（不同发票号）-> 自动展示"办公设备"
        r2 = env["inv"].import_files([("w2.xml", "", xml_invoice("AL10000002", "200.00", item_name=item))])[0]
        with env["db"].read() as conn:
            row2 = env["invoice_repo"].get(conn, r2["invoice_id"])
        assert row2["item_summary"] == "办公设备"

    def test_expense_breakdown_uses_standard_categories(self, env):
        env["inv"].import_files([("c1.xml", "", xml_invoice("CAT0000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["inv"].import_files([("c2.xml", "", xml_invoice("CAT0000002", "200.00", item_name="*运输服务*高速通行费"))])
        env["inv"].import_files([("c3.xml", "", xml_invoice("CAT0000003", "50.00", item_name="*鲜花*鲜花"))])
        data = env["inv"].expense_breakdown()
        names = {i["name"] for i in data["items"]}
        assert "餐饮费" in names and "交通费" in names and "其他费用" in names
        # 严禁原始发票词/税务大类直接出现在图表
        assert "餐饮服务" not in names and "鲜花" not in names and "生产生活服务" not in names
        assert abs(sum(i["percent"] for i in data["items"]) - 100) < 0.1
