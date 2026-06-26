from typing import Any, Dict, List

from .field_mapper import MappingResult


def build_audit(result: MappingResult) -> Dict[str, Any]:
    """Build a structured audit trail from a MappingResult."""
    entries: List[Dict[str, Any]] = []
    summary = {
        "added": 0, "carried": 0, "transformed": 0,
        "removed": 0, "modified": 0, "missing": 0,
    }

    for seg in result.segments:
        for f in seg.in_place + seg.added + seg.removed:
            ct = f.change_type
            if ct in summary:
                summary[ct] += 1

            entries.append({
                "segment": seg.name,
                "occurrence": seg.occurrence,
                "from_field_id": "" if ct == "added" else f.field_id,
                "to_field_id": "" if ct == "removed" else f.field_id,
                "field_name": f.field_name,
                "change_type": ct,
                "old_value": f.old_value,
                "new_value": f.new_value,
                "rule_applied": f.rule_applied,
                "notes": f.notes,
                "condition_evaluated": f.condition_evaluated,
                "condition_result": f.condition_result,
                "condition_expression": f.condition_expression,
            })

    findings = result.findings
    summary["warnings"] = sum(1 for f in findings if f.get("severity") == "WARN")
    summary["errors"] = sum(1 for f in findings if f.get("severity") == "ERROR")

    return {"summary": summary, "findings": findings, "entries": entries}
