"""
ingestion/validator/rule_validator.py

Validates compiled rules against the schema before writing them to disk.

Three severity levels:
  VALID   — rule is correct and ready to use
  WARN    — rule has a non-critical issue (e.g. missing field_name) — still usable
  INVALID — rule has a critical issue — must be fixed before the engine can use it

Invalid rules are written to ingestion_output/flagged_for_review.json.
Valid and WARN rules go to ingestion_output/NN_segment.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


VALID_ACTIONS    = {'carry', 'transform', 'add', 'remove', 'modify', 'cases'}
VALID_OPERATORS  = {
    'eq', 'neq', 'in', 'not_in', 'empty', 'not_empty',
    'gt', 'lt', 'gte', 'lte', 'starts_with', 'ends_with',
    'contains', 'matches',
}
VALID_TRANSFORMS = {
    'ZERO_PAD_LEFT', 'SET_VALUE', 'REMOVE_HYPHENS',
    'UPPERCASE', 'MAP_CODE', 'DATE_REFORMAT',
}

FIELD_ID_PATTERN  = re.compile(r'^\d{3}-[A-Z0-9]{1,4}$')
FIELD_REF_PATTERN = re.compile(r'^[A-Z]{2,4}(?:\[\d+\])?\.[\d]{3}-[A-Z0-9]{1,4}$')


@dataclass
class ValidationResult:
    rule: dict
    status: str              # 'VALID' | 'WARN' | 'INVALID'
    issues: list[str]
    segment_id: str


class RuleValidator:

    def validate_all(self, rules: list[dict], segment_id: str) -> list[ValidationResult]:
        return [self.validate(rule, segment_id) for rule in rules]

    def validate(self, rule: dict, segment_id: str) -> ValidationResult:
        issues: list[str] = []

        field_id = rule.get('field_id', '')
        action   = rule.get('action', '')

        # ── Required fields ───────────────────────────────────────────────────
        if not field_id:
            issues.append('INVALID: field_id is missing')
        elif not FIELD_ID_PATTERN.match(field_id):
            issues.append(f'INVALID: field_id "{field_id}" does not match NNN-XX pattern')

        if not action:
            issues.append('INVALID: action is missing')
        elif action not in VALID_ACTIONS:
            issues.append(f'INVALID: unknown action "{action}"')

        # ── Warn-only issues ──────────────────────────────────────────────────
        if not rule.get('field_name'):
            issues.append('WARN: field_name is missing (field_id will be used as fallback)')

        if not rule.get('notes'):
            issues.append('WARN: notes is missing (recommended for audit trail)')

        # ── Action-specific validation ─────────────────────────────────────────
        if action == 'transform':
            transform = rule.get('transform', '')
            if not transform:
                issues.append('INVALID: transform action requires "transform" field')
            elif transform not in VALID_TRANSFORMS:
                issues.append(f'INVALID: unknown transform "{transform}"')
            if transform == 'ZERO_PAD_LEFT' and 'params' not in rule:
                issues.append('INVALID: ZERO_PAD_LEFT requires "params": {"length": N}')

        if action == 'cases':
            cases = rule.get('cases', [])
            if not cases:
                issues.append('INVALID: cases action requires non-empty "cases" list')
            for i, case in enumerate(cases):
                when = case.get('when')
                then = case.get('then', {})
                if when != 'default' and isinstance(when, dict):
                    issues.extend(self._validate_condition(when, f'cases[{i}].when'))
                if not then.get('action'):
                    issues.append(f'INVALID: cases[{i}].then is missing "action"')

        # ── Condition block validation ─────────────────────────────────────────
        if 'condition' in rule:
            cond = rule['condition'].get('if', {})
            if isinstance(cond, dict) and 'conditions' in cond:
                for i, sub in enumerate(cond.get('conditions', [])):
                    issues.extend(self._validate_condition(sub, f'condition.if.conditions[{i}]'))
            elif isinstance(cond, dict):
                issues.extend(self._validate_condition(cond, 'condition.if'))

        if 'warn_condition' in rule:
            cond = rule['warn_condition'].get('if', {})
            if isinstance(cond, dict):
                issues.extend(self._validate_condition(cond, 'warn_condition.if'))

        # ── Derive status ─────────────────────────────────────────────────────
        has_invalid = any(i.startswith('INVALID') for i in issues)
        has_warn    = any(i.startswith('WARN')    for i in issues)

        if has_invalid:
            status = 'INVALID'
        elif has_warn:
            status = 'WARN'
        else:
            status = 'VALID'

        return ValidationResult(rule=rule, status=status, issues=issues, segment_id=segment_id)

    def validate_segment_ownership(
        self,
        rules: list[dict],
        declared_segment: str,
    ) -> tuple[list[dict], list[dict]]:
        """
        Split rules into (clean, suspect) based on segment ownership.

        clean   — rules whose field_id is confirmed to belong to declared_segment,
                  OR whose field_id is unknown (not in any ownership map)
        suspect — rules whose field_id is known to belong to a DIFFERENT segment

        Returns (clean_rules, suspect_rules).
        Suspect rules are written to flagged_for_review.json with a clear reason.
        """
        from ingestion.extractor.prompts import SEGMENT_OWNED_FIELDS

        # Build reverse lookup: field_id → home segment
        all_owned: dict[str, str] = {}
        for seg, fields in SEGMENT_OWNED_FIELDS.items():
            for fid in fields:
                all_owned[fid] = seg

        clean:   list[dict] = []
        suspect: list[dict] = []

        for rule in rules:
            fid  = rule.get('field_id', '')
            home = all_owned.get(fid)

            if home is not None and home != declared_segment:
                suspect.append({
                    **rule,
                    '_review_reason': (
                        f'Field {fid} belongs to segment {home}, '
                        f'not {declared_segment}. Likely cross-reference hallucination.'
                    ),
                    '_declared_segment': declared_segment,
                    '_correct_segment':  home,
                })
            else:
                clean.append(rule)

        return clean, suspect

    def _validate_condition(self, cond: dict, path: str) -> list[str]:
        issues: list[str] = []
        field_ref = cond.get('field', '')
        operator  = cond.get('operator', '')

        if not field_ref:
            issues.append(f'INVALID: {path}.field is missing')
        elif not FIELD_REF_PATTERN.match(field_ref):
            issues.append(f'WARN: {path}.field "{field_ref}" may not match SEGMENT.NNN-XX format')

        if not operator:
            issues.append(f'INVALID: {path}.operator is missing')
        elif operator not in VALID_OPERATORS:
            issues.append(f'INVALID: {path}.operator "{operator}" is not a known operator')

        if operator in ('in', 'not_in') and not isinstance(cond.get('value'), list):
            issues.append(
                f'INVALID: {path} operator "{operator}" requires value to be a list, '
                f'got {type(cond.get("value")).__name__}'
            )

        return issues
