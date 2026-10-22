"""OCR 两遍策略的字段位数校验（不依赖 OCR 引擎）。"""
from app.domain.dto import InvoiceDTO
from app.infra.ocr import _digit_quality, _digits_suspicious


def _dto(invoice_no="", buyer="", seller=""):
    return InvoiceDTO(invoice_no=invoice_no, buyer_tax_no=buyer, seller_tax_no=seller)


def test_valid_lengths_not_suspicious():
    # 全电 20 位发票号码 + 18 位信用代码
    dto = _dto("26112000003355887706", "911101027355739693", "91310115MA1GHIJK89")
    assert not _digits_suspicious(dto)
    assert _digit_quality(dto) == 3


def test_truncated_invoice_no_suspicious():
    # rec 把连续 0 压缩丢字：20 位读成 18 位
    dto = _dto("261120000335887706", "911101027355739693")
    assert _digits_suspicious(dto)
    assert _digit_quality(dto) == 1   # 仅购方税号合法


def test_traditional_8_digit_invoice_no_valid():
    assert not _digits_suspicious(_dto("24412000"))
    assert _digit_quality(_dto("24412000")) == 1


def test_bad_tax_no_suspicious():
    dto = _dto("24412000", "91110102735573")   # 税号 14 位，位数不足
    assert _digits_suspicious(dto)


def test_empty_fields_ignored():
    # 空字段不算可疑（个人抬头无税号等情况）
    assert not _digits_suspicious(_dto())
    assert _digit_quality(_dto()) == 0


def test_15_digit_old_tax_no_valid():
    dto = _dto("24412000", "110102735573969")
    assert not _digits_suspicious(dto)
