"""
engine/llm_merger.py

Merges LLM-resolved decisions back into a partial F6 output and
produces audit entries tagged with "LLM:" so they appear distinctly
in the ConversionDetailPage AI badge logic.
"""

import re
from engine.llm_resolver import LLMDecision

_LLM_MODEL = "claude-sonnet-4-6"


def _make_audit_entry(decision: LLMDecision) -> dict:
    """Convert an LLMDecision into an audit_entry dict."""
    change = "modified" if decision.action == "RESOLVED" else "missing"
    return {
        "segment":              decision.segment_id or "LLM",
        "occurrence":           1,
        "from_field_id":        decision.field_id,
        "to_field_id":          decision.field_id,
        "field_name":           decision.field_name,
        "change_type":          change,
        "old_value":            decision.original_value,
        "new_value":            decision.resolved_value,
        "rule_applied":         f"LLM:{_LLM_MODEL}:{decision.confidence}",
        "notes":                decision.reasoning,
        "condition_evaluated":  False,
        "condition_passed":     True,
        "condition_expression": "",
    }


def merge_llm_decisions(
    partial_f6_text: str,
    decisions:       list[LLMDecision],
) -> tuple[str, list[dict], dict]:
    """
    Merge LLM decisions into the partial F6 output.

    Returns:
        merged_f6_text      — F6 text with resolved values injected where found
        llm_audit_entries   — list of audit entry dicts (tagged LLM:)
        summary_delta       — {modified: int, errors: int} adjustments for the summary
    """
    if not decisions:
        return partial_f6_text, [], {}

    llm_audit_entries: list[dict] = []
    resolved_count    = 0
    unresolvable_count = 0
    merged = partial_f6_text

    for d in decisions:
        entry = _make_audit_entry(d)
        llm_audit_entries.append(entry)

        if d.action == "RESOLVED" and d.resolved_value:
            resolved_count += 1
            # Try to replace an existing field value in the F6 output
            pat = rf'({re.escape(d.field_id)}=)([^|\r\n]*)'
            if re.search(pat, merged):
                merged = re.sub(
                    pat,
                    lambda m, v=d.resolved_value: m.group(1) + v,
                    merged,
                    count=1,
                )
            # If the field is not yet in the output, we record it in the audit
            # but don't blindly append to the F6 text (assembler controls structure)
        else:
            unresolvable_count += 1

    actually_resolved = sum(
    1 for d in decisions
    if d.resolved_value and d.action != "UNRESOLVABLE"
    )
    summary_delta = {
        "modified": actually_resolved,
        "errors":   max(0, unresolvable_count),
    }

    return merged, llm_audit_entries, summary_delta
