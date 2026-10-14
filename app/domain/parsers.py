"""解析引擎（设计 §6.1）：降级链 XML -> OFD -> PDF_TEXT -> OCR -> MANUAL。

- 终止条件：任一环节解析成功（必填字段齐全）即停止降级；
- 失败定义：必填字段 invoice_no / invoice_date / total_amount 任一缺失；
- 每次降级记录原因到 raw_json.degrade_chain；
- 全部失败 -> 状态置 ABNORMAL，不阻塞其他文件。

解析器为纯函数：输入 ParseTask（文件内容已在内存），输出 InvoiceDTO（未标准化），
由 ParserEngine 统一调用 NormalizeEngine 标准化后再判定必填字段。
"""
import io
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, List, Optional

from app.domain.dto import InvoiceDTO, ParseResult, ParseTask
from app.domain.normalize import NormalizeEngine

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ["invoice_no", "invoice_date", "total_amount"]


def _is_missing(value) -> bool:
    """必填字段缺失判定：None 或空白字符串视为缺失；Decimal('0.00') 合法（0 元发票）。"""
    return value is None or (isinstance(value, str) and not value.strip())


class ParseSkip(Exception):
    """解析器无法处理（缺依赖/未配置等），降级到下一环节。"""


# =====================================================================
# 公共工具：宽容字段抽取
# =====================================================================

_XML_FIELD_KEYS = {
    "invoice_no": ["发票号码", "invoiceno", "fphm", "invoice_number"],
    "invoice_code": ["发票代码", "invoicecode", "fpdm", "invoice_code"],
    "invoice_date": ["开票日期", "invoicedate", "kprq", "date"],
    "buyer_name": ["购买方名称", "buyername", "buyer_name", "购方名称"],
    "buyer_tax_no": ["购买方统一社会信用代码", "购买方纳税人识别号", "buyertaxid",
                     "buyertaxno", "购方税号", "taxpayerid", "buyerid"],
    "seller_name": ["销售方名称", "sellername", "seller_name", "销方名称"],
    "seller_tax_no": ["销售方统一社会信用代码", "销售方纳税人识别号", "sellertaxid",
                      "sellertaxno", "销方税号"],
    "amount": ["金额合计", "合计金额", "amountwithouttax", "heje", "totalamountwithouttax", "amount"],
    "tax_amount": ["税额合计", "合计税额", "taxamount", "se"],
    "total_amount": ["价税合计", "totalextaxamount", "jshj", "totaltaxinclamount", "totalamount"],
    "tax_rate": ["税率", "taxrate", "sl"],
    "item_summary": ["项目名称", "itemname", "xmmc", "goodsname"],
}

# 展平为 (field, key) 并按 key 长度降序：长关键字优先匹配，
# 避免 "totaltaxinclamount" 被短关键字 "amount" 抢先命中。
_FLAT_KEYS = sorted(
    [(f, k) for f, keys in _XML_FIELD_KEYS.items() for k in keys],
    key=lambda x: len(x[1]), reverse=True,
)


def _match_key(key: str, candidates: List[str]) -> bool:
    k = key.lower().replace("_", "").replace("-", "")
    for c in candidates:
        c = c.lower().replace("_", "").replace("-", "")
        if c and c in k:
            return True
    return False


