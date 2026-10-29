"""Excel 基础设施：报销单导入解析 + 差异报告导出。可替换（openpyxl）。

重构后由模板 fields_config 驱动解析，支持：
- 自定义表头行 / 数据起始行；
- 一行多票（one_to_many）按分隔符拆分；
- 多行一票（many_to_one）合并；
- 自定义字段落入 extra_data。
"""
import io
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook


# 兼容旧调用：经典表头别名（仅在未指定模板时作为兜底）
_LEGACY_HEADER_MAP = {
    "报销单号": "reimburse_no", "单号": "reimburse_no", "报销编号": "reimburse_no",
    "提交人": "submitter", "报销人": "submitter",
    "报销日期": "reimburse_date",
    "期间起": "period_start", "期间开始": "period_start",
    "期间止": "period_end", "期间结束": "period_end",
    "费用类型": "expense_type",
    "金额": "amount", "报销金额": "amount",
    "发票号": "invoice_no", "发票号码": "invoice_no",
    "发票日期": "invoice_date",
    "备注": "remark",
}


def read_headers(content: bytes, max_scan: int = 5) -> Tuple[List[str], int]:
    """读取 Excel 表头。

    扫描前 max_scan 行，取非空单元格最多的一行作为表头行。
    返回 (headers, header_row_index)，header_row_index 为 1-based。
    """
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        return [], 0
    ws = wb.active
    if ws is None:
        wb.close()
        return [], 0

    best_headers: List[str] = []
    best_row = 0
    for r_idx, row in enumerate(ws.iter_rows(max_row=max_scan), start=1):
        values = [str(c.value).strip() if c.value is not None else "" for c in row]
        non_empty = [v for v in values if v]
        if len(non_empty) > len(best_headers):
            best_headers = non_empty
            best_row = r_idx
    wb.close()
    return best_headers, best_row


def _cell_value(c) -> Any:
    v = c.value
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    return str(v).strip()


def _cells_1based(row, min_col: int = 1):
    """按 1-based 列号枚举一行单元格。

    read_only 模式下，行内缺失的单元格会被 openpyxl 补成 EmptyCell，
    而 EmptyCell 没有 .column 属性（会抛 AttributeError），
    因此列号必须来自枚举下标，不能取 cell.column。
    """
    for offset, c in enumerate(row):
        yield min_col + offset, c


