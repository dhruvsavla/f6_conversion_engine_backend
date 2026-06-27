"""
backend/agent/field_mapper.py

Maps each ParsedSegment to a MappedSegment by applying JSON rules.
Condition-aware: rules can be gated by condition blocks.

Rule action types:
  carry     — copy field unchanged
  transform — apply a named transform (ZERO_PAD_LEFT, SET_VALUE, …)
  add       — new F6 field absent from D.0; append with default value
  remove    — field deprecated in F6; rendered as ~~field=value~~ in output
  modify    — remap value via lookup table; change_type='modified' if value changed
  cases     — meta-action: pick action based on another field's value
"""
from __future__ import annotations

import logging
from typing import Optional

from .condition_evaluator import ConditionEvaluator
from .segment_parser import ParsedTransaction, ParsedSegment, ParsedField
from .transformer import apply_transform

# Import from sibling package (models/ is a sibling of agent/)
from models.schemas import MappedField, MappedSegment, MappingResult

logger = logging.getLogger(__name__)

_evaluator = ConditionEvaluator()


# ── FieldMapper class ─────────────────────────────────────────────────────────

class FieldMapper:

    def map(self, tx: ParsedTransaction, rules: dict) -> MappingResult:
        """
        Main entry point. Maps all segments in the ParsedTransaction.
        Preserves original segment order and occurrence.
        """
        tx_type = tx.detected_type if hasattr(tx, 'detected_type') else 'RETAIL'
        segment_rules = rules.get('segments', {})

        mapped_segments: list[MappedSegment] = []
        findings: list[dict] = [] # <--- NEW: Initialize findings list

        for parsed_seg in tx.segments:
            seg_rule_list = segment_rules.get(parsed_seg.normalized_id, [])
            mapped = self._map_segment(parsed_seg, seg_rule_list, tx, findings)
            mapped_segments.append(mapped)

        return MappingResult(
            segments=mapped_segments,
            detected_type=tx_type,
            parse_errors=tx.all_errors(),
            findings=findings # <--- NEW: Return findings to the orchestrator
        )

    def _map_segment(
        self,
        parsed_seg: ParsedSegment,
        seg_rules: list[dict],
        tx: ParsedTransaction,
        findings: list[dict] # <--- Pass findings array down
    ) -> MappedSegment:
        
        mapped = MappedSegment(
            segment_id=parsed_seg.segment_id,
            normalized_id=parsed_seg.normalized_id,
            occurrence=parsed_seg.occurrence,
            raw_index=parsed_seg.raw_index,
        )

        d0_field_map: dict[str, ParsedField] = {
            f.field_id: f for f in parsed_seg.fields
        }
        handled_field_ids: set[str] = set()

        for rule in seg_rules:
            result = self._apply_rule(rule, d0_field_map, parsed_seg, tx, findings)
            if result is None:
                continue

            field_id = rule.get('field_id', '')
            handled_field_ids.add(field_id)

            change_type = result.change_type
            if change_type == 'carried': mapped.carried.append(result)
            elif change_type == 'transformed': mapped.transformed.append(result)
            elif change_type == 'added': mapped.added.append(result)
            elif change_type == 'removed': mapped.removed.append(result)
            elif change_type == 'modified': mapped.modified.append(result)
            elif change_type == 'missing': mapped.missing.append(result)

        for d0_field in parsed_seg.fields:
            if d0_field.field_id not in handled_field_ids:
                mapped.carried.append(MappedField(
                    field_id=d0_field.field_id,
                    field_name=d0_field.field_id,
                    change_type='carried',
                    old_value=d0_field.value,
                    new_value=d0_field.value,
                    rule_applied='IMPLICIT_CARRY',
                    notes='No rule defined for this field. Carried unchanged.',
                    occurrence=parsed_seg.occurrence,
                ))

        return mapped

    def _apply_rule(
        self,
        rule: dict,
        d0_field_map: dict[str, ParsedField],
        parsed_seg: ParsedSegment,
        tx: ParsedTransaction,
        findings: list[dict] # <--- Passed from _map_segment
    ) -> Optional[MappedField]:
        
        field_id    = rule.get('field_id', '')
        field_name  = rule.get('field_name', field_id)
        action      = rule.get('action', 'carry')
        occurrence  = parsed_seg.occurrence

        # Step 1: Resolve 'cases' action first
        if action == 'cases':
            resolved_rule = self._resolve_cases(rule, tx)
            if resolved_rule is None:
                return None
            rule = resolved_rule
            action = rule.get('action', 'carry')

        # Step 2: Evaluate condition guard
        condition_evaluated = False
        condition_passed    = True
        condition_expr      = ''

        condition_block = rule.get('condition')
        if condition_block:
            condition_evaluated = True
            if_block = condition_block.get('if')
            if if_block:
                condition_passed, condition_expr = _evaluator.evaluate(if_block, tx) # <--- FIXED
            
            if not condition_passed:
                d0_field = d0_field_map.get(field_id)
                if d0_field:
                    return MappedField(
                        field_id=field_id, field_name=field_name, change_type='carried',
                        old_value=d0_field.value, new_value=d0_field.value, rule_applied='CONDITION_NOT_MET_IMPLICIT_CARRY',
                        notes=f'Condition not satisfied ({condition_expr}). Carried.', occurrence=occurrence,
                        condition_evaluated=True, condition_passed=False, condition_expression=condition_expr,
                    )
                return None

        # Step 3: Apply the concrete action
        d0_field = d0_field_map.get(field_id)

        # ── CARRY ─────────────────────────────────────────────────────────────
        if action == 'carry':
            if d0_field is None:
                # Catch missing fields that are conditionally mandatory
                if rule.get('warn_if_empty') or rule.get('mandatory_f6'):
                    findings.append({
                        "severity": rule.get("warn_severity", "ERROR"),
                        "code": rule.get("warn_code", "MISSING"),
                        "message": rule.get("warn_message", f"{field_name} is required but missing."),
                        "segment": parsed_seg.segment_id,
                        "field_id": field_id,
                    })
                    return MappedField(
                        field_id=field_id, field_name=field_name, change_type='missing',
                        old_value='', new_value='', rule_applied='CARRY_MISSING',
                        notes='Field required by condition but absent from D.0.', occurrence=occurrence,
                        condition_evaluated=condition_evaluated, condition_passed=condition_passed, condition_expression=condition_expr,
                    )
                return None  
            
            return MappedField(
                field_id=field_id, field_name=field_name, change_type='carried',
                old_value=d0_field.value, new_value=d0_field.value, rule_applied='CARRY',
                notes=rule.get('notes', ''), occurrence=occurrence,
                condition_evaluated=condition_evaluated, condition_passed=condition_passed, condition_expression=condition_expr,
            )

        # ── TRANSFORM ─────────────────────────────────────────────────────────
        if action == 'transform':
            if d0_field is None:
                return None
            
            transform_name = rule.get("transform", "")
            params = dict(rule.get("params", {}))
            if "value" in rule:
                params["value"] = rule["value"]
            
            # Using the apply_transform imported at the top of your file
            new_val = apply_transform(d0_field.value, transform_name, params)
            
            return MappedField(
                field_id=field_id, field_name=field_name, change_type='transformed',
                old_value=d0_field.value, new_value=new_val, rule_applied=transform_name,
                notes=rule.get('notes', ''), occurrence=occurrence,
                condition_evaluated=condition_evaluated, condition_passed=condition_passed, condition_expression=condition_expr,
            )

        # ── ADD ───────────────────────────────────────────────────────────────
        if action == 'add':
            default_val = rule.get('default_value', '')
            actual_value = d0_field.value if d0_field else default_val

            if rule.get('warn_if_empty') and not actual_value:
                findings.append({
                    "severity": rule.get("warn_severity", "WARN"),
                    "code": rule.get("warn_code", "NEW"),
                    "message": rule.get("warn_message", f"{field_name} is empty."),
                    "segment": parsed_seg.segment_id,
                    "field_id": field_id,
                })
            return MappedField(
                field_id=field_id, field_name=field_name, change_type='added',
                old_value='', new_value=actual_value, rule_applied='ADD',
                notes=rule.get('notes', ''), occurrence=occurrence,
                condition_evaluated=condition_evaluated, condition_passed=condition_passed, condition_expression=condition_expr,
            )

        # ── REMOVE ────────────────────────────────────────────────────────────
        if action == 'remove':
            if d0_field is None:
                return None # Nothing to remove
                
            return MappedField(
                field_id=field_id, field_name=field_name,
                change_type='removed', old_value=d0_field.value, new_value='',
                rule_applied='REMOVE', notes=rule.get('notes', 'Field deprecated in F6.'),
                occurrence=occurrence,
                condition_evaluated=condition_evaluated, condition_passed=condition_passed,
                condition_expression=condition_expr,
            )

        # ── MODIFY ────────────────────────────────────────────────────────────
        if action == 'modify':
            if d0_field is None:
                return None
                
            mapping_table: dict[str, str] = rule.get('map', {})
            new_val = mapping_table.get(d0_field.value.strip(), d0_field.value)
            ct = 'modified' if new_val != d0_field.value else 'carried'
            
            return MappedField(
                field_id=field_id, field_name=field_name,
                change_type=ct, old_value=d0_field.value, new_value=new_val,
                rule_applied='MAP_CODE', notes=rule.get('notes', ''),
                occurrence=occurrence,
                condition_evaluated=condition_evaluated, condition_passed=condition_passed,
                condition_expression=condition_expr,
            )

        logger.warning(f'Unknown action {action!r} for field {field_id}')
        return None

    def _bucket(self, mapped: MappedSegment, mf: MappedField) -> None:
        """Route a MappedField into the correct list on its MappedSegment."""
        ct = mf.change_type
        if ct == 'carried':
            mapped.carried.append(mf)
        elif ct == 'transformed':
            mapped.transformed.append(mf)
        elif ct == 'removed':
            mapped.removed.append(mf)
        elif ct == 'modified':
            mapped.modified.append(mf)
        elif ct == 'added':
            mapped.added.append(mf)
        elif ct == 'missing':
            mapped.missing.append(mf)
        else:
            mapped.carried.append(mf)  # safe fallback

    def _check_condition(
        self, rule: dict, tx: ParsedTransaction
    ) -> tuple[bool, str, bool]:
        """
        Return (should_apply, expression_str, was_evaluated).
        If no condition block exists, was_evaluated=False and should_apply=True.
        """
        condition = rule.get('condition')
        if not condition:
            return True, '', False
        # Field mapper passes the 'if' sub-block directly to the evaluator
        if_block = condition.get('if', condition)
        result, expr = _evaluator.evaluate(if_block, tx)
        return result, expr, True

    def _resolve_cases(self, rule: dict, tx: ParsedTransaction) -> Optional[dict]:
        """
        Evaluate each case in order; return the first matching case's effective rule.
        The effective rule inherits parent rule fields (field_id, field_name, notes)
        and overrides with the case's 'then' block.
        """
        for case in rule.get('cases', []):
            when = case.get('when')

            if when == 'default':
                then = case.get('then', {})
                return {**rule, **then, 'action': then.get('action', 'carry')}

            if isinstance(when, dict):
                passed, _ = _evaluator.evaluate(when, tx)
                if passed:
                    then = case.get('then', {})
                    return {**rule, **then, 'action': then.get('action', 'carry')}

        return None  # No case matched and no default


# ── Module-level backward-compat wrapper ─────────────────────────────────────

_mapper = FieldMapper()


def map_fields(parsed: ParsedTransaction, tx_rules: dict) -> MappingResult:
    """
    Module-level function matching the old interface.
    Callers (orchestrator, tests) don't need to change.
    """
    return _mapper.map(parsed, tx_rules)
