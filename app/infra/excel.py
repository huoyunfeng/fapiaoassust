"""Excel 基础设施：报销单导入解析 + 差异报告导出。可替换（openpyxl）。"""
import io
from typing import Any, Dict, List, Tuple

from openpyxl import Workbook, load_workbook

# 报销单模板表头 -> DTO 字段（宽容匹配：去空格后包含匹配）
_HEADER_MAP = {
    "报销单号": "reimburse_no",
    "单号": "reimburse_no",
    "报销编号": "reimburse_no",
    "提交人": "submitter",
    "报销人": "submitter",
    "报销日期": "reimburse_date",
    "期间起": "period_start",
    "期间开始": "period_start",
    "期间止": "period_end",
    "期间结束": "period_end",
    "费用类型": "expense_type",
    "金额": "amount",
    "报销金额": "amount",
    "发票号": "invoice_no",
    "发票号码": "invoice_no",
    "发票日期": "invoice_date",
    "备注": "remark",
}


def read_reimburses(content: bytes) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """解析报销单 Excel。

    返回 (rows, errors)：
      rows: [{field: 原始字符串值, "row_index": Excel 实际行号}]
      errors: [{"row_index": 行号, "message": 错误}]
    """
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        return [], [{"row_index": 0, "message": f"无法打开 Excel: {e}"}]

    ws = wb.active
    if ws is None:
        return [], [{"row_index": 0, "message": "Excel 无工作表"}]

    header_row = None
    col_map: Dict[int, str] = {}
    for r_idx, row in enumerate(ws.iter_rows(max_row=5), start=1):
        values = [str(c.value).strip() if c.value is not None else "" for c in row]
        mapped = {}
        for col_idx, v in enumerate(values, start=1):
            key = _HEADER_MAP.get(v)
            if not key:
                for h, k in _HEADER_MAP.items():
                    if v and (h in v or v in h) and k not in mapped.values():
                        key = k
                        break
            if key and key not in mapped.values():
                mapped[col_idx] = key
        if {"reimburse_no", "amount"} <= set(mapped.values()):
            header_row, col_map = r_idx, mapped
            break

    if header_row is None:
        wb.close()
        return [], [{"row_index": 0,
                     "message": "未找到表头行（需包含: 报销单号、金额）"}]

    for r_idx, row in enumerate(ws.iter_rows(min_row=header_row + 1), start=header_row + 1):
        record: Dict[str, Any] = {"row_index": r_idx}
        empty = True
        for c in row:
            key = col_map.get(c.column)
            if not key:
                continue
            v = c.value
            if v is not None and str(v).strip() != "":
                empty = False
                record[key] = str(v).strip() if not isinstance(v, (int, float)) else v
        if empty:
            continue
        rows.append(record)
    wb.close()
    return rows, errors


# =====================================================================
# 差异报告导出
# =====================================================================

def export_report(sheets: List[Tuple[str, List[str], List[List[Any]]]]) -> bytes:
    """导出差异报告。sheets: [(sheet名, 表头, 行数据)]。返回 xlsx 字节。"""
    wb = Workbook()
    wb.remove(wb.active)
    for name, header, rows in sheets:
        ws = wb.create_sheet(title=name[:31])
        ws.append(header)
        for row in rows:
            ws.append(list(row))
        # 简单列宽
        for i, h in enumerate(header, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, len(h) * 2 + 4)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
