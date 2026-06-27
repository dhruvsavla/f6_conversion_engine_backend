"""
ingestion/extractor/rule_compiler.py

Post-processes raw rule dicts from the LLM before they reach the validator.

Responsibilities:
  - Normalize field_id format (strip stray spaces, ensure NNN-XX)
  - Add field_name fallback (use field_id if field_name is missing)
  - Coerce boolean strings ("true"/"false") to actual booleans
  - Strip keys that are not in the known schema (prevents validator confusion)
  - Move top-level "if" blocks into the correct "condition": {"if": ...} wrapper
    if the LLM forgot to nest them

This layer keeps the validator focused on correctness, not normalization.
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# Keys we forward through to the rule file; all others are stripped
ALLOWED_KEYS = {
    'field_id', 'field_name', 'action', 'mandatory_f6', 'notes',
    'transform', 'value', 'params', 'map',
    'default_value', 'd0_present',
    'cases',
    'condition', 'warn_condition', 'warn_if_empty',
    'warn_code', 'warn_severity', 'warn_message',
    'extraction_confidence',   # kept so reviewers can filter LOW-confidence rules
}

_FIELD_ID_RE = re.compile(r'^\d{3}-[A-Z0-9]{1,4}$')


class RuleCompiler:

    def compile_all(self, raw_rules: list[dict], segment_id: str) -> list[dict]:
        """Apply compile() to each raw rule. Silently drops None results."""
        compiled = []
        for i, rule in enumerate(raw_rules):
            result = self.compile(rule, segment_id, index=i)
            if result is not None:
                compiled.append(result)
        return compiled

    def compile(self, rule: dict, segment_id: str, index: int = 0) -> dict | None:
        """
        Normalize a single raw rule dict.
        Returns None if the rule is so malformed it cannot be salvaged.
        """
        if not isinstance(rule, dict):
            logger.warning('Skipping non-dict rule at index %d in %s', index, segment_id)
            return None

        out = {}

        # ── field_id ─────────────────────────────────────────────────────────
        field_id = str(rule.get('field_id', '')).strip().upper()
        # Collapse "406 - D6" → "406-D6"
        field_id = re.sub(r'\s*-\s*', '-', field_id)
        if not field_id:
            logger.warning('Rule at index %d in %s has no field_id — skipping', index, segment_id)
            return None
        out['field_id'] = field_id

        # ── field_name ────────────────────────────────────────────────────────
        out['field_name'] = str(rule.get('field_name', '') or field_id).strip()

        # ── action ───────────────────────────────────────────────────────────
        action = str(rule.get('action', 'carry')).strip().lower()
        out['action'] = action

        # ── mandatory_f6 — coerce string booleans ────────────────────────────
        mf6 = rule.get('mandatory_f6', False)
        if isinstance(mf6, str):
            mf6 = mf6.lower() in ('true', 'yes', '1')
        out['mandatory_f6'] = bool(mf6)

        # ── notes ─────────────────────────────────────────────────────────────
        out['notes'] = str(rule.get('notes', '')).strip()

        # ── Pass through allowed keys unchanged ───────────────────────────────
        for key in ALLOWED_KEYS - {'field_id', 'field_name', 'action', 'mandatory_f6', 'notes'}:
            if key in rule:
                out[key] = rule[key]

        # ── Repair: LLM occasionally emits a bare "if" at the top level ──────
        # instead of {"condition": {"if": ...}}
        if 'if' in rule and 'condition' not in rule:
            out['condition'] = {'if': rule['if']}
            logger.debug('Repaired bare "if" → "condition.if" for %s in %s', field_id, segment_id)

        # ── warn_if_empty — coerce string boolean ─────────────────────────────
        if 'warn_if_empty' in out:
            wie = out['warn_if_empty']
            if isinstance(wie, str):
                out['warn_if_empty'] = wie.lower() in ('true', 'yes', '1')

        # ── extraction_confidence — normalise casing ──────────────────────────
        if 'extraction_confidence' in out:
            out['extraction_confidence'] = str(out['extraction_confidence']).upper()

        return out
