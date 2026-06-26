from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List

from .condition_evaluator import ConditionEvaluator
from .segment_parser import ParsedTransaction
from .transformer import apply_transform


@dataclass
class FieldEntry:
    field_id: str
    field_name: str
    old_value: str
    new_value: str
    change_type: str  # carried | transformed | added | removed | modified
    rule_applied: str
    notes: str
    condition_evaluated: bool = False
    condition_result: bool = True
    condition_expression: str = ""


@dataclass
class SegmentResult:
    name: str
    occurrence: int
    in_place: List[FieldEntry]  # non-removed D.0 fields in original order
    added: List[FieldEntry]     # new F6 fields in rules order
    removed: List[FieldEntry]   # deprecated D.0 fields


@dataclass
class MappingResult:
    segments: List[SegmentResult]  # ordered list — one entry per input segment occurrence
    findings: List[Dict[str, Any]]

    @property
    def segment_order(self) -> List[str]:
        """Unique segment IDs in original order (backward compat)."""
        seen: dict = {}
        for seg in self.segments:
            seen.setdefault(seg.name, True)
        return list(seen.keys())


_evaluator = ConditionEvaluator()


def _check_condition(rule: Dict, parsed: ParsedTransaction) -> tuple[bool, str, bool]:
    """Return (should_apply, expression_str, was_evaluated)."""
    condition = rule.get("condition")
    if not condition:
        return True, "", False
    result, expr = _evaluator.evaluate(condition, parsed)
    return result, expr, True


def _resolve_cases(rule: Dict, parsed: ParsedTransaction) -> Dict:
    """Resolve a cases rule to an effective sub-rule by evaluating each when-block."""
    for case in rule.get("cases", []):
        if case.get("default"):
            then = case.get("then", {})
            return {**rule, **then, "action": then.get("action", "carry")}
        when = case.get("when")
        if when:
            passed, _ = _evaluator.evaluate({"if": when}, parsed)
            if passed:
                then = case.get("then", {})
                return {**rule, **then, "action": then.get("action", "carry")}
    return {**rule, "action": "carry"}


def _build_entry(
    fid: str,
    val: str,
    rule: Dict,
    action: str,
    cond_eval: bool,
    cond_result: bool,
    cond_expr: str,
) -> FieldEntry:
    """Build a FieldEntry for carry / transform / modify actions."""
    field_name = rule.get("field_name", f"Field {fid}")
    notes = rule.get("notes", "")

    if action == "transform":
        transform_name = rule.get("transform", "")
        params = dict(rule.get("params", {}))
        if "value" in rule:
            params["value"] = rule["value"]
        new_val = apply_transform(val, transform_name, params)
        return FieldEntry(
            field_id=fid, field_name=field_name,
            old_value=val, new_value=new_val, change_type="transformed",
            rule_applied=transform_name, notes=notes,
            condition_evaluated=cond_eval, condition_result=cond_result,
            condition_expression=cond_expr,
        )

    if action == "modify":
        mapping = rule.get("map", {})
        default = rule.get("default_value", val)
        new_val = mapping.get(val, str(default))
        return FieldEntry(
            field_id=fid, field_name=field_name,
            old_value=val, new_value=new_val, change_type="modified",
            rule_applied="MAP_CODE", notes=notes,
            condition_evaluated=cond_eval, condition_result=cond_result,
            condition_expression=cond_expr,
        )

    # default: carry
    return FieldEntry(
        field_id=fid, field_name=field_name,
        old_value=val, new_value=val, change_type="carried",
        rule_applied="CARRY", notes=notes,
        condition_evaluated=cond_eval, condition_result=cond_result,
        condition_expression=cond_expr,
    )


def map_fields(parsed: ParsedTransaction, tx_rules: Dict[str, Any]) -> MappingResult:
    """Apply per-field rules to produce a structured mapping result."""
    segment_rules: Dict[str, List[Dict]] = tx_rules.get("segments", {})
    result_segs: List[SegmentResult] = []
    findings: List[Dict[str, Any]] = []

    for seg in parsed.segments:
        seg_id = seg.segment_id
        rules_list: List[Dict] = segment_rules.get(seg_id, [])
        rules_by_id: Dict[str, Dict] = {
            r["field_id"]: r for r in rules_list if r.get("action") != "add"
        }
        no_seg_rules = not rules_list

        in_place: List[FieldEntry] = []
        removed: List[FieldEntry] = []
        added: List[FieldEntry] = []

        # Process each D.0 field in original order
        for pf in seg.fields:
            fid, val = pf.field_id, pf.value
            rule = rules_by_id.get(fid)

            if rule is None:
                in_place.append(FieldEntry(
                    field_id=fid, field_name=f"Field {fid}",
                    old_value=val, new_value=val, change_type="carried",
                    rule_applied="CARRY_NO_RULE" if no_seg_rules else "IMPLICIT_CARRY",
                    notes="No rule defined; carried unchanged.",
                ))
                continue

            should_apply, cond_expr, cond_eval = _check_condition(rule, parsed)

            if not should_apply:
                in_place.append(FieldEntry(
                    field_id=fid, field_name=rule.get("field_name", f"Field {fid}"),
                    old_value=val, new_value=val, change_type="carried",
                    rule_applied="CONDITION_SKIP", notes=rule.get("notes", ""),
                    condition_evaluated=True, condition_result=False,
                    condition_expression=cond_expr,
                ))
                continue

            action = rule.get("action", "carry")
            effective_rule = rule

            if action == "cases":
                effective_rule = _resolve_cases(rule, parsed)
                action = effective_rule.get("action", "carry")

            if action == "remove":
                removed.append(FieldEntry(
                    field_id=fid, field_name=rule.get("field_name", f"Field {fid}"),
                    old_value=val, new_value="", change_type="removed",
                    rule_applied="REMOVE", notes=rule.get("notes", ""),
                    condition_evaluated=cond_eval, condition_result=True,
                    condition_expression=cond_expr,
                ))
            else:
                in_place.append(_build_entry(fid, val, effective_rule, action, cond_eval, True, cond_expr))

        # Process add rules — new F6 fields not present in D.0
        existing_ids = {pf.field_id for pf in seg.fields}
        for rule in rules_list:
            if rule.get("action") != "add":
                continue
            fid = rule["field_id"]
            if fid in existing_ids:
                continue

            should_apply, _, _ = _check_condition(rule, parsed)
            if not should_apply:
                continue

            field_name = rule.get("field_name", f"Field {fid}")
            default_val = str(rule.get("default_value", ""))
            notes = rule.get("notes", "")

            added.append(FieldEntry(
                field_id=fid, field_name=field_name,
                old_value="", new_value=default_val, change_type="added",
                rule_applied="ADD_DEFAULT", notes=notes,
            ))

            if rule.get("warn_if_empty") and not default_val:
                findings.append({
                    "severity": rule.get("warn_severity", "WARN"),
                    "code": rule.get("warn_code", "NEW"),
                    "message": rule.get("warn_message", f"{field_name} is empty."),
                    "segment": seg_id,
                    "field_id": fid,
                })

        result_segs.append(SegmentResult(
            name=seg_id,
            occurrence=seg.occurrence,
            in_place=in_place,
            added=added,
            removed=removed,
        ))

    return MappingResult(segments=result_segs, findings=findings)
