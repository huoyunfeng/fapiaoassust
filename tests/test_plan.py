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
        """跨年期间外扣分（走第二阶段：报销单不填号，日期维度正常参与）。"""
        from app.domain.matcher import MatchEngine
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        ps = MatchEngine().score_pair(
            InvoiceDTO(invoice_no="C", invoice_date="2025-12-31", total_amount=Decimal("100")),
            ReimburseDTO(amount=Decimal("100"), invoice_no="",   # 不填号 → 走第二阶段
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
        # 5 张可凑齐（上限 many_to_one_max_invoices=5）-> 命中
        pool5 = [InvoiceDTO(invoice_no=f"T{i}", invoice_date="2026-09-0%d" % (i + 1),
                            total_amount=Decimal("20.00")) for i in range(5)]
        assert eng.find_many_to_one(pool5, reb) is not None
        # 需要 6 张全取才凑齐 100（任意 5 张都不足）-> 超出上限 5，不命中
        amounts = ["10.00", "15.00", "15.00", "20.00", "20.00", "20.00"]
        pool6 = [InvoiceDTO(invoice_no=f"U{i}", invoice_date="2026-09-0%d" % (i + 1),
                            total_amount=Decimal(a)) for i, a in enumerate(amounts)]
        assert eng.find_many_to_one(pool6, reb) is None


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

    def test_same_file_twice_no_orphan_file(self, env):
        """重复文件跳过时不得再次落盘（避免上传目录留下无引用的孤儿文件）。"""
        content = xml_invoice("ORPH0000001", "100.00")
        env["inv"].import_files([("o.xml", "", content)])[0]
        root = env["inv"]._file_store.root
        before = len([p for p in root.rglob("*") if p.is_file()])
        r2 = env["inv"].import_files([("o.xml", "", content)])[0]
        after = len([p for p in root.rglob("*") if p.is_file()])
        assert r2["skipped"] and after == before

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
        # 同号不同内容 -> DUPLICATE：仪表盘统计不计入，下钻列表必须同样不计入
        env["inv"].import_files([("c2b.xml", "", xml_invoice("CAT0000002", "100.00",
                                                             seller="另一家供应商",
                                                             item_name="*餐饮服务*餐饮服务"))])
        assert env["inv"].count_invoices(category="餐饮费") == 1
        assert len(env["inv"].list_invoices(category="餐饮费")) == 1
        # 用户显式指定状态时以状态为准（可查看重复发票）
        assert env["inv"].count_invoices(category="餐饮费", status="DUPLICATE") == 1
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


# =====================================================================
# 费用归类引擎：报销单（业务实质）> 模板 > 发票（税务凭证）
# =====================================================================
def _ensure_match(env, invoice_no, reimburse_no):
    """确保指定发票与报销单生效匹配（已匹配则跳过）。"""
    with env["db"].read() as conn:
        inv = [i for i in env["invoice_repo"].list(conn)
               if i["invoice_no"] == invoice_no][0]
        rb = [r for r in env["reimburse_repo"].list(conn)
              if r["reimburse_no"] == reimburse_no][0]
    if inv["status"] != "MATCHED":
        env["match"].manual_match(inv["id"], rb["id"])
    return inv["id"]


def test_project_name_is_not_expense_type():
    """「项目名称」是工程项目归属，不能被自动映射成费用类型。"""
    from app.domain.field_registry import auto_map
    m = auto_map(["姓名", "项目名称", "金额", "发票号", "备注"])
    assert m.get("项目名称") != "expense_type"
    assert m.get("金额") == "amount"
    assert m.get("姓名") == "submitter"


class TestCategoryEngine:

    def test_reimburse_expense_type_wins(self, env):
        """第一层：报销单填了类型 -> 强制用报销单类型，明细不符给轻量提示但不拦截。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ENG1000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RE1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "ENG1000001", "", ""]]))
        _ensure_match(env, "ENG1000001", "RE1")

        row = env["inv"].list_invoices()[0]
        assert row["category"] == "办公费"
        assert row["category_status"] == "CONFIRMED"
        assert row["category_hint"] == "发票内容与费用类型不匹配"

        data = env["inv"].expense_breakdown()
        assert [i["name"] for i in data["items"]] == ["办公费"]
        assert data["has_pending"] is False
        # 报销单列表：费用类型 = 所匹配发票的分类（两页同一口径）
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "RE1"][0]
        assert rb["category"] == "办公费"
        assert rb["cat_status"] == "CONFIRMED"
        assert rb["cat_invoice_id"] == row["id"]

    def test_empty_expense_type_inferred_pending(self, env):
        """第二层：报销单类型为空 -> 系统按发票明细预填，标记待确认，金额照常计入统计。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ENG2000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RE2", "李四", "2026-09-10", "2026-09-01", "2026-09-30",
             "", "100.00", "ENG2000001", "", ""]]))
        _ensure_match(env, "ENG2000001", "RE2")

        row = env["inv"].list_invoices()[0]
        assert row["category"] == "餐饮费"
        assert row["category_status"] == "PENDING"
        assert row["category_source"] == "inferred"

        data = env["inv"].expense_breakdown()
        assert data["has_pending"] is True and data["pending_count"] == 1
        assert data["items"][0]["has_pending"] is True
        assert data["items"][0]["count"] == 1          # 金额不漏算
        assert data["total_count"] == 1

        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "RE2"][0]
        assert rb["cat_status"] == "PENDING"
        assert rb["cat_suggest"] == "餐饮费"
        assert rb["category"] == "餐饮费"

    def test_orphan_invoice_inferred(self, env):
        """第四层：无报销单的游离发票同样推测，来源标记 orphan。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ENG3000001", "100.00", item_name="*运输服务*高速通行费"))])
        row = env["inv"].list_invoices()[0]
        assert row["category"] == "交通费"
        assert row["category_status"] == "PENDING"
        assert row["category_source"] == "orphan"

    def test_manual_confirm_not_spread_to_similar(self, env):
        """单条修改=个例修正：不学习记忆，同明细的其他发票不受影响（不扩散）。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("ENG4000001", "100.00",
                                      item_name="*餐饮服务*星巴克咖啡")),
            ("b.xml", "", xml_invoice("ENG4000002", "200.00",
                                      item_name="*餐饮服务*星巴克咖啡")),
        ])
        rows = env["inv"].list_invoices()
        assert len(rows) == 2
        first = rows[0]
        second = rows[1]
        assert first["category"] == "餐饮费" and first["category_status"] == "PENDING"

        res = env["inv"].set_invoice_category(first["id"], "办公费")
        assert res["ok"] is True
        assert not (res.get("learned") or [])     # 单条不学习记忆

        rows = env["inv"].list_invoices()
        r1 = [r for r in rows if r["id"] == first["id"]][0]
        r2 = [r for r in rows if r["id"] == second["id"]][0]
        assert r1["category"] == "办公费" and r1["category_status"] == "CONFIRMED"
        # 关键断言：同摘要的另一张发票分类不被连带改变（仍为系统推测）
        assert r2["category"] == "餐饮费" and r2["category_status"] == "PENDING"

        # 批量修改才学习记忆并扩散
        res2 = env["inv"].set_invoices_category([second["id"]], "办公费")
        assert res2["ok"] is True
        rows = env["inv"].list_invoices()
        r2 = [r for r in rows if r["id"] == second["id"]][0]
        assert r2["category"] == "办公费" and r2["category_status"] == "CONFIRMED"

        # 非法分类被拒绝
        assert env["inv"].set_invoice_category(first["id"], "乱填分类")["ok"] is False

    def test_submitter_memory_only_when_also_blank(self, env):
        """提交人记忆只在该员工当时也没填类型时套用，避免一刀切改掉他所有发票。"""
        from app.domain.categorize import resolve_category
        mem = {"wrong_value": "某项目", "category": "餐饮费"}
        d = resolve_category("出租车", submitter="霍云峰", submitter_memory=mem,
                             has_reimburse=True)
        assert d["status"] == "PENDING"
        assert d["category"] == "交通费"

        d2 = resolve_category("出租车", submitter="霍云峰",
                              submitter_memory={"wrong_value": "", "category": "餐饮费"},
                              has_reimburse=True)
        assert d2["category"] == "餐饮费"
        assert d2["source"] == "memory"

    def test_manual_confirm_clears_mismatch_hint(self, env):
        """人工确认的分类为最终口径：不再提示不匹配，仅保留报销单原始填写值。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ENG7000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RE7", "霍云峰", "2026-09-10", "2026-09-01", "2026-09-30",
             "某项目支出", "100.00", "ENG7000001", "", ""]]))
        inv_id = _ensure_match(env, "ENG7000001", "RE7")

        before = [r for r in env["inv"].list_invoices() if r["id"] == inv_id][0]
        assert before["category"] == "其他费用"   # 报销单自由填写的类型先收敛到标准大类

        env["inv"].set_invoice_category(inv_id, "餐饮费")
        after = [r for r in env["inv"].list_invoices() if r["id"] == inv_id][0]
        assert after["category"] == "餐饮费"
        assert after["category_status"] == "CONFIRMED"
        assert after["category_hint"] == ""
        assert after["category_raw"] == "某项目支出"

    def test_normalize_and_merge_non_standard_type(self, env):
        """第五层：报销单自由填写的类型收敛到标准大类，图表不出现细碎名称。"""
        from app.domain.categorize import STANDARD_CATEGORIES, normalize_category
        assert normalize_category("差旅") == "差旅费"
        assert normalize_category("某项目支出") == "其他费用"
        assert normalize_category("办公费") == "办公费"

        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ENG6000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RE6", "赵六", "2026-09-10", "2026-09-01", "2026-09-30",
             "某项目支出", "100.00", "ENG6000001", "", ""]]))
        _ensure_match(env, "ENG6000001", "RE6")

        row = env["inv"].list_invoices()[0]
        assert row["category"] == "其他费用"
        assert row["category_raw"] == "某项目支出"
        data = env["inv"].expense_breakdown()
        assert all(i["name"] in STANDARD_CATEGORIES for i in data["items"])

    def test_submitter_correction_memory(self, env):
        """第三层：修正员工填错的类型后，同提交人的同一错误值自动纠正。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("ENG5000001", "100.00",
                                      item_name="*餐饮服务*餐饮服务")),
            ("b.xml", "", xml_invoice("ENG5000002", "100.00",
                                      item_name="*餐饮服务*餐饮服务")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RE51", "王五", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "ENG5000001", "", ""],
            ["RE52", "王五", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "ENG5000002", "", ""],
        ]))
        inv1 = _ensure_match(env, "ENG5000001", "RE51")
        _ensure_match(env, "ENG5000002", "RE52")

        assert [r for r in env["inv"].list_invoices() if r["id"] == inv1][0]["category"] == "办公费"
        # 批量修改=财务明确确认同类归属：学习提交人记忆并扩散
        res = env["inv"].set_invoices_category([inv1], "餐饮费")
        assert res["ok"] is True

        # 同提交人另一张（仍填「办公费」）被记忆纠正为餐饮费
        other = [r for r in env["inv"].list_invoices() if r["id"] != inv1][0]
        assert other["category"] == "餐饮费"
        assert other["category_source"] == "memory"
        assert other["category_status"] == "CONFIRMED"

    def test_reimburse_category_syncs_invoices(self, env):
        """报销单页确认费用类型 -> 关联发票的分类同步变更（两页同一口径）。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "SYNC000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RES1", "钱七", "2026-09-10", "2026-09-01", "2026-09-30",
             "", "100.00", "SYNC000001", "", ""]]))
        inv_id = _ensure_match(env, "SYNC000001", "RES1")

        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "RES1"][0]
        assert rb["cat_status"] == "PENDING" and rb["category"] == "餐饮费"

        res = env["inv"].set_reimburse_category(rb["id"], "交通费")
        assert res["ok"] is True and res["invoice_count"] == 1

        # 发票页同步为已确认
        inv = [r for r in env["inv"].list_invoices() if r["id"] == inv_id][0]
        assert inv["category"] == "交通费" and inv["category_status"] == "CONFIRMED"
        # 报销单页同步
        rb2 = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "RES1"][0]
        assert rb2["category"] == "交通费" and rb2["cat_status"] == "CONFIRMED"

    def test_unmatched_reimburse_category_isolated(self, env):
        """未匹配（孤岛）：报销单可独立改分类，不影响任何发票，且留审计。"""
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RES2", "孙八", "2026-09-10", "2026-09-01", "2026-09-30",
             "", "88.00", "", "", ""]]))
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "RES2"][0]
        assert rb["cat_invoice_id"] is None and rb["cat_matched"] is False
        res = env["inv"].set_reimburse_category(rb["id"], "餐饮费")
        assert res["ok"] is True and res["matched"] is False
        assert res["invoice_count"] == 0
        rb2 = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "RES2"][0]
        assert rb2["category"] == "餐饮费" and rb2["cat_status"] == "CONFIRMED"
        from app.service import audit_service
        logs = audit_service.list_recent(limit=20, target_type="reimburse")["items"]
        assert any(l["action"] == "reimburse_category" and l["new_value"] == "餐饮费"
                   for l in logs)


# =====================================================================
# 六模块：字典维护 / 孤岛隔离 / 已匹配联动 / 冲突仲裁 / 统计口径 / 审计
# =====================================================================
def _seed_many_to_one(env, inv_ids, reb_id):
    """构造「1 张报销单对多张发票」的生效汇总匹配（现行流程只生成待确认，无确认入口）。"""
    with env["db"].tx() as conn:
        conn.execute("DELETE FROM match_pair")
        conn.execute("DELETE FROM match_result")
        for iid in inv_ids:
            mid = env["match_repo"].insert(conn, {
                "invoice_id": iid, "reimburse_id": None, "match_type": "many_to_one",
                "match_score": 0.9, "status": "MATCHED", "diff_fields": [],
                "message": "汇总匹配（测试构造）",
            })
            env["match_repo"].insert_pair(conn, mid, iid, reb_id)


class TestCategoryGovernance:

    def test_dict_similar_force_disable_and_merge(self, env):
        """模块一：相似词提示合并、可强制新增、只能禁用、合并同步历史值。"""
        from app.service import category_service
        r = category_service.add_category("餐饮")
        assert r["ok"] is False and r.get("need_confirm") is True
        assert "餐饮费" in r["similar"]

        r2 = category_service.add_category("餐饮", force=True)
        assert r2["ok"] is True
        assert "餐饮" in category_service.enabled_names()

        cats = {c["name"]: c for c in category_service.list_categories(include_disabled=True)}
        cid = cats["餐饮"]["id"]
        assert category_service.toggle_category(cid)["enabled"] is False
        # 禁用后新建下拉隐藏，但历史值仍可校验（all_names 含禁用项）
        assert "餐饮" not in category_service.enabled_names()
        assert "餐饮" in category_service.all_names()
        # 不存在物理删除：同名再新增 -> 恢复启用
        r3 = category_service.add_category("餐饮")
        assert r3["ok"] is True and "重新启用" in r3["message"]

        # 合并：历史里出现过的非字典值「伙食费」并入「餐饮费」
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "DICT000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        inv_id = env["inv"].list_invoices()[0]["id"]
        with env["db"].tx() as conn:
            conn.execute("INSERT INTO invoice_category(invoice_id, category, source) "
                         "VALUES (?, '伙食费', 'manual')", (inv_id,))
        merged = category_service.add_category("伙食费", merge_into="餐饮费")
        assert merged["ok"] is True and "合并" in merged["message"]
        assert [x for x in env["inv"].list_invoices()
                if x["id"] == inv_id][0]["category"] == "餐饮费"

    def test_batch_master_slave_rules(self, env):
        """统一批量面板（模块二+三）：无论是否匹配都可批量改，按主从法则分桶处理。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("BATCH00001", "100.00", item_name="*餐饮服务*咖啡")),
            ("b.xml", "", xml_invoice("BATCH00002", "120.00", item_name="*餐饮服务*咖啡")),
            ("c.xml", "", xml_invoice("BATCH00003", "140.00", item_name="*餐饮服务*盒饭")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["BATCHR1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "", "140.00", "BATCH00003", "", ""]]))
        matched_id = _ensure_match(env, "BATCH00003", "BATCHR1")
        rows = env["inv"].list_invoices()
        free_ids = [r["id"] for r in rows if r["id"] != matched_id]
        assert len(free_ids) == 2

        # 相似查找三层判定：咖啡→餐饮大类，命中「咖啡」（规则一/三）与「盒饭」（规则一同大类，含已匹配）
        sim = env["inv"].similar_unmatched(free_ids[0])
        assert sim["ok"] is True and len(sim["items"]) == 2
        by_id = {it["id"]: it for it in sim["items"]}
        assert by_id[free_ids[1]]["matched"] is False
        assert by_id[matched_id]["matched"] is True          # 含已匹配记录并标注状态
        assert "同属「餐饮费」大类" in by_id[matched_id]["reason"]
        assert any("餐饮费" in b for b in sim["basis"])      # 顶部提示依据

        # 批量：已匹配（一对一）连带同步其报销单；未匹配仅改自身
        res = env["inv"].set_invoices_category(free_ids + [matched_id], "办公费")
        assert res["ok"] is True and res["updated"] == 3
        assert res["unmatched"] == 2 and res["matched"] == 1
        with env["db"].read() as conn:
            reb = [r for r in env["reimburse_repo"].list(conn)
                   if r["reimburse_no"] == "BATCHR1"][0]
            assert reb["id"] in res["linked_reimburses"]
        after = {r["id"]: r for r in env["inv"].list_invoices()}
        assert after[free_ids[0]]["category"] == "办公费"
        assert after[free_ids[1]]["category"] == "办公费"
        assert after[matched_id]["category"] == "办公费"    # 已匹配发票一并更新
        # 一对一连带：报销单主分类被同步为办公费
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "BATCHR1"][0]
        assert rb["category"] == "办公费"

    def test_matched_invoice_one_to_one_syncs_reimburse(self, env):
        """主从法则.1：一对一匹配下改发票，连带同步其绑定的报销单分类，两边天然一致。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ISO0000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["ISOR1", "李四", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "ISO0000001", "", ""]]))
        inv_id = _ensure_match(env, "ISO0000001", "ISOR1")

        res = env["inv"].set_invoice_category(inv_id, "交通费")
        assert res["ok"] is True and res["matched"] is True
        assert res["linked_sync"] is True and res["mismatch"] is False
        inv = [r for r in env["inv"].list_invoices() if r["id"] == inv_id][0]
        assert inv["category"] == "交通费"
        # 一对一连带：报销单同步为交通费，无分类差异
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "ISOR1"][0]
        assert rb["category"] == "交通费" and rb["cat_conflict"] is False

    def test_matched_invoice_one_to_many_keeps_others(self, env):
        """主从法则.2：一对多匹配下改发票，绝对只改当前发票，不动报销单与其他发票，差异打标。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("OTO1000001", "60.00", item_name="*餐饮服务*餐饮服务")),
            ("b.xml", "", xml_invoice("OTO1000002", "40.00", item_name="*运输服务*出租车")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["OTO1R1", "孙八", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "", "", ""]]))
        rows = env["inv"].list_invoices()
        with env["db"].read() as conn:
            reb = env["reimburse_repo"].list(conn)[0]
        _seed_many_to_one(env, [r["id"] for r in rows], reb["id"])

        first = rows[0]["id"]
        res = env["inv"].set_invoice_category(first, "差旅费")
        assert res["ok"] is True and res["matched"] is True
        assert res["linked_sync"] is False     # 一对多：严禁连带报销单
        # 绝对只改当前发票：invoice_category 人工决策表只有当前发票一条记录
        with env["db"].read() as conn:
            manual = conn.execute(
                "SELECT invoice_id, category FROM invoice_category ORDER BY invoice_id").fetchall()
        assert len(manual) == 1 and manual[0]["invoice_id"] == first
        assert manual[0]["category"] == "差旅费"
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "OTO1R1"][0]
        assert rb["category"] == "办公费"                  # 报销单总分类未被改
        assert rb["cat_conflict"] is True                  # 差异打标

    def test_matched_reimburse_always_syncs_invoices(self, env):
        """主从法则.3：改报销单分类，无条件强制联动所有关联发票（mode 参数仅兼容保留）。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "SELF000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["SELFR1", "王五", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "SELF000001", "", ""]]))
        inv_id = _ensure_match(env, "SELF000001", "SELFR1")
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "SELFR1"][0]

        # 即便调用方传 self，也已匹配即强制联动发票
        r1 = env["inv"].set_reimburse_category(rb["id"], "交通费", mode="self")
        assert r1["ok"] is True and r1["mode"] == "sync" and r1["invoice_count"] == 1
        assert [r for r in env["inv"].list_invoices()
                if r["id"] == inv_id][0]["category"] == "交通费"
        rb2 = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "SELFR1"][0]
        assert rb2["category"] == "交通费" and rb2["cat_conflict"] is False

    def test_conflict_three_arbitrations(self, env):
        """模块四：一对多差异的三种仲裁（以报销单为主拉平 / 以发票为准 / 保留差异）。"""
        from app.service import category_service
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("CF0000001", "60.00", item_name="*餐饮服务*餐饮服务")),
            ("b.xml", "", xml_invoice("CF0000002", "40.00", item_name="*运输服务*出租车")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["CFR1", "赵六", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "", "", ""]]))
        rows = env["inv"].list_invoices()
        with env["db"].read() as conn:
            reb = env["reimburse_repo"].list(conn)[0]
        _seed_many_to_one(env, [r["id"] for r in rows], reb["id"])
        inv1, inv2 = rows[0]["id"], rows[1]["id"]
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "CFR1"][0]

        # 一对多改单张发票 -> 产生分类差异
        env["inv"].set_invoice_category(inv1, "交通费")
        assert category_service.count_unresolved() == 1

        # 1) 以报销单为主（强制拉平明细）-> 两张发票都改为办公费
        assert category_service.resolve_conflict(rb["id"], "reimburse")["ok"] is True
        after = {r["id"]: r for r in env["inv"].list_invoices()}
        assert after[inv1]["category"] == "办公费" and after[inv2]["category"] == "办公费"
        assert category_service.count_unresolved() == 0

        # 2) 以发票为准 -> 报销单分类改为发票侧分类
        env["inv"].set_invoice_category(inv1, "交通费")
        env["inv"].set_invoice_category(inv2, "交通费")
        assert category_service.resolve_conflict(rb["id"], "invoice")["ok"] is True
        rb2 = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "CFR1"][0]
        assert rb2["category"] == "交通费"

        # 3) 保留差异 -> 两边分类各自保留，差异关闭
        env["inv"].set_invoice_category(inv1, "餐饮费")
        assert category_service.resolve_conflict(rb["id"], "keep")["ok"] is True
        assert category_service.count_unresolved() == 0
        rb3 = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "CFR1"][0]
        assert rb3["category"] == "交通费"
        # 保留差异：发票1=餐饮费、发票2=交通费（各自保留，按序拼接展示）
        assert rb3["cat_invoice_category"] == "交通费、餐饮费"

    def test_one_to_many_allow_flatten_or_keep(self, env):
        """模块四：一对多且发票分类不同时，允许「保留差异」，也允许一键拉平（以报销单为准）。"""
        from app.service import category_service
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("OTO0000001", "60.00", item_name="*餐饮服务*餐饮服务")),
            ("b.xml", "", xml_invoice("OTO0000002", "40.00", item_name="*运输服务*出租车")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["OTOR1", "孙八", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "", "", ""]]))
        rows = env["inv"].list_invoices()
        env["inv"].set_invoice_category(rows[0]["id"], "餐饮费")
        env["inv"].set_invoice_category(rows[1]["id"], "交通费")
        with env["db"].read() as conn:
            reb = env["reimburse_repo"].list(conn)[0]
        _seed_many_to_one(env, [r["id"] for r in rows], reb["id"])
        with env["db"].tx() as conn:
            assert category_service.detect_conflict(conn, reb["id"]) is not None

        # 一键拉平（以报销单为主）：强制所有明细发票分类 = 报销单分类
        assert category_service.resolve_conflict(reb["id"], "reimburse")["ok"] is True
        after = {r["id"]: r for r in env["inv"].list_invoices()}
        assert after[rows[0]["id"]]["category"] == "办公费"
        assert after[rows[1]["id"]]["category"] == "办公费"
        assert category_service.count_unresolved() == 0

        # 再次产生差异后可保留差异
        env["inv"].set_invoice_category(rows[0]["id"], "餐饮费")
        assert category_service.count_unresolved() == 1
        assert category_service.resolve_conflict(reb["id"], "keep")["ok"] is True
        assert category_service.count_unresolved() == 0

    def test_unmatch_keeps_both_categories(self, env):
        """模块三.3：解除匹配后两边分类各自保留（一对一同步后的最新值），并留审计。"""
        from app.service import audit_service
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "UM0000001", "100.00", item_name="*餐饮服务*餐饮服务"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["UMR1", "钱七", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "UM0000001", "", ""]]))
        inv_id = _ensure_match(env, "UM0000001", "UMR1")
        # 一对一：改发票连带同步报销单 -> 两边都是交通费
        env["inv"].set_invoice_category(inv_id, "交通费")
        mid = env["match"].list_matches(["MATCHED", "CONFIRMED"])[0]["id"]

        assert env["match"].unmatch(mid)["ok"] is True
        inv = [r for r in env["inv"].list_invoices() if r["id"] == inv_id][0]
        assert inv["status"] == "NORMAL" and inv["category"] == "交通费"
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "UMR1"][0]
        # 解除后两边各自保留解除前的最新分类（同步后的交通费）
        assert rb["cat_matched"] is False and rb["category"] == "交通费"
        assert any(l["action"] == "unmatch" for l in audit_service.list_recent(limit=50)["items"])

    def test_expense_breakdown_amount_counted_once(self, env):
        """模块五：金额只遍历发票表一次；已匹配用报销单分类，否则用发票分类。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("SUM0000001", "100.00", item_name="*餐饮服务*餐饮服务")),
            ("b.xml", "", xml_invoice("SUM0000002", "200.00", item_name="*餐饮服务*餐饮服务")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["SUMR1", "周九", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "100.00", "SUM0000001", "", ""]]))
        _ensure_match(env, "SUM0000001", "SUMR1")

        data = env["inv"].expense_breakdown()
        assert data["total_amount"] == 300.00 and data["total_count"] == 2
        assert sum(i["amount"] for i in data["items"]) == 300.00
        by_name = {i["name"]: i["amount"] for i in data["items"]}
        assert by_name.get("办公费") == 100.00      # 已匹配 -> 报销单分类
        assert by_name.get("餐饮费") == 200.00      # 未匹配 -> 发票分类（也计入金额）

    def test_expense_breakdown_invoice_confirmed_overrides_reimburse(self, env):
        """模块四.3：发票已人工确认分类优先于报销单分类（高铁票改业务招待费 → 钱算发票的）。

        报销单=差旅费，但其中一张发票被财务确认为「餐饮费」-> 该发票金额计入餐饮费，
        其余未确认明细仍跟随报销单分类。
        """
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("SUM1000001", "300.00", item_name="*运输服务*高铁票")),
            ("b.xml", "", xml_invoice("SUM1000002", "200.00", item_name="*住宿服务*酒店")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["SUM1R1", "周九", "2026-09-10", "2026-09-01", "2026-09-30",
             "差旅费", "500.00", "", "", "出差"]]))
        rows = env["inv"].list_invoices()
        with env["db"].read() as conn:
            reb = env["reimburse_repo"].list(conn)[0]
        _seed_many_to_one(env, [r["id"] for r in rows], reb["id"])

        # 财务把第一张发票（高铁票）人工确认为餐饮费（一对多 -> 只改明细、差异打标）
        env["inv"].set_invoice_category(rows[0]["id"], "餐饮费")

        data = env["inv"].expense_breakdown()
        by_name = {i["name"]: i["amount"] for i in data["items"]}
        # 发票已确认分类优先：高铁票 300 计入餐饮费
        assert by_name.get("餐饮费") == 300.00
        # 另一张未确认 -> 跟随报销单「差旅费」
        assert by_name.get("差旅费") == 200.00
        assert data["total_amount"] == 500.00

    def test_dict_builtin_locked(self, env):
        """系统预设分类强制锁定：不可禁用、不可重命名、不可作为合并源，且永远启用。"""
        from app.service import category_service
        cats = category_service.list_categories(include_disabled=True)
        builtins = [c for c in cats if c["is_builtin"]]
        assert len(builtins) == 6
        first = builtins[0]
        for c in builtins:
            assert category_service.toggle_category(c["id"])["ok"] is False
            assert "锁定" in category_service.toggle_category(c["id"])["message"]
            assert "锁定" in category_service.rename_category(c["id"], "改名分类")["message"]
        other = [c for c in builtins if c["id"] != first["id"]][0]
        assert "锁定" in category_service.merge_category(first["id"], other["id"])["message"]
        for c in builtins:
            assert c["enabled"] and c["name"] in category_service.enabled_names()

    def test_dict_custom_rename_only_when_unused(self, env):
        """自定义分类：未使用可直接重命名；已使用则拒绝（只能合并）。"""
        from app.service import category_service
        r = category_service.add_category("研发加班餐", force=True)
        assert r["ok"] is True
        cid = r["id"]

        # 未使用 -> 可直接重命名（解决打错字）
        assert category_service.rename_category(cid, "研发餐补")["ok"] is True
        cats = {c["name"]: c for c in category_service.list_categories(include_disabled=True)}
        assert "研发餐补" in cats and "研发加班餐" not in cats

        # 重命名为已存在的名称 -> 拒绝
        assert category_service.rename_category(cid, "餐饮费")["ok"] is False

        # 被使用后 -> 锁定，不允许直接重命名
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "GOV0000001", "50.00", item_name="*餐饮服务*加班餐"))])
        inv_id = env["inv"].list_invoices()[0]["id"]
        assert env["inv"].set_invoice_category(inv_id, "研发餐补")["ok"] is True
        r2 = category_service.rename_category(cid, "改名餐补")
        assert r2["ok"] is False and "已被" in r2["message"] and "合并" in r2["message"]

    def test_dict_custom_merge_moves_history_and_disables(self, env):
        """自定义分类合并：历史数据（发票+报销单）全部改目标分类、源分类禁用、留审计。"""
        from app.service import audit_service, category_service
        cid = category_service.add_category("研发加班餐", force=True)["id"]
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "GOV0000002", "60.00", item_name="*餐饮服务*加班餐"))])
        inv_id = env["inv"].list_invoices()[0]["id"]
        env["inv"].set_invoice_category(inv_id, "研发加班餐")
        env["reb"].import_excel("r.xlsx", make_excel([
            ["GOVR1", "吴十", "2026-09-10", "2026-09-01", "2026-09-30",
             "", "60.00", "", "", ""]]))
        rb = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "GOVR1"][0]
        env["inv"].set_reimburse_category(rb["id"], "研发加班餐")

        cats = {c["name"]: c for c in category_service.list_categories(include_disabled=True)}
        target = cats["餐饮费"]
        # 不能与自身合并
        assert category_service.merge_category(cid, cid)["ok"] is False
        # 合并到系统预设分类
        res = category_service.merge_category(cid, target["id"])
        assert res["ok"] is True and res["moved"] == 2   # 发票 1 条 + 报销单 1 条（单条修改不学记忆）
        # 源分类已禁用但保留备查
        after = {c["name"]: c for c in category_service.list_categories(include_disabled=True)}
        assert after["研发加班餐"]["enabled"] == 0
        assert "研发加班餐" not in category_service.enabled_names()
        # 历史数据已改为目标分类
        assert [x for x in env["inv"].list_invoices()
                if x["id"] == inv_id][0]["category"] == "餐饮费"
        rb2 = [x for x in env["reb"].list_reimburses() if x["reimburse_no"] == "GOVR1"][0]
        assert rb2["category"] == "餐饮费"
        # 审计：记录原分类名与目标分类名
        logs = audit_service.list_recent(limit=50, target_type="category_dict")["items"]
        assert any(l["action"] == "category_dict_merge" and l["old_value"] == "研发加班餐"
                   and l["new_value"] == "餐饮费" for l in logs)
        # 已禁用的目标分类不可作为合并目标
        c2 = category_service.add_category("团建聚餐", force=True)["id"]
        category_service.toggle_category(c2)  # 禁用
        r3 = category_service.add_category("夜宵餐补", force=True)["id"]
        assert category_service.merge_category(r3, c2)["ok"] is False


