"""核心引擎单元测试：状态机 / 标准化 / 校验 / 去重 / 匹配 / 规则。

运行: python -m pytest tests/ -v
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.domain.dedup import DedupEngine, Fingerprint
from app.domain.dto import InvoiceDTO, ReimburseDTO, RuleDTO
from app.domain.matcher import MatchEngine
from app.domain.normalize import NormalizeEngine, parse_date, to_decimal
from app.domain.rules_engine import RuleEngine
from app.domain.states import (InvoiceStatus, InvalidTransition,
                               assert_invoice_transition, assert_match_transition)
from app.domain.validator import InvoiceValidator


# ---------------------------------------------------------------- 状态机
class TestStateMachine:
    def test_happy_path(self):
        assert_invoice_transition("PARSING", "PARSED")
        assert_invoice_transition("PARSED", "NORMAL")
        assert_invoice_transition("NORMAL", "MATCHED")
        assert_invoice_transition("NORMAL", "DUPLICATE")
        assert_invoice_transition("ABNORMAL", "NORMAL")
        assert_invoice_transition("DUPLICATE", "NORMAL")

    def test_forbidden_jumps(self):
        with pytest.raises(InvalidTransition):
            assert_invoice_transition("PARSING", "NORMAL")       # 跳过 PARSED
        with pytest.raises(InvalidTransition):
            assert_invoice_transition("ABNORMAL", "MATCHED")     # 跳转
        with pytest.raises(InvalidTransition):
            assert_invoice_transition("DUPLICATE", "MATCHED")
        with pytest.raises(InvalidTransition):
            assert_match_transition("PENDING", "CONFIRMED")      # 必须经 SUSPICIOUS/CONFLICT

    def test_match_machine(self):
        assert_match_transition("PENDING", "MATCHED")
        assert_match_transition("PENDING", "SUSPICIOUS")
        assert_match_transition("PENDING", "CONFLICT")
        assert_match_transition("PENDING", "UNMATCHED")
        assert_match_transition("SUSPICIOUS", "CONFIRMED")
        assert_match_transition("SUSPICIOUS", "REJECTED")
        assert_match_transition("CONFLICT", "CONFIRMED")


# ---------------------------------------------------------------- 标准化
class TestNormalize:
    def test_to_decimal(self):
        assert to_decimal("1,234.50") == Decimal("1234.50")
        assert to_decimal("￥99.9") == Decimal("99.90")
        assert to_decimal("") is None
        assert to_decimal(None) is None
        assert to_decimal("abc") is None
        assert to_decimal("-56.789") == Decimal("-56.79")

    def test_parse_date(self):
        assert parse_date("2026-01-05") == "2026-01-05"
        assert parse_date("2026/1/5") == "2026-01-05"
        assert parse_date("2026年1月5日") == "2026-01-05"
        assert parse_date("20260105") == "2026-01-05"
        assert parse_date("2026.1.5") == "2026-01-05"
        assert parse_date("垃圾") is None
        assert parse_date("") is None

    def test_clean_tax_no(self):
        ne = NormalizeEngine()
        assert ne._clean_tax_no(" 91 1100-MA5x ") == "911100MA5X"

    def test_roundtrip(self):
        ne = NormalizeEngine()
        dto = ne.normalize(InvoiceDTO(invoice_no=" 24120 ", invoice_date="2026/3/1",
                                      total_amount="￥1,000.00"))
        assert dto.invoice_no == "24120"
        assert dto.invoice_date == "2026-03-01"
        assert dto.total_amount == Decimal("1000.00")


# ---------------------------------------------------------------- 校验
class TestValidator:
    def _ok(self, **kw):
        base = dict(invoice_no="24120001", invoice_date="2026-06-01",
                    amount=Decimal("88.00"), tax_amount=Decimal("12.00"),
                    total_amount=Decimal("100.00"))
        base.update(kw)
        return InvoiceDTO(**base)

    def test_valid(self):
        assert InvoiceValidator().validate(self._ok()) == []

    def test_c1_missing(self):
        issues = InvoiceValidator().validate(self._ok(invoice_no=""))
        assert any(i.code == "MISSING_FIELD" for i in issues)

    def test_c2_amount_mismatch(self):
        issues = InvoiceValidator().validate(self._ok(total_amount=Decimal("101.00")))
        assert any(i.code == "AMOUNT_MISMATCH" for i in issues)

    def test_c2_tolerance(self):
        # ±0.01 容差内不报错
        assert not any(i.code == "AMOUNT_MISMATCH"
                       for i in InvoiceValidator().validate(self._ok(total_amount=Decimal("100.01"))))

    def test_c3_tax_no_format(self):
        issues = InvoiceValidator().validate(self._ok(buyer_tax_no="123"))
        assert any(i.code == "TAX_NO_FORMAT" and i.severity == "WARN" for i in issues)

    def test_c4_future_date(self):
        issues = InvoiceValidator().validate(self._ok(invoice_date="2099-01-01"))
        assert any(i.code == "FUTURE_DATE" and i.severity == "ERROR" for i in issues)

    def test_c4_too_old(self):
        issues = InvoiceValidator().validate(self._ok(invoice_date="2000-01-01"))
        assert any(i.code == "TOO_OLD" and i.severity == "WARN" for i in issues)

    def test_c5_red_flush(self):
        dto = self._ok(amount=Decimal("-100.00"), tax_amount=Decimal("0"),
                       total_amount=Decimal("-100.00"))
        issues = InvoiceValidator().validate(dto)
        assert dto.is_red_flush is True
        issues2 = InvoiceValidator().validate(self._ok(amount=Decimal("100.00"),
                                                       total_amount=Decimal("-100.00")))
        assert any(i.code == "SIGN_MISMATCH" for i in issues2)


# ---------------------------------------------------------------- 去重
class TestDedup:
    def test_file_hash(self):
        hit = DedupEngine().check(
            Fingerprint("hash1", "", "NO1"),
            [(5, "hash1", "CODE", "NO1")])
        assert hit and hit.kind == "file_hash" and hit.existing_id == 5

    def test_code_no(self):
        hit = DedupEngine().check(
            Fingerprint("hash2", "CODE", "NO1"),
            [(5, "hash1", "CODE", "NO1")])
        assert hit and hit.kind == "code_no"

    def test_no_hit(self):
        assert DedupEngine().check(
            Fingerprint("hash2", "CODE", "NO2"),
            [(5, "hash1", "CODE", "NO1")]) is None

    def test_codeless_invoice_matched_by_no(self):
        # 数电发票无代码：仅按发票号判重
        hit = DedupEngine().check(
            Fingerprint("hash2", "", "NO1"),
            [(5, "hash1", "", "NO1")])
        assert hit and hit.kind == "code_no"

    def test_code_mismatch_no_hit(self):
        # 一方有代码一方无代码：不判重
        assert DedupEngine().check(
            Fingerprint("hash2", "", "NO1"),
            [(5, "hash1", "CODE", "NO1")]) is None


# ---------------------------------------------------------------- 匹配
class TestMatcher:
    def _inv(self, **kw):
        base = dict(invoice_no="NO100", invoice_date="2026-06-01",
                    total_amount=Decimal("100.00"))
        base.update(kw)
        return InvoiceDTO(**base)

    def _reb(self, **kw):
        base = dict(reimburse_no="R1", amount=Decimal("100.00"), invoice_no="NO100",
                    period_start="2026-06-01", period_end="2026-06-30")
        base.update(kw)
        return ReimburseDTO(**base)

    def test_exact_auto_match(self):
        ps = MatchEngine().score_pair(self._inv(), self._reb())
        assert ps.match_type == "exact"
        assert ps.score >= 0.95
        assert MatchEngine().decide_status(ps) == "MATCHED"

    def test_conflict_on_no_diff(self):
        ps = MatchEngine().score_pair(
            self._inv(invoice_no="NO999"), self._reb(invoice_no="NO100"))
        assert MatchEngine().decide_status(ps) == "CONFLICT"

    def test_conflict_on_amount(self):
        ps = MatchEngine().score_pair(
            self._inv(), self._reb(amount=Decimal("150.00"), invoice_no="NO100"))
        assert "amount" in ps.conflict_fields
        assert MatchEngine().decide_status(ps) == "CONFLICT"

    def test_suspicious_fuzzy(self):
        ps = MatchEngine().score_pair(
            self._inv(invoice_no="NO1001"), self._reb(invoice_no="NO100"))
        assert MatchEngine().decide_status(ps) == "SUSPICIOUS"

    def test_no_invoice_no_amount_only(self):
        ps = MatchEngine().score_pair(self._inv(), self._reb(invoice_no=""))
        assert MatchEngine().decide_status(ps) == "SUSPICIOUS"

    def test_out_of_period_penalty(self):
        ps_in = MatchEngine().score_pair(self._inv(), self._reb())
        ps_out = MatchEngine().score_pair(
            self._inv(invoice_date="2026-01-01"), self._reb())
        assert ps_out.score < ps_in.score

    def test_many_to_one(self):
        eng = MatchEngine()
        combo = eng.find_many_to_one(
            [self._inv(invoice_no="A", total_amount=Decimal("60.00")),
             self._inv(invoice_no="B", total_amount=Decimal("40.00"))],
            self._reb())
        assert combo is not None and len(combo) == 2

    def test_many_to_one_single_not_allowed(self):
        eng = MatchEngine()
        combo = eng.find_many_to_one(
            [self._inv(invoice_no="A", total_amount=Decimal("100.00"))], self._reb())
        assert combo is None

    def test_score_range(self):
        # I10: score ∈ [0,1]
        ps = MatchEngine().score_pair(self._inv(), self._reb())
        assert 0.0 <= ps.score <= 1.0


# ---------------------------------------------------------------- 规则
class TestRules:
    def test_buyer_tax_no(self):
        rule = RuleDTO(name="白名单", rule_type="buyer_tax_no",
                       config={"tax_nos": ["91110000AAAA"]})
        issues = RuleEngine().evaluate(InvoiceDTO(buyer_tax_no="OTHER"), [rule])
        assert any(i.code == "BUYER_TAX_NO_NOT_ALLOWED" for i in issues)
        assert RuleEngine().evaluate(InvoiceDTO(buyer_tax_no="91110000AAAA"), [rule]) == []

    def test_period(self):
        rule = RuleDTO(name="期间", rule_type="period",
                       config={"start": "2026-01-01", "end": "2026-12-31"})
        issues = RuleEngine().evaluate(InvoiceDTO(invoice_date="2025-06-01"), [rule])
        assert any(i.code == "PERIOD_BEFORE" for i in issues)

    def test_amount_bounds(self):
        rule = RuleDTO(name="上限", rule_type="amount", config={"max": 1000})
        issues = RuleEngine().evaluate(InvoiceDTO(total_amount=Decimal("2000")), [rule])
        assert any(i.code == "AMOUNT_ABOVE_MAX" for i in issues)

    def test_priority_and_stop(self):
        r1 = RuleDTO(name="r1", rule_type="buyer_tax_no", priority=1,
                     stop_on_fail=True, config={"tax_nos": ["X"]})
        r2 = RuleDTO(name="r2", rule_type="amount", priority=2, config={"max": 10})
        issues = RuleEngine().evaluate(
            InvoiceDTO(buyer_tax_no="Y", total_amount=Decimal("999")), [r1, r2])
        # r1 失败且短路 -> r2 不执行
        assert len(issues) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
