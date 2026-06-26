"""
Condition evaluator for rules-driven conditional logic.

Supports condition blocks in rule JSON:
  {"condition": {"if": {"field": "CLM.420-DK", "operator": "eq", "value": "42"}}}
  {"condition": {"if": [...], "logic": "OR"}}

Operators: empty, not_empty, eq, neq, in, not_in, starts_with, gt, lt
Field refs: "SEGMENT_ID.field_id" e.g. "CLM.420-DK"
"""
from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .segment_parser import ParsedTransaction


class ConditionEvaluator:

    def evaluate(self, condition: dict, transaction: "ParsedTransaction") -> tuple[bool, str]:
        """
        Evaluate a condition block.
        Returns (passed: bool, expression_str: str).
        """
        if not condition:
            return True, ""

        if_block = condition.get("if", condition)
        logic = condition.get("logic", "AND") if isinstance(condition, dict) else "AND"

        if isinstance(if_block, list):
            results = [self._eval_single(c, transaction) for c in if_block]
            passed = any(results) if logic == "OR" else all(results)
            expr = f" {logic} ".join(self._expr_str(c) for c in if_block)
            return passed, expr

        result = self._eval_single(if_block, transaction)
        return result, self._expr_str(if_block)

    def _eval_single(self, cond: dict, transaction: "ParsedTransaction") -> bool:
        actual = self._resolve(cond.get("field", ""), transaction)
        return self._apply(actual, cond.get("operator", "eq"), cond.get("value"))

    def _resolve(self, field_ref: str, transaction: "ParsedTransaction") -> Optional[str]:
        if "." not in field_ref:
            return None
        seg_id, field_id = field_ref.split(".", 1)
        return transaction.get_field(seg_id, field_id)

    def _apply(self, actual: Optional[str], operator: str, expected: Any) -> bool:
        if operator == "empty":
            return actual is None or actual.strip() == ""
        if operator == "not_empty":
            return actual is not None and actual.strip() != ""
        if actual is None:
            return False
        a = actual.strip()
        if operator == "eq":
            return a == str(expected).strip()
        if operator == "neq":
            return a != str(expected).strip()
        if operator == "in":
            return a in [str(v).strip() for v in (expected or [])]
        if operator == "not_in":
            return a not in [str(v).strip() for v in (expected or [])]
        if operator == "starts_with":
            return a.startswith(str(expected))
        if operator == "gt":
            try:
                return float(a) > float(expected)
            except (ValueError, TypeError):
                return False
        if operator == "lt":
            try:
                return float(a) < float(expected)
            except (ValueError, TypeError):
                return False
        return False

    def _expr_str(self, cond: dict) -> str:
        field = cond.get("field", "?")
        op = cond.get("operator", "eq").upper()
        val = cond.get("value", "")
        if isinstance(val, list):
            val = f"[{', '.join(str(v) for v in val)}]"
        return f"{field} {op} {val}"
