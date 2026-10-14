"""生成功能测试数据（第二批，发票号与第一批不冲突）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

XML_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <Header>
    <InvoiceNo>{no}</InvoiceNo>
    <InvoiceCode></InvoiceCode>
    <InvoiceDate>{date}</InvoiceDate>
  </Header>
  <Buyer><BuyerName>{buyer}</BuyerName><BuyerTaxID>{buyer_tax}</BuyerTaxID></Buyer>
  <Seller><SellerName>{seller}</SellerName><SellerTaxID>{seller_tax}</SellerTaxID></Seller>
  <Items><Item><ItemName>{item}</ItemName><Amount>{amount}</Amount><TaxRate>{rate}</TaxRate><TaxAmount>{tax}</TaxAmount></Item></Items>
  <Settlement><TotalTaxInclAmount>{total}</TotalTaxInclAmount></Settlement>
</Invoice>
"""

BUYER, BTAX = "北京示例科技有限公司", "91110108MA01AAAA1A"
rows = [
    # 用例1: 精确匹配（号一致+金额一致+期间内）-> 应自动 MATCHED (score=1.0)
    ("t2_exact.xml", "251000000001", "2026-09-05", "上海供应商有限公司", "91310000MA01BBBB2B",
     "*信息技术服务*软件服务费", "88.50", "13%", "11.51", "100.01"),
    # 用例2: 报销单登记发票号错误（无此号）-> 发票应 UNMATCHED 或由金额兜底 SUSPICIOUS
    ("t2_orphan.xml", "251000000002", "2026-09-10", "上海供应商有限公司", "91310000MA01BBBB2B",
     "*信息技术服务*咨询服务费", "442.48", "13%", "57.52", "500.00"),
    # 用例3: 发票号一致但金额差异大 -> 应 CONFLICT
    ("t2_conflict.xml", "251000000003", "2026-09-01", "杭州办公用品商行", "91330000MA01CCCC3C",
     "*办公用品*打印纸", "176.99", "13%", "23.01", "200.00"),
    # 用例4: 报销单未登记发票号，仅金额一致 -> 应 SUSPICIOUS
    ("t2_fuzzy.xml", "251000000004", "2026-09-20", "广州差旅服务公司", "91440000MA01DDDD4D",
     "*运输服务*机票款", "796.46", "9%", "71.68", "868.14"),
    # 用例5: 缺失必填字段（无金额）-> 应 ABNORMAL
    ("t2_broken.xml", "251000000005", "2026-09-22", BUYER, BTAX,
     "*测试*缺字段", "", "", "", ""),
    # 用例6: 金额加税额不等价税合计（超±0.01）-> 应 ABNORMAL (AMOUNT_MISMATCH)
    ("t2_mismatch.xml", "251000000006", "2026-09-23", "深圳检测公司", "91440300MA01EEEE5E",
     "*检测服务*检测费", "100.00", "6%", "6.00", "999.00"),
]

out_dir = os.path.join(os.path.dirname(__file__), "sample_invoices_t2")
os.makedirs(out_dir, exist_ok=True)
for fn, no, date, seller, stax, item, amt, rate, tax, total in rows:
    buyer, btax = (BUYER, BTAX) if fn != "t2_broken.xml" else (seller, stax)
    with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
        f.write(XML_TPL.format(no=no, date=date, buyer=buyer, buyer_tax=btax,
                               seller=seller, seller_tax=stax, item=item,
                               amount=amt, rate=rate, tax=tax, total=total))

from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.append(["报销单号", "提交人", "报销日期", "期间起", "期间止", "费用类型", "金额", "发票号", "发票日期", "备注"])
# 对应 用例1 精确
ws.append(["BX202610001", "张三", "2026-10-08", "2026-09-01", "2026-09-30", "软件服务费", "100.01", "251000000001", "2026-09-05", "9月软件服务"])
# 对应 用例2：登记的发票号不存在
ws.append(["BX202610002", "李四", "2026-10-08", "2026-09-01", "2026-09-30", "咨询服务费", "500.00", "251000009999", "2026-09-10", "发票号填错"])
# 对应 用例3：号一致金额差
ws.append(["BX202610003", "王五", "2026-10-09", "2026-09-01", "2026-09-30", "办公用品", "180.00", "251000000003", "2026-09-01", "金额填错"])
# 对应 用例4：无发票号，仅金额一致
ws.append(["BX202610004", "赵六", "2026-10-09", "2026-09-01", "2026-09-30", "差旅费", "868.14", "", "", "未登记发票号"])
# 无任何对应发票
ws.append(["BX202610005", "张三", "2026-10-10", "2026-10-01", "2026-10-31", "杂费", "66.66", "", "", "无对应发票"])
# 非法行：缺金额（应被跳过并报错）
ws.append(["BX202610006", "测试", "2026-10-10", "", "", "测试", "", "", "", "金额为空行"])
wb.save(os.path.join(out_dir, "reimburses_t2.xlsx"))
print("生成完成:", out_dir)