# =====================================================================
# 平局候选：无发票号 + 同金额同日期（重复票）场景
# =====================================================================
class TestAmbiguousCandidates:

    def _seed_duplicates(self, env, n=3, amount="38.00"):
        """n 张金额/日期相同但发票号不同的发票 + 一张未填发票号的等额报销单。"""
        env["inv"].import_files([
            (f"d{i}.xml", "", xml_invoice(f"DUP{i:05d}", amount,
                                          date="2026-09-05",
                                          seller="嘀嘀出行",
                                          item_name="*运输服务*打车费"))
            for i in range(n)])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-DUP1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", amount, "", "", "打车报销"]]))

    def _candidate_rows(self, env):
        with env["db"].read() as conn:
            return conn.execute(
                "SELECT invoice_id, status, message FROM match_result "
                "WHERE reimburse_id IS NOT NULL").fetchall()

    def test_tie_listed_and_deterministic(self, env):
        """3 张重复票：候选集整体落库（每张并列发票一条候选记录）、不自动匹配、
        消息列出全部并列候选、两次执行结果一致（确定性）。"""
        self._seed_duplicates(env)
        s1 = env["match"].run_matching()
        rows1 = self._candidate_rows(env)
        assert s1["auto_matched"] == 0 and s1["pending"] == 1
        # 平局：每张并列发票各落一条 SUSPICIOUS 候选记录（候选集整体返回）
        assert len(rows1) == 3
        assert all(r["status"] == "SUSPICIOUS" for r in rows1)
        # 候选按发票 id 升序（确定性）
        assert [r["invoice_id"] for r in rows1] == sorted(r["invoice_id"] for r in rows1)
        msg = rows1[0]["message"]
        assert "3 张" in msg and "无法自动区分" in msg and "请人工" in msg
        # 并列候选全部列出（发票号）
        for no in ("DUP00000", "DUP00001", "DUP00002"):
            assert no in msg
        # 确定性：再次执行候选集与顺序完全一致
        first_ids = [r["invoice_id"] for r in rows1]
        env["match"].run_matching()
        rows2 = self._candidate_rows(env)
        assert [r["invoice_id"] for r in rows2] == first_ids

    def test_tied_invoices_not_reported_as_no_candidate(self, env):
        """并列候选的发票不应被误判为「无候选配对」。"""
        self._seed_duplicates(env)
        env["match"].run_matching()
        with env["db"].read() as conn:
            orphan = conn.execute(
                "SELECT COUNT(*) n FROM match_result "
                "WHERE reimburse_id IS NULL AND message LIKE '%无候选%'").fetchone()
        assert orphan["n"] == 0

    def test_ambiguity_aux_hint_uses_seller(self, env):
        """弱相似度提示：销售方名称出现在候选说明中（仅展示，不影响判定）。"""
        self._seed_duplicates(env, n=2)
        env["match"].run_matching()
        rows = self._candidate_rows(env)
        assert len(rows) == 2
        assert all("嘀嘀出行" in r["message"] and "文本相似度" in r["message"] for r in rows)

    def test_soft_lock_across_reimburses(self, env):
        """软占用（模块一.2）：同一发票被多张报销单同时列为候选 -> 消息追加高亮警告。"""
        # 1 张发票 + 2 张金额/日期相同、均未填发票号的报销单 -> 发票同时进入两个候选集
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "SOFT00001", "38.00", date="2026-09-05", seller="嘀嘀出行",
            item_name="*运输服务*打车费"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["SOFTR1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"],
            ["SOFTR2", "李四", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"],
        ]))
        env["match"].run_matching()
        rows = self._candidate_rows(env)
        assert len(rows) == 2   # 同一发票分别进入两张报销单的候选集
        # 两边都被打上软占用警告（⚠️ 同时被多张报销单作为候选，请核实）
        for r in rows:
            assert "同时被多张报销单" in r["message"] and "请核实" in r["message"]
        assert "SOFTR1" in rows[0]["message"] and "SOFTR2" in rows[0]["message"]
        # list_matches 输出 soft_locked 供前端高亮
        soft = [m for m in env["match"].list_matches(["SUSPICIOUS"]) if m.get("soft_locked")]
        assert len(soft) == 2

    def test_manual_match_kicks_other_candidates(self, env):
        """人工终裁（模块一.3）：确认归属后，该发票在其他候选池中的记录自动淘汰（REJECTED+留痕+审计）。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "KICK00001", "38.00", date="2026-09-05", seller="嘀嘀出行",
            item_name="*运输服务*打车费"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["KICKR1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"],
            ["KICKR2", "李四", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"],
        ]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            inv_id = env["invoice_repo"].list(conn)[0]["id"]
            reb1 = [r for r in env["reimburse_repo"].list(conn)
                    if r["reimburse_no"] == "KICKR1"][0]

        res = env["match"].manual_match(inv_id, reb1["id"])
        # 踢出 2 条：同发票在 KICKR2 的候选 + 同发票在 KICKR1 的旧候选（已被新 CONFIRMED 记录取代）
        assert res["ok"] is True and "踢出 2 条" in res["message"]
        with env["db"].read() as conn:
            # 发票对 KICKR2 的候选已被淘汰并留痕
            kicked = conn.execute(
                "SELECT status, message FROM match_result WHERE reimburse_id = "
                "(SELECT id FROM reimburse WHERE reimburse_no='KICKR2')").fetchone()
            assert kicked["status"] == "REJECTED"
            assert "候选流失" in kicked["message"] and "归属已确定" in kicked["message"]
            # 审计留痕
            audit = conn.execute(
                "SELECT action, detail FROM audit_log WHERE action='match_arbitrate'").fetchone()
            assert audit is not None and "踢出" in (audit["detail"] or "")


# =====================================================================
# 快捷修复（单点重匹配事务）：状态翻转 / 防抢占 / 强制绑定 / 审计
# =====================================================================
class TestQuickFix:

    def _one_pair(self, env, inv_no, inv_total, reb_no, reb_amount, reb_inv_no=""):
        env["inv"].import_files([("a.xml", "", xml_invoice(
            inv_no, inv_total, date="2026-09-05", seller="嘀嘀出行",
            item_name="*运输服务*打车费"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            [reb_no, "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", reb_amount, reb_inv_no, "", "打车报销"],
        ]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            reb = [r for r in env["reimburse_repo"].list(conn)
                   if r["reimburse_no"] == reb_no][0]
            m = conn.execute(
                "SELECT * FROM match_result WHERE invoice_id = ? ORDER BY id DESC LIMIT 1",
                (inv["id"],)).fetchone()
        return inv, reb, dict(m) if m else None

    def test_quick_fix_repairs_amount_and_flips_status(self, env):
        """号一致金额大差 -> 快捷修复报销金额 -> 单点重匹配成功，发票同步 MATCHED 并写审计。"""
        inv, reb, m = self._one_pair(env, "QF000001", "100.00", "QFR1", "120.00", "QF000001")
        assert m is not None
        res = env["match"].quick_fix(match_id=m["id"], reb_fields={"amount": "100.00"})
        assert res["ok"] is True and res["status"] == "MATCHED"
        with env["db"].read() as conn:
            inv_row = env["invoice_repo"].get(conn, inv["id"])
            match_row = env["match_repo"].get(conn, m["id"])
            audit = conn.execute(
                "SELECT action, old_value, new_value, detail FROM audit_log "
                "WHERE action='match_quick_fix'").fetchone()
        assert inv_row["status"] == "MATCHED"
        assert match_row["status"] == "MATCHED"
        assert audit is not None
        assert audit["old_value"] == "UNMATCHED" and audit["new_value"] == "MATCHED"
        assert "金额: 120.00 -> 100.00" in (audit["detail"] or "")

    def test_quick_fix_blocks_when_invoice_occupied(self, env):
        """防抢占：发票已生效匹配到其他报销单时立即阻断，且不产生任何写入。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "QF000002", "38.00", date="2026-09-05", seller="嘀嘀出行",
            item_name="*运输服务*打车费"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["QFR2A", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"],
            ["QFR2B", "李四", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"],
        ]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            rebs = {r["reimburse_no"]: r for r in env["reimburse_repo"].list(conn)}
        assert env["match"].manual_match(inv["id"], rebs["QFR2A"]["id"])["ok"] is True
        # 基线行数（run_matching 为 QFR2B 产出的候选记录，非本次写入）
        with env["db"].read() as conn:
            n_before = conn.execute(
                "SELECT COUNT(*) AS c FROM match_result WHERE reimburse_id = ?",
                (rebs["QFR2B"]["id"],)).fetchone()["c"]
        # 未占用的报销单 + 已占用发票 -> 阻断
        res = env["match"].quick_fix(invoice_id=inv["id"],
                                     reimburse_id=rebs["QFR2B"]["id"],
                                     force_bind=True)
        assert res["ok"] is False and "已被占用" in res["message"]
        with env["db"].read() as conn:
            n_after = conn.execute(
                "SELECT COUNT(*) AS c FROM match_result WHERE reimburse_id = ?",
                (rebs["QFR2B"]["id"],)).fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM match_result WHERE invoice_id = ? "
                "AND status IN ('MATCHED','CONFIRMED')", (inv["id"],)).fetchone()["c"]
        assert n_after == n_before          # 冲突阻断后未新增写入
        assert active == 1                  # 仍只保留原有 QFR2A 生效匹配

    def test_quick_fix_force_bind_unmatched_pair(self, env):
        """强制绑定：无匹配记录的发票+报销单可直接生效（match_type=manual）。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "QF000003", "50.00", date="2026-09-05", seller="某公司",
            item_name="*服务*咨询费"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["QFR3", "王五", "2026-09-10", "2026-09-01", "2026-09-30",
             "办公费", "999.00", "", "", "无号报销"],
        ]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            reb = env["reimburse_repo"].list(conn)[0]
        res = env["match"].quick_fix(invoice_id=inv["id"], reimburse_id=reb["id"],
                                     force_bind=True, reason="财务强制指定")
        assert res["ok"] is True and res["status"] == "MATCHED"
        with env["db"].read() as conn:
            row = env["match_repo"].active_for_invoice(conn, inv["id"])
        assert row is not None and row["match_type"] == "manual"
        assert row["status"] == "MATCHED"


# =====================================================================
# 相似记录三层判定：归类大类 / 销方核心字号 / 摘要核心词
# =====================================================================
class TestSimilarRules:
    def test_rule1_category_group(self, env):
        """规则一：摘要命中同一归类大类的记录全部查出（星巴克→瑞幸/餐费/餐饮服务）。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("SIM100001", "35.00", seller="瑞幸咖啡（北京）有限公司",
                                      item_name="*餐饮服务*星巴克咖啡")),
            ("b.xml", "", xml_invoice("SIM100002", "28.00", seller="瑞幸咖啡（上海）有限公司",
                                      item_name="*餐饮服务*瑞幸咖啡")),
            ("c.xml", "", xml_invoice("SIM100003", "50.00", seller="某酒楼",
                                      item_name="*餐饮服务*餐费")),
            ("d.xml", "", xml_invoice("SIM100004", "38.00", seller="嘀嘀出行",
                                      item_name="*运输服务*打车费")),
        ])
        target = [r for r in env["inv"].list_invoices() if r["invoice_no"] == "SIM100001"][0]
        sim = env["inv"].similar_unmatched(target["id"])
        assert sim["ok"] is True
        nos = {it["invoice_no"] for it in sim["items"]}
        # 同餐饮大类：瑞幸咖啡、餐费 命中；打车费（交通大类）不命中
        assert nos == {"SIM100002", "SIM100003"}
        assert all(it["reason"] == "同属「餐饮费」大类" for it in sim["items"])
        assert any("餐饮费" in b for b in sim["basis"])

    def test_rule2_seller_core_name(self, env):
        """规则二：销方核心字号匹配（剔除公司后缀+去地域前缀变体）。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("SIM200001", "35.00", seller="北京星巴克咖啡有限公司",
                                      item_name="*其他*杂项")),
            ("b.xml", "", xml_invoice("SIM200002", "28.00", seller="上海星巴克咖啡有限公司",
                                      item_name="*其他*杂物")),
            ("c.xml", "", xml_invoice("SIM200003", "50.00", seller="北京麦当劳食品有限公司",
                                      item_name="*其他*杂品")),
        ])
        target = [r for r in env["inv"].list_invoices() if r["invoice_no"] == "SIM200001"][0]
        sim = env["inv"].similar_unmatched(target["id"])
        nos = {it["invoice_no"] for it in sim["items"]}
        # 「星巴克咖啡」去地域前缀命中上海星巴克；麦当劳不命中
        assert "SIM200002" in nos and "SIM200003" not in nos
        hit = [it for it in sim["items"] if it["invoice_no"] == "SIM200002"][0]
        assert "同一商家" in hit["reason"] and any("星巴克" in h for h in hit["hits"])
        assert "商家名称" in sim["basis"]

    def test_rule3_keyword_fallback(self, env):
        """规则三兜底：无归类大类（其他费用不算）、无商家关系时按摘要核心词命中。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("SIM300001", "35.00", seller="甲公司",
                                      item_name="*咨询服务*XX项目可行性研究")),
            ("b.xml", "", xml_invoice("SIM300002", "28.00", seller="乙公司",
                                      item_name="*服务*XX项目可行性研究报告费")),
            ("c.xml", "", xml_invoice("SIM300003", "50.00", seller="丙公司",
                                      item_name="*运输服务*打车费")),
        ])
        target = [r for r in env["inv"].list_invoices() if r["invoice_no"] == "SIM300001"][0]
        sim = env["inv"].similar_unmatched(target["id"])
        nos = {it["invoice_no"] for it in sim["items"]}
        # 「XX项目可行性研究」核心词命中 SIM300002；打车费不命中
        assert "SIM300002" in nos and "SIM300003" not in nos
        hit = [it for it in sim["items"] if it["invoice_no"] == "SIM300002"][0]
        assert "关键词" in hit["reason"] and hit["hits"]
        assert "摘要关键词" in sim["basis"]

    def test_no_similar_returns_empty(self, env):
        """三条规则都不命中时返回空池（不弹批量面板）。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("SIM400001", "35.00", seller="甲公司",
                                      item_name="*餐饮服务*咖啡")),
            ("b.xml", "", xml_invoice("SIM400002", "28.00", seller="乙公司",
                                      item_name="*运输服务*打车费")),
        ])
        target = [r for r in env["inv"].list_invoices() if r["invoice_no"] == "SIM400001"][0]
        sim = env["inv"].similar_unmatched(target["id"])
        assert sim["ok"] is True and sim["items"] == [] and sim["basis"] == []


# =====================================================================
# 批次卡片联动：批次聚合统计 / 批次模板列 / 全部批次公共列
# =====================================================================
class TestBatchCards:

    def test_batches_unmatched_count(self, env):
        """批次聚合：row_count / unmatched_count（未匹配 = 无生效匹配）。"""
        env["inv"].import_files([("a.xml", "", xml_invoice("BAT000001", "100.00"))])
        env["reb"].import_excel("r1.xlsx", make_excel([
            ["RB0001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "99.00", "", "", ""],
            ["RB0002", "李四", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "200.00", "", "", ""],
        ]), batch_id="batch_A")
        env["reb"].import_excel("r2.xlsx", make_excel([
            ["RB0003", "王五", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "300.00", "", "", ""],
        ]), batch_id="batch_B")
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            rebs = {r["reimburse_no"]: r for r in env["reimburse_repo"].list(conn)}
        r = env["match"].manual_match(inv["id"], rebs["RB0001"]["id"])
        assert r["ok"]
        batches = {b["batch_id"]: b for b in env["reb"].list_batches()}
        assert batches["batch_A"]["row_count"] == 2
        assert batches["batch_A"]["unmatched_count"] == 1    # RB0002 未匹配
        assert batches["batch_B"]["row_count"] == 1
        assert batches["batch_B"]["unmatched_count"] == 1
        assert batches["batch_A"]["template_name"]           # 批次卡片标题用

    def test_batch_template_id(self, env):
        """batch_template_id 返回批次绑定的模板，供表头严格按该模板渲染。"""
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RB1001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "99.00", "", "", ""],
        ]), batch_id="batch_T")
        tid = env["reb"].batch_template_id("batch_T")
        assert isinstance(tid, int)
        assert env["reb"].batch_template_id("batch_不存在") is None

    def test_global_reimburse_no_across_batches(self, env):
        """模块一.1：跨批次/跨模板导入的缺号报销单全局连续递增，不按模板重置。"""
        env["reb"].import_excel("r1.xlsx", make_excel([
            ["", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "99.00", "", "", ""],
            ["", "李四", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "100.00", "", "", ""],
        ]), batch_id="batch_G1")
        env["reb"].import_excel("r2.xlsx", make_excel([
            ["", "王五", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "300.00", "", "", ""],
        ]), batch_id="batch_G2")
        with env["db"].read() as conn:
            nos = [r["reimburse_no"] for r in env["reimburse_repo"].list(conn)]
        assert all(n and n.startswith("BX-") for n in nos)
        seqs = sorted(int(n.rsplit("-", 1)[1]) for n in nos)
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))   # 全局连续，无撞号

    def test_display_columns_all_batches(self):
        """全部批次模式：核心公共列，隐藏模板专属列。"""
        from app.web.pages import _display_columns
        cols = _display_columns([], all_batches=True)
        keys = [c["key"] for c in cols]
        assert keys == ["id", "reimburse_no", "_template", "submitter",
                        "reimburse_date", "expense_type", "amount", "invoice_no"]

    def test_display_columns_batch_template(self, env):
        """具体批次模式：严格按批次绑定模板渲染列（即使筛选后无行）。"""
        from app.web.pages import _display_columns
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RB2001", "张三", "2026-09-10", "2026-09-01", "2026-09-30", "费用", "99.00", "", "", ""],
        ]), batch_id="batch_C")
        tid = env["reb"].batch_template_id("batch_C")
        cols = _display_columns([], all_batches=False, batch_tpl_id=tid)
        labels = [c["label"] for c in cols]
        # 经典模板表头：含报销单号/金额；备注列被剔除
        assert "报销单号" in labels and "金额" in labels and "备注" not in labels


# =====================================================================
# 高鲁棒性匹配引擎：动态归一化 / 安全锁 / 三轮调度 / 平局
# =====================================================================
class TestRobustMatchEngine:
    """模块一~四：失效维度剔除、安全锁封顶、号码冲突淘汰、平局与三轮调度。"""

    def _pair(self, inv=None, reb=None, company_tax_nos=None):
        from app.domain.matcher import MatchEngine
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        inv = inv or InvoiceDTO(invoice_no="N001", invoice_date="2026-09-05",
                                total_amount=Decimal("100.00"))
        reb = reb or ReimburseDTO(amount=Decimal("100.00"),
                                  period_start="2026-09-01", period_end="2026-09-30")
        return MatchEngine().score_pair(inv, reb, company_tax_nos)

    def test_amount_tiers(self):
        """金额绝对误差：±0.01 -> 1.0；0.02~1.00 -> 0.5；>1.00 -> 0。"""
        from app.domain.dto import InvoiceDTO
        for total, expect in (("100.01", 1.0), ("100.50", 0.5), ("101.50", 0.0)):
            ps = self._pair(InvoiceDTO(invoice_no="N001", invoice_date="2026-09-05",
                                       total_amount=Decimal(total)))
            assert ps.raw_scores["amount"] == expect

    def test_no_mismatch_with_same_amount_is_suspected(self):
        """裁决2：号码不一致但金额一致 + 日期相近 -> 疑似发票号填错，不淘汰。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        from app.domain.matcher import MatchEngine
        ps = self._pair(
            InvoiceDTO(invoice_no="N002", invoice_date="2026-09-05",
                       total_amount=Decimal("100.00")),
            ReimburseDTO(invoice_no="N001", amount=Decimal("100.00"),
                         period_start="2026-09-01", period_end="2026-09-30"))
        assert ps.is_conflict and ps.suspected_no_error and not ps.is_rejected
        assert "invoice_no" in ps.conflict_fields
        assert ps.raw_scores["amount"] == 1.0       # 继续计算其余维度
        assert "invoice_no" not in ps.raw_scores     # 号码维度仍记 0 分
        assert any("疑似发票号填错" in n for n in ps.field_notes)
        assert MatchEngine().decide_status(ps) == "SUSPICIOUS"

    def test_no_mismatch_without_date_still_suspected(self):
        """无日期信息不算反证：号不一致 + 金额一致 -> 仍按疑似号填错处理。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        ps = self._pair(
            InvoiceDTO(invoice_no="N002", invoice_date=None,
                       total_amount=Decimal("100.00")),
            ReimburseDTO(invoice_no="N001", amount=Decimal("100.00")))
        assert ps.suspected_no_error and not ps.is_rejected

    def test_no_mismatch_with_wrong_date_is_rejected(self):
        """日期明确不在期间 -> 疑似号填错不成立，淘汰。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        ps = self._pair(
            InvoiceDTO(invoice_no="N002", invoice_date="2026-01-05",
                       total_amount=Decimal("100.00")),
            ReimburseDTO(invoice_no="N001", amount=Decimal("100.00"),
                         period_start="2026-09-01", period_end="2026-09-30"))
        assert ps.raw_scores["date"] == 0.0
        assert ps.is_rejected and not ps.suspected_no_error

    def test_no_mismatch_with_diff_amount_is_rejected(self):
        """裁决2 反面：号码不一致且金额不同 -> 淘汰（CONFLICT）。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        from app.domain.matcher import MatchEngine
        ps = self._pair(
            InvoiceDTO(invoice_no="N002", invoice_date="2026-09-05",
                       total_amount=Decimal("100.00")),
            ReimburseDTO(invoice_no="N001", amount=Decimal("80.00"),
                         period_start="2026-09-01", period_end="2026-09-30"))
        assert ps.is_conflict and ps.is_rejected
        assert MatchEngine().decide_status(ps) == "CONFLICT"

    def test_low_confidence_channel(self):
        """裁决4：无号 + 金额误差 <= 1.00 元即使不足阈值也捞回（0.64 分陷阱）。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        from app.domain.matcher import MatchEngine
        eng = MatchEngine()
        # 无号、金额差 0.50 -> 归一化 0.583 分（不足 0.70），但应进低置信度通道
        ps = self._pair(
            InvoiceDTO(invoice_no="", invoice_date="2026-09-05",
                       total_amount=Decimal("100.00")),
            ReimburseDTO(amount=Decimal("100.50"),
                         period_start="2026-09-01", period_end="2026-09-30"))
        assert ps.score < 0.70 and ps.is_low_confidence
        assert eng.decide_status(ps) == "SUSPICIOUS"

    def test_low_confidence_not_for_wrong_date(self):
        """裁决4 例外：日期全错时不捞回（无号 + 金额相近也不行）。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        from app.domain.matcher import MatchEngine
        eng = MatchEngine()
        ps = self._pair(
            InvoiceDTO(invoice_no="", invoice_date="2026-01-05",
                       total_amount=Decimal("100.00")),
            ReimburseDTO(amount=Decimal("100.50"),
                         period_start="2026-09-01", period_end="2026-09-30"))
        assert ps.raw_scores["date"] == 0.0
        assert not ps.is_low_confidence
        assert eng.decide_status(ps) == "UNMATCHED"

    def test_low_confidence_not_for_wrong_title(self):
        """裁决4 例外：抬头不属于本公司时不捞回。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        ps = self._pair(
            InvoiceDTO(invoice_no="", invoice_date="2026-09-05",
                       total_amount=Decimal("100.00"),
                       buyer_tax_no="000000000000000000"),
            ReimburseDTO(amount=Decimal("100.50"),
                         period_start="2026-09-01", period_end="2026-09-30"),
            company_tax_nos={"91110108MA01AAAA1A"})
        assert any("抬头" in n for n in ps.field_notes)
        assert not ps.is_low_confidence

    def test_missing_no_or_date_capped_at_094(self):
        """安全锁（仅第二阶段启用）：无号->封顶；双方都没日期->封顶；号一致时安全锁被短路跳过。"""
        from app.domain.matcher import MatchEngine
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        eng = MatchEngine()

        # 场景1：双方都没号（报销单不填号）+ 双方都没日期 -> "发票号、日期缺失" 双封顶
        ps1 = self._pair(InvoiceDTO(invoice_no=None, invoice_date=None,
                                    total_amount=Decimal("100.00")),
                         ReimburseDTO(invoice_no="", amount=Decimal("100.00")))
        assert ps1.capped and ps1.score == 0.94
        assert "发票号" in ps1.cap_reason and "日期" in ps1.cap_reason
        assert eng.decide_status(ps1) == "SUSPICIOUS"

        # 场景2：报销单未填发票号 + 有日期 -> 只有 "发票号缺失" 封顶
        ps2 = self._pair(reb=ReimburseDTO(
            invoice_no="", amount=Decimal("100.00"),
            period_start="2026-09-01", period_end="2026-09-30"))
        assert ps2.capped and ps2.score == 0.94 and "发票号" in ps2.cap_reason
        assert eng.decide_status(ps2) == "SUSPICIOUS"

        # 场景3（关键新语义）：号一致 + 金额一致 + 双方都没日期
        # -> 第一阶段唯一键短路命中，跳过安全锁！auto_matchable=True -> MATCHED
        ps3 = self._pair(InvoiceDTO(invoice_no="N001", invoice_date=None,
                                    total_amount=Decimal("100.00")),
                         ReimburseDTO(invoice_no="N001", amount=Decimal("100.00")))
        assert ps3.auto_matchable and not ps3.capped
        assert ps3.score == 1.0 and eng.decide_status(ps3) == "MATCHED"

        # 场景4（关键新语义）：报销单缺期间但发票有日期 + 号一致 -> 短路跳过安全锁 -> MATCHED
        ps4 = self._pair(InvoiceDTO(invoice_no="N001", invoice_date="2026-09-05",
                                    total_amount=Decimal("100.00")),
                         ReimburseDTO(invoice_no="N001", amount=Decimal("100.00")))
        assert ps4.auto_matchable and ps4.score == 1.0
        assert eng.decide_status(ps4) == "MATCHED"

    def test_empty_dimensions_excluded_not_neutral(self):
        """无日期：直接剔除分母，不给中性分（模块四）。"""
        from app.domain.dto import ReimburseDTO
        ps = self._pair(reb=ReimburseDTO(amount=Decimal("100.00")))   # 无期间也无报销日期
        assert ps.applicable["date"] is False
        assert "date" not in ps.raw_scores

    def test_only_amount_normalized(self):
        """只剩金额维度时按权重归一化：0.5×0.5/0.5 = 0.5。"""
        from app.domain.dto import ReimburseDTO
        ps = self._pair(reb=ReimburseDTO(amount=Decimal("100.50")))
        assert ps.score == 0.5 and ps.capped

    def test_text_similarity_tiers(self):
        """文本/抬头：相似度 >=0.8 -> 1.0；0.4~0.8 -> 0.5；<0.4 -> 0。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        inv = InvoiceDTO(invoice_no="", invoice_date="2026-09-05",
                         total_amount=Decimal("100.00"),
                         seller_name="", item_summary="公司到机场")

        def _reb(remark):
            return ReimburseDTO(amount=Decimal("100.00"), remark=remark,
                                period_start="2026-09-01", period_end="2026-09-30")

        assert self._pair(inv, _reb("公司到机场")).raw_scores["text"] == 1.0
        assert self._pair(inv, _reb("到机场")).raw_scores["text"] == 0.5
        assert self._pair(inv, _reb("购买办公用品")).raw_scores["text"] == 0.0

    def test_company_title_mismatch_zeroes_text(self):
        """抬头不属于本公司 -> 文本维度 0（负向证据）。"""
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        ps = self._pair(
            InvoiceDTO(invoice_no="N001", invoice_date="2026-09-05",
                       total_amount=Decimal("100.00"),
                       buyer_tax_no="000000000000000000"),
            ReimburseDTO(remark="打车费"), company_tax_nos={"91110108MA01AAAA1A"})
        assert ps.raw_scores["text"] == 0.0

    def test_round1_auto_match(self, env):
        """第一轮：号一致 + 金额一致 + 日期有效 -> MATCHED。"""
        env["inv"].import_files([("a.xml", "", xml_invoice("R1N0001", "100.00",
                                                           date="2026-09-05"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R1-1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.00", "R1N0001", "", ""]]))
        matched = env["match"].list_matches(["MATCHED"])
        assert len(matched) == 1 and matched[0]["invoice_no"] == "R1N0001"

    def test_no_invoice_no_never_auto_matched(self, env):
        """报销单未填发票号：无论特征多全，都只能落 SUSPICIOUS。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "NONO0001", "38.00", date="2026-09-05", seller="嘀嘀出行",
            item_name="*运输服务*打车费"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-NONO", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "38.00", "", "", "打车报销"]]))
        assert env["match"].list_matches(["MATCHED"]) == []
        cands = [m for m in env["match"].list_matches(["SUSPICIOUS"])
                 if m.get("reimburse_no") == "R-NONO"]
        assert len(cands) == 1 and cands[0]["match_score"] <= 0.94

    def test_tie_all_candidates_persisted_no_preferred(self, env):
        """裁决3：同分候选全量落库，不选定首选（无「备选候选/首选」标记）。"""
        # 两张发票金额误差 0.01 / 0 都落在 1.0 档 -> 同分；仅展示顺序按误差小的靠前
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("AMT00001", "100.01", date="2026-09-05")),
            ("b.xml", "", xml_invoice("AMT00002", "100.00", date="2026-09-05")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-AMT", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.00", "", "", ""]]))
        rows = [m for m in env["match"].list_matches(["SUSPICIOUS"])
                if m.get("reimburse_no") == "R-AMT"]
        assert len(rows) == 2
        assert {m["invoice_no"] for m in rows} == {"AMT00001", "AMT00002"}
        for m in rows:
            msg = m["message"] or ""
            assert "备选候选" not in msg and "首选" not in msg
            assert "无法自动区分" in msg            # 平局提示（全量展示）

    def test_conflict_amount_diff_rejected(self, env):
        """裁决2 反面：号不一致且金额也不同 -> 淘汰，不产生待人工记录。"""
        env["inv"].import_files([("a.xml", "", xml_invoice("CONF0001", "100.00",
                                                           date="2026-09-05"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-CONF", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "80.00", "CONF9999", "", ""]]))
        assert env["match"].list_matches(["SUSPICIOUS"]) == []
        assert env["match"].list_matches(["CONFLICT"]) == []
        with env["db"].read() as conn:
            row = conn.execute("SELECT remark FROM reimburse WHERE reimburse_no='R-CONF'"
                               ).fetchone()
        assert "未找到可比对的发票" in (row["remark"] or "")

    def test_conflict_amount_same_pushed_to_suspicious(self, env):
        """裁决2：号不一致但金额一致 + 日期相近 -> 疑似发票号填错，进待人工池。"""
        env["inv"].import_files([("a.xml", "", xml_invoice("CONF0001", "100.00",
                                                           date="2026-09-05"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-CONF", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.00", "CONF9999", "", ""]]))
        rows = [m for m in env["match"].list_matches(["SUSPICIOUS"])
                if m.get("reimburse_no") == "R-CONF"]
        assert len(rows) == 1 and rows[0]["invoice_no"] == "CONF0001"
        assert "疑似发票号填错" in (rows[0]["message"] or "")

    def test_low_confidence_rescued_to_suspicious(self, env):
        """裁决4：无号 + 金额误差 <= 1.00 元，分数不足 0.70 也要捞回并标注。"""
        env["inv"].import_files([("a.xml", "", xml_invoice("LOWC0001", "100.00",
                                                           date="2026-09-05"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-LOW", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.50", "", "", ""]]))
        rows = [m for m in env["match"].list_matches(["SUSPICIOUS"])
                if m.get("reimburse_no") == "R-LOW"]
        assert len(rows) == 1 and rows[0]["invoice_no"] == "LOWC0001"
        assert rows[0]["match_score"] < 0.70
        assert "[低置信度推荐]" in (rows[0]["message"] or "")

    def test_low_confidence_not_rescued_on_wrong_date(self, env):
        """裁决4 例外：日期全错时低置信通道不生效，仍判无候选。"""
        env["inv"].import_files([("a.xml", "", xml_invoice("LOWD0001", "100.00",
                                                           date="2026-01-05"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-LOWD", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.50", "", "", ""]]))
        assert env["match"].list_matches(["SUSPICIOUS"]) == []


# =====================================================================
# 差异报告工作台：搜索 / 排序 / 批量绑定 / 并发失效 / 导出列
# =====================================================================
class TestReportWorkbench:

    def _seed(self, env):
        """一张精确匹配 + 一张疑似号填错（待处理），用于验证排序与搜索。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("RPW0001", "100.00", date="2026-09-05")),
            ("b.xml", "", xml_invoice("RPW0002", "200.00", date="2026-09-05")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RPW-A", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "100.00", "RPW0001", "", ""],
            ["RPW-B", "李四", "2026-09-10", "2026-09-01", "2026-09-30",
             "交通费", "200.00", "RPW0009", "", ""],
        ]))
        env["match"].run_matching()

    def test_details_pending_first_then_time_desc(self, env):
        """默认排序：异常（待处理）优先，其次按创建时间倒序。"""
        self._seed(env)
        data = env["report"].report_payload()
        rows = data["details"]
        assert len(rows) >= 2
        assert rows[0]["pending"] is True
        assert rows[0]["reimburse_no"] == "RPW-B"
        assert data["stats"]["pending"] == 1

    def test_search_q_filters_details_and_lists(self, env):
        """关键词同时作用于主表与底部清单；筛选后回到第一页。"""
        self._seed(env)
        hit = env["report"].report_payload(q="RPW-A")
        assert [r["reimburse_no"] for r in hit["details"]] == ["RPW-A"]
        assert hit["pagers"]["dpage"]["page"] == 1

        by_invoice = env["report"].report_payload(q="RPW0002")
        assert any(r["invoice_no"] == "RPW0002" for r in by_invoice["details"])

        miss = env["report"].report_payload(q="不存在的关键词")
        assert miss["details"] == [] and miss["stats"]["pending"] == 1

    def test_page_clamped_when_tail_emptied(self, env):
        """末页被清空时后端把页码钳制回最后一页（前端自动回退上一页）。"""
        self._seed(env)
        data = env["report"].report_payload(page_size=1, dpage=99)
        assert data["pagers"]["dpage"]["page"] == data["pagers"]["dpage"]["pages"]

    def test_per_pager_page_size_independent(self, env):
        """主表与底部 Tab 的分页完全独立：每页条数各自生效、互不影响。"""
        self._seed(env)
        data = env["report"].report_payload(
            page_size=20, page_sizes={"dpage": 50, "ipage": 100})
        assert data["pagers"]["dpage"]["page_size"] == 50
        assert data["pagers"]["ipage"]["page_size"] == 100
        assert data["pagers"]["rpage"]["page_size"] == 20   # 未指定 -> 全局默认
        assert data["pagers"]["apage"]["page_size"] == 20
        # 非法条数回落到默认值（防呆）
        bad = env["report"].report_payload(page_size=20, page_sizes={"dpage": 37})
        assert bad["pagers"]["dpage"]["page_size"] == 20

    def test_batch_bind_many_invoices_to_one_reimburse(self, env):
        """多张发票批量绑定同一报销单：逐票 MATCHED + match_pair + manual + 审计。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("BB000001", "30.00", date="2026-09-05")),
            ("b.xml", "", xml_invoice("BB000002", "50.00", date="2026-09-05")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RBB1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "80.00", "", "", ""]]))
        with env["db"].read() as conn:
            ids = [r["id"] for r in env["invoice_repo"].list(conn)]
            reb = env["reimburse_repo"].list(conn)[0]
        res = env["match"].manual_batch_bind(ids, reb["id"])
        assert res["ok"] is True and len(res["match_ids"]) == 2
        assert res["amount_diff"] == 0.0
        with env["db"].read() as conn:
            for iid in ids:
                row = env["match_repo"].active_for_invoice(conn, iid)
                assert row is not None and row["match_type"] == "manual"
                assert env["invoice_repo"].get(conn, iid)["status"] == "MATCHED"
            pairs = conn.execute(
                "SELECT COUNT(*) AS c FROM match_pair WHERE reimburse_id = ?",
                (reb["id"],)).fetchone()["c"]
            audit = conn.execute(
                "SELECT action FROM audit_log WHERE action='match_manual_batch'").fetchone()
        assert pairs == 2 and audit is not None

    def test_batch_bind_marks_amount_diff_but_still_binds(self, env):
        """汇总金额有差异时允许强绑，并在差异说明打标。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("BD000001", "30.00", date="2026-09-05")),
            ("b.xml", "", xml_invoice("BD000002", "50.00", date="2026-09-05")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RBD1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.00", "", "", ""]]))
        with env["db"].read() as conn:
            ids = [r["id"] for r in env["invoice_repo"].list(conn)]
            reb = env["reimburse_repo"].list(conn)[0]
        res = env["match"].manual_batch_bind(ids, reb["id"])
        assert res["ok"] is True and abs(res["amount_diff"] - 20.0) < 1e-6
        with env["db"].read() as conn:
            row = env["match_repo"].active_for_invoice(conn, ids[0])
        assert "汇总金额差异 ¥20.00" in (row["message"] or "")

    def test_batch_bind_blocked_when_reimburse_occupied(self, env):
        """报销单已有生效匹配时批量绑定被阻断且不产生任何写入。"""
        env["inv"].import_files([
            ("a.xml", "", xml_invoice("BE000001", "100.00", date="2026-09-05")),
            ("b.xml", "", xml_invoice("BE000002", "100.00", date="2026-09-05")),
        ])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RBE1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "100.00", "BE000001", "", ""]]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            ids = [r["id"] for r in env["invoice_repo"].list(conn)]
            reb = env["reimburse_repo"].list(conn)[0]
        res = env["match"].manual_batch_bind(ids, reb["id"])
        assert res["ok"] is False and "已存在生效匹配" in res["message"]

    def test_expect_status_stale_raises(self, env):
        """并发失效：期望状态与库内不一致时抛 StaleError（API 层返回 409）。"""
        env["inv"].import_files([("a.xml", "", xml_invoice(
            "ST000001", "100.00", date="2026-09-05"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RST1", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "120.00", "ST000001", "", ""]]))
        env["match"].run_matching()
        with env["db"].read() as conn:
            m = conn.execute("SELECT * FROM match_result ORDER BY id DESC LIMIT 1").fetchone()
        with pytest.raises(env["match"].StaleError):
            env["match"].quick_fix(match_id=m["id"], reb_fields={"amount": "100.00"},
                                   expect_status="MATCHED")
        # 状态未变（校验失败在任何写入之前）
        with env["db"].read() as conn:
            row = env["match_repo"].get(conn, m["id"])
        assert row["status"] == m["status"]

    def test_export_stream_respects_visible_cols_and_filter(self, env):
        """导出与视图一致：按可见列裁剪表头，并按 filter 过滤行。"""
        import io as _io

        from openpyxl import load_workbook

        self._seed(env)
        stream = env["report"].export_excel_stream(filter="pending", q="",
                                                  cols=["invoice_no", "status"])
        content = b"".join(stream)
        wb = load_workbook(_io.BytesIO(content))
        ws = wb["匹配明细"]
        header = [c.value for c in ws[1]]
        assert header == ["发票号", "状态"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        assert len(rows) == 1 and rows[0][0] == "RPW0002"


# =====================================================================
# 无票号场景：业务特征碰撞（金额 0.4 / 日期 0.3 / 文本 0.3）
# =====================================================================
class TestNoInvoiceFeatureMatch:
    """双方都无发票号时，只按金额、日期、事由/起止地点/票面文字碰撞，绝不看序号。"""

    def _pair(self, inv, reb):
        from app.domain.matcher import MatchEngine
        return MatchEngine().score_pair(inv, reb)

    def _inv(self, amount="40.00", date="2026-08-25", text="出租车 40.00 盈创"):
        from app.domain.dto import InvoiceDTO
        return InvoiceDTO(invoice_no="", invoice_date=date,
                          total_amount=Decimal(amount), traffic_text=text)

    def _reb(self, amount="40.00", date="2026-08-25", row_index=1):
        from app.domain.dto import ReimburseDTO
        return ReimburseDTO(amount=Decimal(amount), reimburse_date=date,
                            reason="送材料", origin="TBD",
                            destination="盈创动力E座", row_index=row_index)

    def test_amount_is_hard_gate(self):
        """金额必须绝对一致（±0.01），不一致直接 0 分。"""
        ps = self._pair(self._inv(), self._reb(amount="40.02"))
        assert ps.score == 0.0 and "amount" in ps.conflict_fields
        assert any("金额不一致" in p for p in ps.match_points)

    def test_date_two_days_half_and_text_hit(self):
        """日期相差 2 天得半分；票面命中「盈创」得高文本分。"""
        ps = self._pair(self._inv(date="2026-08-27"), self._reb(date="2026-08-25"))
        assert ps.raw_scores["date"] == 0.5
        assert ps.raw_scores["text"] == 0.8          # 「盈创」两字命中
        assert any("盈创" in p for p in ps.match_points)
        assert ps.score == 0.79                       # 0.4 + 0.15 + 0.24
        assert ps.capped and "无发票号" in ps.cap_reason

    def test_date_beyond_tolerance_zero(self):
        ps = self._pair(self._inv(date="2026-09-10"), self._reb(date="2026-08-25"))
        assert ps.raw_scores["date"] == 0.0
        assert any("超出 2 天容差" in p for p in ps.match_points)

    def test_row_index_never_affects_score(self):
        """序号/行号变化不影响评分（禁止按物理行号匹配）。"""
        a = self._pair(self._inv(), self._reb(row_index=1))
        b = self._pair(self._inv(), self._reb(row_index=99))
        assert a.score == b.score == 0.94

    def _seed_traffic(self, env, n=3):
        import json
        ids = []
        with env["db"].tx() as conn:
            for i in range(n):
                ids.append(env["invoice_repo"].insert(conn, {
                    "file_hash": f"traffic-hash-{i}",
                    "file_type": "jpg",
                    "parse_engine": "ocr",
                    "invoice_no": "",
                    "invoice_date": "2026-08-25",
                    "seller_name": "出租车",
                    "total_amount": "40.00",
                    "status": "NORMAL",
                    "raw_json": json.dumps({
                        "traffic": {"text": "出租车 40.00 盈创 08-25",
                                    "plate_no": "", "ride_time": "2026-08-25 09:12"},
                    }, ensure_ascii=False),
                }))
        env["reb"].import_excel("r.xlsx", make_excel([
            ["R-TRAFFIC", "张三", "2026-08-25", "", "", "交通费", "40.00", "",
             "", "TBD到盈创动力E座 送材料"]]))
        return ids

    def test_tie_candidates_and_delete_isolated(self, env):
        """平局候选全部列出；删除一张发票不影响其他发票的匹配状态。"""
        ids = self._seed_traffic(env)
        env["match"].run_matching()
        with env["db"].read() as conn:
            rows = conn.execute(
                "SELECT invoice_id, status, match_points FROM match_result "
                "WHERE reimburse_id IS NOT NULL ORDER BY invoice_id").fetchall()
        assert len(rows) == 3
        assert all(r["status"] == "SUSPICIOUS" for r in rows)
        assert all("盈创" in (r["match_points"] or "") for r in rows)

        # 并排比对数据包：推荐发票 + 3 张平局候选
        payload = env["report"].compare_payload(env["match"].list_matches(["SUSPICIOUS"])[0]["id"])
        assert "盈创动力E座" in payload["reimburse"]["destination"]
        assert len(payload["candidates"]) == 3
        assert any("盈创" in p for p in payload["invoice"]["match_points"])

        # 删除一张发票：只级联删除它自己的匹配记录，其余两张状态不变
        env["inv"].delete_invoice(ids[0])
        with env["db"].read() as conn:
            left = conn.execute(
                "SELECT invoice_id, status FROM match_result "
                "WHERE reimburse_id IS NOT NULL ORDER BY invoice_id").fetchall()
        assert [r["invoice_id"] for r in left] == ids[1:]
        assert all(r["status"] == "SUSPICIOUS" for r in left)


# =====================================================================
# 模块二/三/五：人工干预强制原因 + 审计 detail 结构化 JSON
# =====================================================================
class TestManualReasonAuditStruct:
    """人工干预必须带原因依据；match_result 说明与 audit_log.detail 同步结构化。"""

    def _pair(self, env, inv_no="ANI000001", amount="29.30", reb_amount=None,
              run_match=True):
        env["inv"].import_files([("a.xml", "", xml_invoice(
            inv_no, amount, date="2026-09-01", seller="测试供应商"))])
        env["reb"].import_excel("r.xlsx", make_excel([
            ["", "张三", "2026-09-01", "2026-09-01", "2026-09-30",
             "费用", reb_amount or amount, inv_no, "2026-09-01", ""],
        ]))
        if run_match:
            env["match"].run_matching()
        with env["db"].read() as conn:
            inv = env["invoice_repo"].list(conn)[0]
            reb = env["reimburse_repo"].list(conn)[0]
            m = conn.execute(
                "SELECT * FROM match_result WHERE invoice_id = ? AND reimburse_id = ?",
                (inv["id"], reb["id"])).fetchone()
        return inv, reb, dict(m) if m else None

    def test_special_reason_requires_note(self, env):
        """选「特殊情况」未填说明：confirm/reject/unmatch/manual/batch/quick_fix 全部拦截。"""
        inv, reb, m = self._pair(env)
        assert m is not None
        results = [
            env["match"].confirm(m["id"], "admin", "", "", "特殊情况"),
            env["match"].reject(m["id"], "admin", "", "", "特殊情况"),
            env["match"].unmatch(m["id"], "admin", "", "", "特殊情况"),
            env["match"].manual_match(inv["id"], reb["id"], "admin", "特殊情况", ""),
            env["match"].manual_batch_bind([inv["id"]], reb["id"], "admin", "特殊情况", ""),
            env["match"].quick_fix(match_id=m["id"], reb_fields={"amount": "29.50"},
                                   reason_option="特殊情况", reason=""),
        ]
        for res in results:
            assert res["ok"] is False and "备注" in res["message"]
        # 拦截发生在写入之前：状态与金额均未变
        with env["db"].read() as conn:
            assert env["match_repo"].get(conn, m["id"])["status"] == m["status"]
            assert env["reimburse_repo"].get(conn, reb["id"])["amount"] == "29.30"

    def test_manual_bind_message_and_structured_audit(self, env):
        """人工绑定：说明带业务单号与依据；审计 detail 存 JSON（单号/依据/变更）。"""
        from app.service import audit_service
        # 金额差 199 元 -> 唯一键金额冲突（CONFLICT），发票仍为 NORMAL，可走人工绑定
        inv, reb, _ = self._pair(env, reb_amount="199.00")
        assert reb["reimburse_no"].startswith("BX-")   # 缺号导入自动生成全局单号
        res = env["match"].manual_match(inv["id"], reb["id"], "admin", "业务人员已确认")
        assert res["ok"] is True
        with env["db"].read() as conn:
            row = conn.execute(
                "SELECT message FROM match_result WHERE invoice_id = ? AND status = 'CONFIRMED'",
                (inv["id"],)).fetchone()
            aud = conn.execute(
                "SELECT detail FROM audit_log WHERE action = 'match_manual' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        assert row["message"] == (f"人工绑定 [发票:{inv['invoice_no']}] "
                                  f"至 [报销单:{reb['reimburse_no']}]（依据: 业务人员已确认）")
        detail = audit_service.parse_detail(aud["detail"])
        assert detail["invoice_no"] == inv["invoice_no"]
        assert detail["reimburse_no"] == reb["reimburse_no"]
        assert detail["reason_option"] == "业务人员已确认"
        assert {"field": "绑定依据", "old": "-", "new": "业务人员已确认"} in detail["changes"]

    def test_quick_fix_message_and_change_pairs(self, env):
        """快捷修复：说明按「报销单 + 金额 旧->新 + 发票」输出；审计 detail 存变更对。"""
        from app.service import audit_service
        inv, reb, m = self._pair(env)
        assert m is not None
        res = env["match"].quick_fix(match_id=m["id"], reb_fields={"amount": "29.50"},
                                     reason_option="员工补了凭证", operator="admin")
        assert res["ok"] is True
        with env["db"].read() as conn:
            msg = env["match_repo"].get(conn, m["id"])["message"]
            raw_detail = env["match_repo"].get(conn, m["id"])["detail"]
            aud = conn.execute(
                "SELECT detail FROM audit_log WHERE action = 'match_quick_fix' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        assert msg.startswith(f"快捷修复 [报销单:{reb['reimburse_no']}] ")
        assert "(金额: 29.30 -> 29.50)" in msg
        assert f"匹配 [发票:{inv['invoice_no']}]" in msg
        detail = audit_service.parse_detail(aud["detail"])
        assert {"field": "报销金额", "old": "29.30", "new": "29.50"} in detail["changes"]
        assert detail["reason_option"] == "员工补了凭证"
        # 模块三.2：match_result.detail 与 audit_log.detail 同源（结构化 JSON）
        import json
        md = json.loads(raw_detail or "{}")
        assert md["reimburse_no"] == reb["reimburse_no"]
        assert md["invoice_no"] == inv["invoice_no"]
        assert {"field": "报销金额", "old": "29.30", "new": "29.50"} in md["changes"]

    def test_parse_detail_legacy_text(self):
        """历史纯文本 detail 仍可解析（降级为 legacy 字段）。"""
        from app.service import audit_service
        assert audit_service.parse_detail("人工确认：业务已核实") == {
            "legacy": "人工确认：业务已核实"}
        assert audit_service.parse_detail("") == {}

    def test_display_columns_puts_no_right_after_seq(self, env):
        """具体批次模式：前两列固定为「序号 / 报销单号」（模块一列顺序）。"""
        from app.web.pages import _display_columns
        env["reb"].import_excel("r.xlsx", make_excel([
            ["RB3001", "张三", "2026-09-10", "2026-09-01", "2026-09-30",
             "费用", "99.00", "", "", ""],
        ]), batch_id="batch_D")
        cols = _display_columns([], all_batches=False,
                                batch_tpl_id=env["reb"].batch_template_id("batch_D"))
        assert [c["label"] for c in cols[:2]] == ["序号", "报销单号"]



