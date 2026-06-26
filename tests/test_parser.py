"""
Tests for all three production fixes.

Fix 1 — Hex parsing: auto-detect \x1c/\x1d/\x1e streams
Fix 2 — Repeating segments: list-based ParsedTransaction
Fix 3 — Conditional rules: ConditionEvaluator
"""
import pytest

from agent.segment_parser import detect_format, parse, parse_d0, ParsedTransaction
from agent.condition_evaluator import ConditionEvaluator


# ── Fix 1: Hex parsing ────────────────────────────────────────────────────────

PIPE_SAMPLE = (
    "HDR|101-A1=610279|102-A2=D0|103-A3=B1\n"
    "INS|302-C2=ZH48291045|301-C1=RXGRP88\n"
    "PAT|304-C4=19580712|305-C5=2"
)

# Pipe-equivalent as NCPDP hex bytes (FS=\x1c, GS=\x1d, RS=\x1e)
HEX_SAMPLE = (
    b"HDR\x1c101-A1\x1d610279\x1c102-A2\x1dD0\x1c103-A3\x1dB1\x1e"
    b"INS\x1c302-C2\x1dZH48291045\x1c301-C1\x1dRXGRP88\x1e"
    b"PAT\x1c304-C4\x1d19580712\x1c305-C5\x1d2\x1e"
)


class TestDetectFormat:
    def test_pipe_str(self):
        assert detect_format(PIPE_SAMPLE) == "pipe"

    def test_hex_bytes(self):
        assert detect_format(HEX_SAMPLE) == "hex"

    def test_hex_str(self):
        assert detect_format(HEX_SAMPLE.decode("latin-1")) == "hex"

    def test_empty_is_pipe(self):
        assert detect_format("") == "pipe"


class TestParsePipe:
    def setup_method(self):
        self.parsed = parse(PIPE_SAMPLE)

    def test_format_is_pipe(self):
        assert self.parsed.fmt == "pipe"

    def test_segment_count(self):
        assert len(self.parsed.segments) == 3

    def test_segment_ids(self):
        ids = [s.segment_id for s in self.parsed.segments]
        assert ids == ["HDR", "INS", "PAT"]

    def test_first_occurrence(self):
        for seg in self.parsed.segments:
            assert seg.occurrence == 1

    def test_field_values(self):
        assert self.parsed.get_field("HDR", "101-A1") == "610279"
        assert self.parsed.get_field("INS", "302-C2") == "ZH48291045"

    def test_total_fields(self):
        assert self.parsed.total_fields == 7  # HDR:3 + INS:2 + PAT:2

    def test_segment_order_property(self):
        assert self.parsed.segment_order == ["HDR", "INS", "PAT"]


class TestParseHex:
    def setup_method(self):
        self.parsed = parse(HEX_SAMPLE)

    def test_format_is_hex(self):
        assert self.parsed.fmt == "hex"

    def test_segment_count(self):
        assert len(self.parsed.segments) == 3

    def test_segment_ids(self):
        ids = [s.segment_id for s in self.parsed.segments]
        assert ids == ["HDR", "INS", "PAT"]

    def test_field_values_match_pipe(self):
        pipe_parsed = parse(PIPE_SAMPLE)
        assert (self.parsed.get_field("HDR", "101-A1") ==
                pipe_parsed.get_field("HDR", "101-A1") == "610279")
        assert (self.parsed.get_field("INS", "302-C2") ==
                pipe_parsed.get_field("INS", "302-C2") == "ZH48291045")

    def test_parse_d0_alias_handles_hex_str(self):
        tx = parse_d0(HEX_SAMPLE.decode("latin-1"))
        assert tx.fmt == "hex"
        assert tx.get_field("HDR", "103-A3") == "B1"


class TestParseHexLatin1:
    def test_high_byte_survives(self):
        high_byte_val = b"HDR\x1c101-A1\x1d\xe9l\xe8ve\x1e"
        tx = parse(high_byte_val)
        assert tx.get_field("HDR", "101-A1") == "\xe9l\xe8ve"


# ── Fix 2: Repeating segments ─────────────────────────────────────────────────

