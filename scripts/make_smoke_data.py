"""生成冒烟测试数据：2 张数电发票 XML + 1 张红冲 XML + 1 个报销单 Excel。"""
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
  <Buyer>
    <BuyerName>{buyer}</BuyerName>
    <BuyerTaxID>{buyer_tax}</BuyerTaxID>
  </Buyer>
  <Seller>
    <SellerName>{seller}</SellerName>
    <SellerTaxID>{seller_tax}</SellerTaxID>
  </Seller>
  <Items>
    <Item>
      <ItemName>{item}</ItemName>
      <Amount>{amount}</Amount>
      <TaxRate>{rate}</TaxRate>
      <TaxAmount>{tax}</TaxAmount>
    </Item>
  </Items>
  <Settlement>
    <TotalTaxInclAmount>{total}</TotalTaxInclAmount>
  </Settlement>
</Invoice>
"""

rows = [
    # (文件名, no, date, buyer, buyer_tax, seller, seller_tax, item, amount, rate, tax, total)
    ("inv_match.xml", "241200000001", "2026-06-05", "北京示例科技有限公司",
     "91110108MA01AAAA1A", "上海供应商有限公司", "91310000MA01BBBB2B",
     "*信息技术服务*软件服务费", "88.50", "13%", "11.51", "100.01"),
    ("inv_wrong_no.xml", "241200000002", "2026-06-10", "北京示例科技有限公司",
     "91110108MA01AAAA1A", "上海供应商有限公司", "91310000MA01BBBB2B",
     "*信息技术服务*咨询服务费", "442.48", "13%", "57.52", "500.00"),
    ("inv_amount_diff.xml", "241200000003", "2026-07-01", "北京示例科技有限公司",
     "91110108MA01AAAA1A", "杭州办公用品商行", "91330000MA01CCCC3C",
     "*办公用品*打印纸", "176.99", "13%", "23.01", "200.00"),
    ("inv_red.xml", "241200000004", "2026-07-15", "北京示例科技有限公司",
     "91110108MA01AAAA1A", "上海供应商有限公司", "91310000MA01BBBB2B",
     "*信息技术服务*软件服务费红冲", "-88.50", "13%", "-11.51", "-100.01"),
    ("inv_amount_only.xml", "241200000005", "2026-07-20", "北京示例科技有限公司",
     "91110108MA01AAAA1A", "广州差旅服务公司", "91440000MA01DDDD4D",
     "*运输服务*机票款", "796.46", "9%", "71.68", "868.14"),
]

out_dir = os.path.join(os.path.dirname(__file__), "sample_invoices")
os.makedirs(out_dir, exist_ok=True)
for fn, no, date, buyer, btax, seller, stax, item, amt, rate, tax, total in rows:
    with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
        f.write(XML_TPL.format(no=no, date=date, buyer=buyer, buyer_tax=btax,
                               seller=seller, seller_tax=stax, item=item,
                               amount=amt, rate=rate, tax=tax, total=total))

# 报销单 Excel
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.append(["报销单号", "提交人", "报销日期", "期间起", "期间止", "费用类型", "金额", "发票号", "发票日期", "备注"])
ws.append(["BX202607001", "张三", "2026-07-05", "2026-06-01", "2026-06-30", "软件服务费", "100.01", "241200000001", "2026-06-05", "6月软件服务"])
ws.append(["BX202607002", "李四", "2026-07-06", "2026-06-01", "2026-06-30", "咨询服务费", "500.00", "241200000099", "2026-06-10", "发票号填错了"])
ws.append(["BX202607003", "王五", "2026-07-16", "2026-07-01", "2026-07-31", "办公用品", "180.00", "241200000003", "2026-07-01", "金额填错"]) 
ws.append(["BX202607004", "赵六", "2026-07-25", "2026-07-01", "2026-07-31", "差旅费", "868.14", "", "", "未登记发票号"])
ws.append(["BX202607005", "张三", "2026-07-26", "2026-07-01", "2026-07-31", "杂费", "66.66", "", "", "无对应发票"])
xlsx_path = os.path.join(out_dir, "reimburses.xlsx")
wb.save(xlsx_path)

print("生成完成:", out_dir)
