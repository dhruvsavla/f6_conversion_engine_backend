"""
backend/agent/audit_builder.py

Builds the structured audit trail from a MappingResult.
Iterates MappedSegment.all_fields() which covers every change type bucket.
"""
from typing import Any, Dict, List

from models.schemas import MappingResult


def build_audit(result: MappingResult) -> Dict[str, Any]:
    """Build a structured audit trail from a MappingResult."""
    entries: List[Dict[str, Any]] = []
    summary = {
        'added': 0, 'carried': 0, 'transformed': 0,
        'removed': 0, 'modified': 0, 'missing': 0,
    }

    for seg in result.segments:
        # all_fields() returns every field across all change-type buckets
        for f in seg.all_fields():
            ct = f.change_type
            if ct in summary:
                summary[ct] += 1

            entries.append({
                'segment': seg.normalized_id,
                'occurrence': seg.occurrence,
                'from_field_id': '' if ct == 'added' else f.field_id,
                'to_field_id': '' if ct == 'removed' else f.field_id,
                'field_name': f.field_name,
                'change_type': ct,
                'old_value': f.old_value,
                'new_value': f.new_value,
                'rule_applied': f.rule_applied,
                'notes': f.notes,
                'condition_evaluated': f.condition_evaluated,
                'condition_result': f.condition_passed,
                'condition_expression': f.condition_expression,
            })

    findings = result.findings  # list[dict] with severity, code, message, segment, field_id
    summary['warnings'] = sum(1 for f in findings if f.get('severity') == 'WARN')
    summary['errors']   = sum(1 for f in findings if f.get('severity') == 'ERROR')

    return {'summary': summary, 'findings': findings, 'entries': entries}