REPEATING_PIPE = (
    "HDR|101-A1=610279|103-A3=B1\n"
    "INS|302-C2=FIRST_INS\n"
    "INS|302-C2=SECOND_INS\n"
    "CLM|455-EM=1"
)


class TestRepeatingSegments:
    def setup_method(self):
        self.parsed = parse(REPEATING_PIPE)

    def test_total_segments_includes_repeats(self):
        assert len(self.parsed.segments) == 4

    def test_segment_types(self):
        assert self.parsed.segment_order == ["HDR", "INS", "CLM"]

    def test_occurrences_assigned(self):
        ins_segs = self.parsed.get_segments("INS")
        assert len(ins_segs) == 2
        assert ins_segs[0].occurrence == 1
        assert ins_segs[1].occurrence == 2

    def test_get_first_returns_first(self):
        first = self.parsed.get_first("INS")
        assert first.occurrence == 1
        assert first.fields[0].value == "FIRST_INS"

    def test_get_field_reads_first_occurrence(self):
        assert self.parsed.get_field("INS", "302-C2") == "FIRST_INS"

    def test_get_segments_returns_all(self):
        all_ins = self.parsed.get_segments("INS")
        values = [s.fields[0].value for s in all_ins]
        assert values == ["FIRST_INS", "SECOND_INS"]

    def test_missing_segment_returns_none(self):
        assert self.parsed.get_first("DUR") is None
        assert self.parsed.get_segments("DUR") == []
        assert self.parsed.get_field("DUR", "999-XX") is None


# ── Fix 3: Condition evaluator ────────────────────────────────────────────────

class TestConditionEvaluator:
    def setup_method(self):
        self.ev = ConditionEvaluator()
        self.tx = parse("CLM|420-DK=42|406-D6=0\nHDR|103-A3=B1")

    def _eval(self, condition):
        result, expr = self.ev.evaluate(condition, self.tx)
        return result

    def test_eq_true(self):
        assert self._eval({"if": {"field": "CLM.420-DK", "operator": "eq", "value": "42"}})

    def test_eq_false(self):
        assert not self._eval({"if": {"field": "CLM.420-DK", "operator": "eq", "value": "99"}})

    def test_neq(self):
        assert self._eval({"if": {"field": "CLM.420-DK", "operator": "neq", "value": "99"}})

    def test_in(self):
        assert self._eval({"if": {"field": "CLM.420-DK", "operator": "in", "value": ["41", "42", "43"]}})

    def test_not_in(self):
        assert self._eval({"if": {"field": "CLM.406-D6", "operator": "not_in", "value": ["1", "2"]}})

    def test_empty_on_missing_field(self):
        assert self._eval({"if": {"field": "DUR.999-XX", "operator": "empty"}})

    def test_not_empty_on_present_field(self):
        assert self._eval({"if": {"field": "CLM.420-DK", "operator": "not_empty"}})

    def test_starts_with(self):
        assert self._eval({"if": {"field": "HDR.103-A3", "operator": "starts_with", "value": "B"}})

    def test_and_logic_all_true(self):
        assert self._eval({
            "if": [
                {"field": "CLM.420-DK", "operator": "eq", "value": "42"},
                {"field": "HDR.103-A3", "operator": "eq", "value": "B1"},
            ],
            "logic": "AND",
        })

    def test_and_logic_one_false(self):
        assert not self._eval({
            "if": [
                {"field": "CLM.420-DK", "operator": "eq", "value": "42"},
                {"field": "HDR.103-A3", "operator": "eq", "value": "B2"},
            ],
            "logic": "AND",
        })

    def test_or_logic_one_true(self):
        assert self._eval({
            "if": [
                {"field": "CLM.420-DK", "operator": "eq", "value": "99"},
                {"field": "HDR.103-A3", "operator": "eq", "value": "B1"},
            ],
            "logic": "OR",
        })

    def test_expression_string(self):
        _, expr = self.ev.evaluate(
            {"if": {"field": "CLM.420-DK", "operator": "in", "value": ["42", "43"]}},
            self.tx,
        )
        assert "CLM.420-DK" in expr
        assert "IN" in expr

    def test_no_condition_returns_true(self):
        assert self._eval({})
