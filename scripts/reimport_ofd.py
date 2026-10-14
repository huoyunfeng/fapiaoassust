"""重新导入此前解析失败的 OFD 发票：删除旧 ABNORMAL 记录并按新解析器重导。"""
import os
import sys

sys.path.insert(0, ".")

from app.repository import db
from app.service import invoice_service

with db.read() as conn:
    rows = conn.execute(
        "SELECT id, source_path, status FROM invoice WHERE file_type='ofd'"
    ).fetchall()

print(f"找到 {len(rows)} 条 OFD 记录")
results = []
for r in rows:
    path = r["source_path"]
    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError as e:
        print(f"#{r['id']} 文件不可读，跳过: {e}")
        continue
    res = invoice_service.delete_invoice(r["id"], operator="admin")
    if not res["ok"]:
        print(f"#{r['id']} 删除失败: {res['message']}")
        continue
    out = invoice_service.import_files([(name, "", content)], operator="admin")[0]
    results.append(out)
    print(f"#{r['id']} {name} -> ok={out['ok']} status={out['status']} {out['message'][:50]}")

print("完成:", sum(1 for x in results if x["ok"]), "/", len(results))
