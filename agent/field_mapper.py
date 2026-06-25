from dataclasses import dataclass
from typing import Any, Dict, List

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


@dataclass
class SegmentResult:
    name: str
    in_place: List[FieldEntry]  # non-removed D.0 fields in original order
    added: List[FieldEntry]     # new F6 fields in rules order
    removed: List[FieldEntry]   # deprecated D.0 fields


@dataclass
class MappingResult:
    segments: Dict[str, SegmentResult]
    segment_order: List[str]
    findings: List[Dict[str, Any]]


def map_fields(parsed: ParsedTransaction, tx_rules: Dict[str, Any]) -> MappingResult:
    """Apply per-field rules to produce a structured mapping result."""
    segment_rules: Dict[str, List[Dict]] = tx_rules.get("segments", {})
    segments: Dict[str, SegmentResult] = {}
    findings: List[Dict[str, Any]] = []

    for seg_name in parsed.segment_order:
        d0_fields = parsed.segments[seg_name]
        rules_list: List[Dict] = segment_rules.get(seg_name, [])
        rules_by_id: Dict[str, Dict] = {r["field_id"]: r for r in rules_list}

        in_place: List[FieldEntry] = []
        removed: List[FieldEntry] = []
        added: List[FieldEntry] = []

        # Process each D.0 field in its original order
        for pf in d0_fields:
            fid = pf.field_id
            val = pf.value
            rule = rules_by_id.get(fid)

            if rule is None:
                in_place.append(FieldEntry(
                    field_id=fid, field_name=f"Field {fid}",
                    old_value=val, new_value=val, change_type="carried",
                    rule_applied="IMPLICIT_CARRY",
                    notes="No rule defined; carried unchanged.",
                ))
                continue

            action = rule.get("action", "carry")
            field_name = rule.get("field_name", f"Field {fid}")
            notes = rule.get("notes", "")

            if action == "carry":
                in_place.append(FieldEntry(
                    field_id=fid, field_name=field_name,
                    old_value=val, new_value=val, change_type="carried",
                    rule_applied="CARRY", notes=notes,
                ))

            elif action == "transform":
                transform_name = rule.get("transform", "")
                params = dict(rule.get("params", {}))
                if "value" in rule:
                    params["value"] = rule["value"]
                new_val = apply_transform(val, transform_name, params)
                in_place.append(FieldEntry(
                    field_id=fid, field_name=field_name,
                    old_value=val, new_value=new_val, change_type="transformed",
                    rule_applied=transform_name, notes=notes,
                ))

            elif action == "remove":
                removed.append(FieldEntry(
                    field_id=fid, field_name=field_name,
                    old_value=val, new_value="", change_type="removed",
                    rule_applied="REMOVE", notes=notes,
                ))

            elif action == "modify":
                mapping = rule.get("map", {})
                default = rule.get("default_value", val)
                new_val = mapping.get(val, str(default))
                in_place.append(FieldEntry(
                    field_id=fid, field_name=field_name,
                    old_value=val, new_value=new_val, change_type="modified",
                    rule_applied="MAP_CODE", notes=notes,
                ))

        # Process "add" rules — new F6 fields not present in D.0
        existing_ids = {pf.field_id for pf in d0_fields}
        for rule in rules_list:
            if rule.get("action") != "add":
                continue
            fid = rule["field_id"]
            if fid in existing_ids:
                continue  # field already came in from D.0

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
                    "segment": seg_name,
                    "field_id": fid,
                })

        segments[seg_name] = SegmentResult(
            name=seg_name, in_place=in_place, added=added, removed=removed,
        )

    # Any D.0 segments with no rule definitions — carry all fields
    for seg_name in parsed.segment_order:
        if seg_name not in segments:
            in_place = [
                FieldEntry(
                    field_id=pf.field_id, field_name=f"Field {pf.field_id}",
                    old_value=pf.value, new_value=pf.value, change_type="carried",
                    rule_applied="CARRY_NO_RULE",
                    notes="Segment has no rules defined; all fields carried.",
                )
                for pf in parsed.segments[seg_name]
            ]
            segments[seg_name] = SegmentResult(
                name=seg_name, in_place=in_place, added=[], removed=[],
            )

    return MappingResult(segments=segments, segment_order=parsed.segment_order, findings=findings)