def read_reimburses(content: bytes,
                    fields_config: Optional[dict] = None
                    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """解析报销单 Excel。

    若提供 fields_config（含 fields 与 options），按模板解析；
    否则使用经典兜底映射（向后兼容）。

    返回 (rows, errors)：
      rows: [{field_key: value, "row_index": Excel 行号, "_extra": {自定义字段}}]
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
        wb.close()
        return [], [{"row_index": 0, "message": "Excel 无工作表"}]

    options = (fields_config or {}).get("options", {}) or {}
    header_row = int(options.get("header_row", 1))

    if fields_config:
        # ---- 模板驱动：在候选行中自动定位真正的表头行 ----
        # 报销单常带合并标题行（如「餐费报销明细表」），不能写死 header_row
        field_defs = fields_config.get("fields", [])
        header_row, col_map = _locate_header(ws, field_defs, header_row)
    else:
        # ---- 经典兜底：自动识别表头行 ----
        col_map, header_row = _legacy_detect_header(ws)

    data_start_row = header_row + 1

    # 表头行全部列名：模板未映射的列（事由 / 起止地点 / 车牌号等业务特征）
    # 也要落进 extra_data，否则无票号场景拿不到这些字段
    headers_by_col: Dict[int, str] = {}
    for hrow in ws.iter_rows(min_row=header_row, max_row=header_row,
                             min_col=1, max_col=ws.max_column):
        for col_idx, c in _cells_1based(hrow):
            hv = str(c.value).strip() if c.value is not None else ""
            if hv:
                headers_by_col[col_idx] = hv

    if not col_map:
        wb.close()
        expected = [fd.get("excel_header") for fd in (fields_config or {}).get("fields", [])]
        found = _scan_header_texts(ws, header_row)
        return [], [{
            "row_index": 0,
            "message": "未找到匹配的表头。模板期望列：%s；Excel 实际表头：%s"
                       % ("、".join(x for x in expected if x) or "—",
                          "、".join(found) or "—"),
        }]

    # ---- 读取数据行 ----
    for r_idx, row in enumerate(ws.iter_rows(min_row=data_start_row, min_col=1,
                                             max_col=ws.max_column),
                                start=data_start_row):
        record: Dict[str, Any] = {"row_index": r_idx}
        extra: Dict[str, Any] = {}
        empty = True
        for col_idx, c in _cells_1based(row):
            fd = col_map.get(col_idx)
            if fd is None:
                # 未映射列：表头存在且单元格非空时自动落 extra_data，
                # 保留「事由 / 起始地点 / 到达地点 / 车牌号」等业务特征
                header_name = headers_by_col.get(col_idx)
                v = _cell_value(c)
                if header_name and v is not None and str(v).strip() != "":
                    extra.setdefault(header_name, v)
                continue
            v = _cell_value(c)
            if v is None or str(v).strip() == "":
                continue
            empty = False
            if fd.get("is_custom"):
                # 自定义字段 key 用表头显示名，保证多列之间唯一且可读
                extra[fd.get("display_name") or fd.get("excel_header") or f"col{col_idx}"] = v
            else:
                record[fd["system_key"]] = v
        if empty:
            continue
        record["_extra"] = extra
        rows.append(record)

    wb.close()
    return rows, errors


def _locate_header(ws, field_defs: List[dict],
                   fallback_row: int = 1, max_scan: int = 15
                   ) -> Tuple[int, Dict[int, dict]]:
    """在 Excel 前 max_scan 行中定位真正的表头行。

    报销单常在首行放合并标题（如「餐费报销明细表」），
    因此按"与模板 excel_header 匹配数最多"的行作为表头行。
    返回 (header_row, {列号: field_def})。
    """
    wanted = {fd["excel_header"].strip(): fd for fd in field_defs if fd.get("excel_header")}
    best_row, best_map = fallback_row, {}
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan, min_col=1,
                                             max_col=ws.max_column), start=1):
        cmap: Dict[int, dict] = {}
        used_keys = set()
        for col_idx, c in _cells_1based(row):
            v = str(c.value).strip() if c.value is not None else ""
            fd = wanted.get(v)
            if fd is None:
                continue
            # 同一系统字段只绑定一次，避免重复列互相覆盖
            key = fd.get("system_key")
            if key in used_keys:
                continue
            used_keys.add(key)
            cmap[col_idx] = fd
        if len(cmap) > len(best_map):
            best_map, best_row = cmap, r_idx
    return best_row, best_map


def _scan_header_texts(ws, header_row: int, max_scan: int = 15) -> List[str]:
    """扫描候选表头行文本，用于错误提示。"""
    texts: List[str] = []
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan), start=1):
        for c in row:
            v = str(c.value).strip() if c.value is not None else ""
            if v and v not in texts:
                texts.append(v)
    return texts[:20]


def _legacy_detect_header(ws) -> Tuple[Dict[int, dict], int]:
    """旧版自动表头识别（向后兼容）。"""
    col_map: Dict[int, dict] = {}
    header_row = 0
    for r_idx, row in enumerate(ws.iter_rows(max_row=5, min_col=1, max_col=ws.max_column),
                                start=1):
        mapped: Dict[int, dict] = {}
        for col_idx, c in _cells_1based(row):
            v = str(c.value).strip() if c.value is not None else ""
            key = _LEGACY_HEADER_MAP.get(v)
            if not key:
                for h, k in _LEGACY_HEADER_MAP.items():
                    if v and (h in v or v in h) and k not in {m.get("system_key") for m in mapped.values()}:
                        key = k
                        break
            if key and key not in {m.get("system_key") for m in mapped.values()}:
                mapped[col_idx] = {"system_key": key, "excel_header": v}
        if {"reimburse_no", "amount"} <= {m.get("system_key") for m in mapped.values()}:
            col_map, header_row = mapped, r_idx
            break
    return col_map, header_row


def split_multi_invoice(rows: List[dict], separator: str = ",") -> List[dict]:
    """一行多票拆分（invoice_mode=one_to_many）。

    若 invoice_no 含分隔符，按分隔符拆成多行，金额按票数均分。
    其他字段复制，split_type=one_to_many。
    拆分后的行用 row_index + i/1000 偏移避免唯一索引冲突。
    """
    out: List[dict] = []
    for r in rows:
        inv_no = r.get("invoice_no")
        if inv_no and separator in str(inv_no):
            parts = [p.strip() for p in str(inv_no).split(separator) if p.strip()]
            n = len(parts)
            if n <= 1:
                out.append(r)
                continue
            amount = r.get("amount")
            try:
                per = float(amount) / n if amount else None
            except (TypeError, ValueError):
                per = amount
            base_row = r.get("row_index", 0)
            for i, p in enumerate(parts):
                nr = dict(r)
                nr["invoice_no"] = p
                nr["row_index"] = base_row + (i + 1) * 0.001
                if per is not None:
                    nr["amount"] = round(per, 2) if i < n - 1 else round(
                        float(amount) - per * (n - 1), 2)
                nr["split_type"] = "one_to_many"
                nr["_extra"] = dict(r.get("_extra", {}))
                out.append(nr)
        else:
            out.append(r)
    return out


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
        for i, h in enumerate(header, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, len(h) * 2 + 4)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_report_stream(sheets: List[Tuple[str, List[str], List[Any]]],
                         chunk_size: int = 1 << 16):
    """流式导出差异报告：write_only 工作簿落盘后分块读出。

    sheets 中的 rows 支持任意可迭代对象（可传生成器，边查边写），
    大报表不把整表读进内存，配合 StreamingResponse 逐块下发。
    """
    import tempfile

    wb = Workbook(write_only=True)
    for name, header, rows in sheets:
        ws = wb.create_sheet(title=name[:31])
        ws.append(list(header))
        for row in rows:
            ws.append(list(row))

    tmp = tempfile.SpooledTemporaryFile(max_size=8 << 20)
    wb.save(tmp)
    tmp.seek(0)

    def _iter_chunks():
        try:
            while True:
                chunk = tmp.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            tmp.close()

    return _iter_chunks()
