"""
backend/agent/condition_evaluator.py

Evaluates conditional expressions from rule JSON against a ParsedTransaction.

Single condition format:
  {"field": "CLM.420-DK", "operator": "in", "value": ["42", "43"]}

Compound condition format:
  {"logic": "OR", "conditions": [{"field": "CLM.420-DK", ...}, ...]}

Field reference format:
  "SEGMENT.field_id"       — occurrence 1 (default)
  "SEGMENT[2].field_id"    — occurrence 2 (repeating segments like dual COB)

Supported operators:
  empty, not_empty, eq, neq, in, not_in, starts_with, ends_with, contains,
  gt, lt, gte, lte, matches (regex)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Parses "SEGMENT[N].field_id" — brackets+occurrence are optional
_FIELD_REF_RE = re.compile(r'^([A-Z0-9_-]+)(?:\[(\d+)\])?\.(.+)$')


class ConditionEvaluator:
    """
    Stateless evaluator. Thread-safe. One instance shared across all requests.
    Never raises — any error returns (False, error_description).
    """

    def evaluate(self, condition: dict, tx) -> tuple[bool, str]:
        """
        Evaluate a condition block against a ParsedTransaction.
        Returns (result: bool, expression_str: str).

        Handles three input forms:
          1. Compound: {"logic": "AND", "conditions": [{...}, ...]}
          2. Single:   {"field": "SEG.id", "operator": "eq", "value": "X"}
          3. Wrapped:  {"if": {...}} — legacy format from existing rule files
        """
        try:
            if not condition:
                return True, 'NO_CONDITION'

            # New compound format
            if 'conditions' in condition:
                return self._evaluate_compound(condition, tx)

            # Direct single condition
            if 'field' in condition:
                return self._evaluate_single(condition, tx)

            # Legacy wrapped format: {"if": {...}} or {"if": [...]}
            if 'if' in condition:
                if_block = condition['if']
                if isinstance(if_block, list):
                    # Old list-of-conditions format
                    return self._evaluate_list(if_block, condition.get('logic', 'AND'), tx)
                return self.evaluate(if_block, tx)

            return True, 'UNRECOGNIZED_CONDITION_FORMAT'

        except Exception as e:
            logger.error(f'ConditionEvaluator.evaluate() error: {e}', exc_info=True)
            return False, f'EVAL_ERROR: {e}'

    def _evaluate_compound(self, condition: dict, tx) -> tuple[bool, str]:
        """Evaluate {"logic": "AND"|"OR", "conditions": [{...}, ...]}"""
        logic = condition.get('logic', 'AND').upper()
        sub_conditions = condition.get('conditions', [])
        if not sub_conditions:
            return True, 'NO_SUB_CONDITIONS'
        return self._evaluate_list(sub_conditions, logic, tx)

    def _evaluate_list(
        self, conditions: list, logic: str, tx
    ) -> tuple[bool, str]:
        results = []
        exprs = []
        for sub in conditions:
            r, e = self._evaluate_single(sub, tx)
            results.append(r)
            exprs.append(e)
        final = any(results) if logic.upper() == 'OR' else all(results)
        combined = f' {logic.upper()} '.join(exprs)
        return final, f'({combined})'

    def _evaluate_single(self, condition: dict, tx) -> tuple[bool, str]:
        """Evaluate {"field": "SEG.id", "operator": "...", "value": ...}"""
        field_ref = condition.get('field', '')
        operator  = condition.get('operator', 'eq')
        expected  = condition.get('value')

        actual, resolve_expr = self._resolve_field(field_ref, tx)
        result = self._apply(actual, operator, expected)

        # Human-readable expression for audit trail
        expected_repr = repr(expected) if expected is not None else 'None'
        expr = f'{resolve_expr} {operator.upper()} {expected_repr}'
        return result, expr

    def _resolve_field(self, field_ref: str, tx) -> tuple[Optional[str], str]:
        """
        Resolve "SEGMENT[N].field_id" to its value in the transaction.
        Returns (value, description_string).
        """
        if not field_ref or '.' not in field_ref:
            return None, f'{field_ref}=<invalid_ref>'

        m = _FIELD_REF_RE.match(field_ref)
        if not m:
            # Fallback: split on last '.' — handles simple "SEG.fid" without regex
            dot = field_ref.rfind('.')
            segment_id, field_id = field_ref[:dot], field_ref[dot + 1:]
            occurrence = 1
        else:
            segment_id = m.group(1)
            occurrence = int(m.group(2)) if m.group(2) else 1
            field_id = m.group(3)

        value = tx.get_field(segment_id, field_id, occurrence=occurrence)
        if value is None:
            return None, f'{field_ref}=<absent>'
        return value, f'{field_ref}={repr(value)}'

    def _apply(self, actual: Optional[str], operator: str, expected: Any) -> bool:
        """Apply comparison operator."""
        op = operator.lower().strip()

        # Operators that work even when field is absent
        if op == 'empty':
            return actual is None or actual.strip() == ''
        if op == 'not_empty':
            return actual is not None and actual.strip() != ''

        # Everything else requires a present value
        if actual is None:
            return False
        a = actual.strip()

        if op == 'eq':
            return a == str(expected).strip()
        if op == 'neq':
            return a != str(expected).strip()
        if op == 'in':
            return a in [str(v).strip() for v in (expected or [])]
        if op == 'not_in':
            return a not in [str(v).strip() for v in (expected or [])]
        if op == 'starts_with':
            return a.startswith(str(expected))
        if op == 'ends_with':
            return a.endswith(str(expected))
        if op == 'contains':
            return str(expected) in a
        if op == 'matches':
            try:
                return bool(re.match(str(expected), a))
            except re.error:
                logger.warning(f'Invalid regex in condition: {expected!r}')
                return False
        if op in ('gt', 'lt', 'gte', 'lte'):
            try:
                an, en = float(a), float(expected)
                if op == 'gt':  return an > en
                if op == 'lt':  return an < en
                if op == 'gte': return an >= en
                if op == 'lte': return an <= en
            except (ValueError, TypeError):
                logger.warning(
                    f'Numeric comparison on non-numeric values: '
                    f'actual={a!r}, expected={expected!r}'
                )
                return False

        logger.warning(f'Unknown operator: {operator!r}. Treating as False.')
        return False
