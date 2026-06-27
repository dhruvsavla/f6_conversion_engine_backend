"""
agent/reverse_audit_builder.py

Builds the audit trail for F6 → D.0 conversions.
"""
from __future__ import annotations
from .reverse_field_mapper import ReverseMappingResult

ACTION_TO_CHANGE_TYPE = {
    'carry':               'carried',
    'drop':                'dropped',
    'restore':             'restored',
    'reverse_transform':   'transformed',
    'warn_cannot_reverse': 'missing',
    'warn_cannot_restore': 'missing',
}


def build_reverse_audit(result: ReverseMappingResult) -> dict:
    entries  = []
    findings = []
    summary  = {
        'carried': 0, 'dropped': 0, 'restored': 0,
        'transformed': 0, 'missing': 0,
        'added': 0, 'removed': 0, 'modified': 0,
        'warnings': 0, 'errors': 0,
    }

    for seg in result.segments:
        all_fields = seg.d0_fields + seg.dropped + seg.restored + seg.warned

        for rmf in all_fields:
            ct = ACTION_TO_CHANGE_TYPE.get(rmf.reverse_action, 'carried')

            entries.append({
                'segment':             seg.normalized_id,
                'occurrence':          rmf.occurrence,
                'from_field_id':       rmf.field_id,
                'to_field_id':         rmf.field_id,
                'field_name':          rmf.field_name,
                'change_type':         ct,
                'old_value':           rmf.f6_value,
                'new_value':           rmf.d0_value,
                'rule_applied':        rmf.reverse_action.upper(),
                'notes':               rmf.notes,
                'condition_evaluated': False,
                'condition_passed':    True,
                'condition_expression': '',
            })

            if ct in summary:
                summary[ct] += 1

            if rmf.warn_code:
                sev = rmf.warn_severity or 'WARN'
                findings.append({
                    'severity':   sev,
                    'code':       rmf.warn_code,
                    'message':    rmf.warn_message,
                    'segment':    seg.normalized_id,
                    'field_id':   rmf.field_id,
                    'occurrence': rmf.occurrence,
                })
                if sev == 'ERROR':
                    summary['errors'] += 1
                else:
                    summary['warnings'] += 1

    return {'summary': summary, 'findings': findings, 'entries': entries}
