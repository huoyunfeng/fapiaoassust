# -*- coding: utf-8 -*-
"""修复已入库 PDF/OFD 发票的购买方/销售方名称、税号、税率（按修复后的解析器重新解析源文件）。"""
import sys
sys.path.insert(0, r'e:\fapiaosystem\fapiaocheck')
from app.domain.parsers import PdfTextParser, OfdInvoiceParser, ParseTask
from app.repository import db

parsers = {'pdf': PdfTextParser(), 'ofd': OfdInvoiceParser()}
fixed, skipped = 0, 0
with db.tx() as conn:
    rows = conn.execute(
        "SELECT id, source_path, file_type, buyer_name, buyer_tax_no, seller_name, seller_tax_no, tax_rate, item_summary "
        "FROM invoice WHERE file_type IN ('pdf','ofd')"
    ).fetchall()
    for r in rows:
        r = dict(r)
        parser = parsers[r['file_type']]
        try:
            with open(r['source_path'], 'rb') as f:
                content = f.read()
            dto = parser.parse(ParseTask(file_name=r['source_path'], file_type=r['file_type'], content=content))
        except Exception as e:
            print(f"#{r['id']} 重解析失败: {e}")
            skipped += 1
            continue
        changed = {}
        for field in ('buyer_name', 'buyer_tax_no', 'seller_name', 'seller_tax_no', 'tax_rate'):
            new_v = getattr(dto, field) or ''
            if new_v and new_v != (r[field] or ''):
                changed[field] = new_v
        new_summary = dto.item_summary or ''
        if new_summary and new_summary != (r.get('item_summary') or ''):
            changed['item_summary'] = new_summary
        if changed:
            sets = ', '.join(f"{k}=?" for k in changed)
            conn.execute(
                f"UPDATE invoice SET {sets}, updated_at=datetime('now','localtime') WHERE id=?",
                list(changed.values()) + [r['id']])
            print(f"#{r['id']} 修复: {changed}")
            fixed += 1
print(f"完成：修复 {fixed} 条，跳过 {skipped} 条")