def _first_value(value) -> str:
    """属性/文本值取第一个非空。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for v in value:
            s = str(v).strip()
            if s:
                return s
        return ""
    return str(value).strip()


def _extract_from_element(elem, dto: InvoiceDTO, seen: set) -> None:
    """遍历 XML 元素，按关键字映射填充 DTO（先到先得，长关键字优先）。"""
    tag = elem.tag.rsplit("}", 1)[-1]

    # 元素文本
    text = (elem.text or "").strip()
    if text:
        for field, key in _FLAT_KEYS:
            if field in seen:
                continue
            if _match_key(tag, [key]):
                setattr(dto, field, text)
                seen.add(field)
                break
    # 元素属性
    for attr_name, attr_val in elem.attrib.items():
        attr_local = attr_name.rsplit("}", 1)[-1]
        v = _first_value(attr_val)
        if not v:
            continue
        for field, key in _FLAT_KEYS:
            if field in seen:
                continue
            if _match_key(attr_local, [key]) or (tag and _match_key(tag, [key])):
                setattr(dto, field, v)
                seen.add(field)
                break


def _parse_xml_bytes(content: bytes) -> InvoiceDTO:
    """宽容解析 XML 字节流，返回未标准化 DTO（尽可能多填字段）。"""
    dto = InvoiceDTO()
    seen: set = set()
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        # 容错：XML 前可能有声明外的杂散字符，尝试截取 <?xml 起始
        idx = content.find(b"<?xml")
        if idx >= 0:
            root = ET.fromstring(content[idx:])
        else:
            raise
    for elem in root.iter():
        _extract_from_element(elem, dto, seen)
        if len(seen) >= len(_XML_FIELD_KEYS):
            break
    return dto


# =====================================================================
# 解析器
# =====================================================================

class BaseParser:
    name = "base"
    confidence = 0.5

    def parse(self, task: ParseTask) -> InvoiceDTO:
        raise NotImplementedError


class XmlInvoiceParser(BaseParser):
    """数电/电子发票 XML（宽容关键字匹配）。"""
    name = "xml"
    confidence = 1.0

    def parse(self, task: ParseTask) -> InvoiceDTO:
        return _parse_xml_bytes(task.content)


class OfdInvoiceParser(BaseParser):
    """OFD（GB/T 33190，实为 zip 包内嵌发票 XML）。

    两种取值路径：
    1. Attachs/original_invoice.xml 等内嵌发票 XML（部分厂商直接内嵌）；
    2. Tags/CustomTag.xml 自定义标签：字段元素内嵌 <ObjectRef PageRef="...">对象ID</ObjectRef>，
       按 Document.xml 的 Page ID->BaseLoc 找到页面 Content.xml，
       再从 TextObject[ID].TextCode 取真实文本（税局签发的数电票 OFD 均为该结构）。
    """
    name = "ofd"
    confidence = 0.95

    # CustomTag 标签 -> DTO 字段（仅映射确定的字段，避免 Amount 等行项目标签误伤合计）
    _TAG_FIELD = {
        "InvoiceNo": "invoice_no",
        "InvoiceCode": "invoice_code",
        "IssueDate": "invoice_date",
        "BuyerName": "buyer_name",
        "BuyerTaxID": "buyer_tax_no",
        "SellerName": "seller_name",
        "SellerTaxID": "seller_tax_no",
        "TaxExclusiveTotalAmount": "amount",
        "TaxTotalAmount": "tax_amount",
        "TaxInclusiveTotalAmount": "total_amount",
        "Item": "item_summary",
    }

    def parse(self, task: ParseTask) -> InvoiceDTO:
        best: Optional[InvoiceDTO] = None
        best_filled = -1
        with zipfile.ZipFile(io.BytesIO(task.content)) as zf:
            for info in zf.infolist():
                if not info.filename.lower().endswith(".xml"):
                    continue
                try:
                    dto = _parse_xml_bytes(zf.read(info))
                except Exception:
                    continue
                filled = sum(1 for f in REQUIRED_FIELDS if not _is_missing(getattr(dto, f)))
                if filled > best_filled:
                    best, best_filled = dto, filled
                if filled == len(REQUIRED_FIELDS):
                    break
            tag_dto = self._parse_custom_tags(zf)

        # 合并：内嵌 XML 缺失的字段用 CustomTag 结果补齐
        if best is None:
            best = tag_dto
        else:
            for field in self._TAG_FIELD.values():
                if _is_missing(getattr(best, field)) and not _is_missing(getattr(tag_dto, field)):
                    setattr(best, field, getattr(tag_dto, field))

        if all(_is_missing(getattr(best, f)) for f in REQUIRED_FIELDS):
            raise ParseSkip("OFD 内未找到可解析的发票 XML")
        return best

    def _parse_custom_tags(self, zf: zipfile.ZipFile) -> InvoiceDTO:
        dto = InvoiceDTO()
        doc_dirs = sorted({n.split("/")[0] for n in zf.namelist()
                           if "/" in n and n.split("/")[0].startswith("Doc_")})
        for doc_dir in doc_dirs:
            pages = self._page_map(zf, doc_dir)
            if not pages:
                continue
            text_cache: dict = {}
            for tag_file in zf.namelist():
                if not tag_file.startswith(doc_dir + "/Tags/") or not tag_file.lower().endswith(".xml"):
                    continue
                try:
                    root = ET.fromstring(zf.read(tag_file))
                except ET.ParseError:
                    continue
                self._fill_from_tag_tree(root, pages, zf, doc_dir, text_cache, dto)
        return dto

    @staticmethod
    def _page_map(zf: zipfile.ZipFile, doc_dir: str) -> dict:
        """Document.xml -> {页面 ID: BaseLoc}"""
        try:
            root = ET.fromstring(zf.read(f"{doc_dir}/Document.xml"))
        except (KeyError, ET.ParseError):
            return {}
        return {e.attrib["ID"]: e.attrib["BaseLoc"]
                for e in root.iter()
                if e.tag.rsplit("}", 1)[-1] == "Page"
                and "ID" in e.attrib and "BaseLoc" in e.attrib}

    @staticmethod
    def _text_objects(zf: zipfile.ZipFile, path: str) -> dict:
        """页面 Content.xml -> {对象 ID: 文本}"""
        try:
            root = ET.fromstring(zf.read(path))
        except (KeyError, ET.ParseError):
            return {}
        objs: dict = {}
        for elem in root.iter():
            if elem.tag.rsplit("}", 1)[-1] != "TextObject":
                continue
            oid = elem.attrib.get("ID")
            if not oid:
                continue
            text = "".join(s.text or "" for s in elem.iter()
                           if s.tag.rsplit("}", 1)[-1] == "TextCode")
            if text:
                objs[oid] = text
        return objs

    def _fill_from_tag_tree(self, root, pages: dict, zf: zipfile.ZipFile,
                            doc_dir: str, text_cache: dict, dto: InvoiceDTO) -> None:
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1]
            field = self._TAG_FIELD.get(tag)
            if not field or not _is_missing(getattr(dto, field)):
                continue
            obj_ref = next((s for s in elem.iter()
                            if s.tag.rsplit("}", 1)[-1] == "ObjectRef"), None)
            if obj_ref is not None and (obj_ref.text or "").strip():
                page_id = obj_ref.attrib.get("PageRef", "")
                base = pages.get(page_id)
                if not base:
                    continue
                path = f"{doc_dir}/{base}"
                if path not in text_cache:
                    text_cache[path] = self._text_objects(zf, path)
                value = text_cache[path].get(obj_ref.text.strip())
                if value:
                    setattr(dto, field, value)
            elif (elem.text or "").strip():
                setattr(dto, field, elem.text.strip())


class PdfTextParser(BaseParser):
    """PDF 文本层解析（pdfplumber，可替换）。"""
    name = "pdf_text"
    confidence = 0.85

    _RE_NO = re.compile(r"发票号码\s*[:：]?\s*(\d{8,20})")
    _RE_CODE = re.compile(r"发票代码\s*[:：]?\s*(\d{10,12})")
    _RE_DATE = re.compile(r"开票日期\s*[:：]?\s*(\d{4}[年\-/.]\d{1,2}[月\-/.]\d{1,2}日?)")
    _RE_TOTAL = re.compile(r"[（(]小写[）)]\s*[:：]?\s*[¥￥]?\s*([0-9,，.]+)")
    _RE_TOTAL2 = re.compile(r"价税合计\s*[:：]?\s*[¥￥]?\s*([0-9,，.]+)")
    _RE_SUM_ROW = re.compile(r"合\s*计\s*[¥￥]?\s*([0-9,，.]+)\s+[¥￥]?([0-9,，.]+)")

    def parse(self, task: ParseTask) -> InvoiceDTO:
        try:
            import pdfplumber
        except ImportError as e:  # 基础设施可替换：未安装则明确降级
            raise ParseSkip(f"pdfplumber 未安装: {e}") from e

        dto = InvoiceDTO()
        with pdfplumber.open(io.BytesIO(task.content)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages[:5])
        if not text.strip():
            raise ParseSkip("PDF 无文本层（可能为扫描件）")

        m = self._RE_NO.search(text)
        if m:
            dto.invoice_no = m.group(1)
        m = self._RE_CODE.search(text)
        if m:
            dto.invoice_code = m.group(1)
        m = self._RE_DATE.search(text)
        if m:
            dto.invoice_date = m.group(1)
        m = self._RE_TOTAL.search(text) or self._RE_TOTAL2.search(text)
        if m:
            dto.total_amount = m.group(1)
        m = self._RE_SUM_ROW.search(text)
        if m:
            dto.amount, dto.tax_amount = m.group(1), m.group(2)

        # 购买方/销售方块：名称 + 税号
        for side, name_attr, tax_attr in (
            ("购", "buyer_name", "buyer_tax_no"),
            ("售", "seller_name", "seller_tax_no"),
            ("销", "seller_name", "seller_tax_no"),
        ):
            block_m = re.search(rf"{side}[\s\S]{{0,400}}", text)
            if not block_m:
                continue
            block = block_m.group(0)
            nm = re.search(r"名称\s*[:：]\s*([^\s]+)", block)
            tm = re.search(r"(?:统一社会信用代码|纳税人识别号)\s*[:：]\s*([0-9A-Z]{15,20})", block)
            if nm and not getattr(dto, name_attr):
                setattr(dto, name_attr, nm.group(1))
            if tm and not getattr(dto, tax_attr):
                setattr(dto, tax_attr, tm.group(1))

        # 项目名称（取前几个）
        items = re.findall(r"\*[^*]+\*([^\n*]{2,30})", text)
        if items:
            dto.item_summary = "；".join(dict.fromkeys(x.strip() for x in items[:5]))

        if not dto.invoice_no and not dto.total_amount:
            raise ParseSkip("PDF 文本层未提取到关键字段（可能为扫描件）")
        return dto


class OcrParser(BaseParser):
    """OCR 解析：后端可插拔（基础设施层注册），默认未配置。"""
    name = "ocr"
    confidence = 0.6

    def __init__(self) -> None:
        self._backend: Optional[Callable[[bytes], InvoiceDTO]] = None

    def register_backend(self, backend: Callable[[bytes], InvoiceDTO]) -> None:
        """由基础设施层注入 OCR 后端（PaddleOCR / 云 OCR 等）。"""
        self._backend = backend

    def parse(self, task: ParseTask) -> InvoiceDTO:
        if self._backend is None:
            raise ParseSkip("OCR 未配置（可在 infra 中注册后端，或改用人工录入）")
        return self._backend(task.content)


# OCR 后端注册表（进程级单例，供 ParserEngine 与基础设施解耦）
ocr_parser_singleton = OcrParser()


class ParserEngine:
    """解析引擎：构建降级链并逐环节尝试（设计 §6.1）。"""

    def __init__(self, normalize: Optional[NormalizeEngine] = None) -> None:
        self._normalize = normalize or NormalizeEngine()

    def parse(self, task: ParseTask) -> ParseResult:
        chain = self._build_chain(task.file_type)
        if not chain:
            return ParseResult(success=False, error=f"不支持的文件类型: {task.file_type}")

        last_error = ""
        degrade_chain: List[dict] = []

        for parser in chain:
            try:
                dto = parser.parse(task)
                dto = self._normalize.normalize(dto)
                missing = [f for f in REQUIRED_FIELDS if _is_missing(getattr(dto, f))]
                if not missing:
                    degrade_chain.append({"engine": parser.name, "status": "ok"})
                    return ParseResult(
                        success=True, dto=dto, engine=parser.name,
                        confidence=parser.confidence, degrade_chain=degrade_chain,
                    )
                last_error = f"{parser.name}: 缺失字段: {missing}"
                logger.warning(last_error)
                degrade_chain.append({"engine": parser.name, "status": "degraded", "reason": last_error})
            except ParseSkip as e:
                last_error = f"{parser.name}: {e}"
                logger.warning(last_error)
                degrade_chain.append({"engine": parser.name, "status": "degraded", "reason": str(e)})
            except Exception as e:  # 任何异常均降级，不阻塞其他文件
                last_error = f"{parser.name}: 异常: {e}"
                logger.warning(last_error, exc_info=True)
                degrade_chain.append({"engine": parser.name, "status": "error", "reason": str(e)})

        # 全部失败 -> ABNORMAL（由服务层处理），携带降级链
        return ParseResult(success=False, error=last_error or "全部解析环节失败",
                           degrade_chain=degrade_chain)

    def _build_chain(self, file_type: str) -> List[BaseParser]:
        ft = (file_type or "").lower()
        if ft == "xml":
            return [XmlInvoiceParser()]
        if ft == "ofd":
            return [OfdInvoiceParser(), ocr_parser_singleton]
        if ft == "pdf":
            return [PdfTextParser(), ocr_parser_singleton]
        if ft in ("jpg", "jpeg", "png"):
            return [ocr_parser_singleton]
        return []
