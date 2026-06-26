from pathlib import Path
from typing import Any, AsyncGenerator, Dict

from . import audit_builder, f6_assembler, field_mapper, rules_reader, segment_parser, transaction_detector

RULES_DIR = Path(__file__).parent.parent / "rules"


async def convert_stream(d0_text: str) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Agentic conversion pipeline. Yields SSE-style event dicts for each step,
    then a final 'result' event with the complete conversion output.
    """

    def step(id: str, label: str, status: str, detail: str = "") -> Dict[str, Any]:
        return {"type": "step", "data": {"id": id, "label": label, "status": status, "detail": detail}}

    # STEP 1 — Read rules
    yield step("reading_rules", "Reading rule files from rules/ folder", "running")
    try:
        ruleset = rules_reader.load_all(str(RULES_DIR))
    except Exception as e:
        yield step("reading_rules", "Reading rule files from rules/ folder", "error", str(e))
        yield {"type": "error", "data": {"message": f"Failed to load rules: {e}"}}
        return
    yield step(
        "reading_rules", "Reading rule files from rules/ folder", "complete",
        f"Loaded {len(ruleset.files)} rule files, {ruleset.total_field_rules} field rules",
    )

    # STEP 2 — Parse D.0
    yield step("parsing", "Parsing D.0 segments", "running")
    try:
        parsed = segment_parser.parse_d0(d0_text)
    except ValueError as e:
        yield step("parsing", "Parsing D.0 segments", "error", str(e))
        yield {"type": "error", "data": {"message": str(e)}}
        return
    seg_types = len(parsed.segment_order)
    seg_total = len(parsed.segments)
    detail = f"Parsed {seg_total} segments ({seg_types} types), {parsed.total_fields} fields [{parsed.fmt} format]"
    yield step("parsing", "Parsing D.0 segments", "complete", detail)

    # STEP 3 — Detect transaction type
    yield step("detecting", "Detecting transaction type", "running")
    tx_type = transaction_detector.detect(parsed, ruleset)
    yield step("detecting", "Detecting transaction type", "complete", f"Detected: {tx_type}")

    # STEP 4 — Load applicable rules
    yield step("planning", "Loading applicable rules", "running")
    tx_rules = ruleset.get_rules_for(tx_type)
    seg_count = len(tx_rules.get("segments", {}))
    field_count = sum(len(v) for v in tx_rules.get("segments", {}).values())
    yield step(
        "planning", "Loading applicable rules", "complete",
        f"Applying {field_count} field rules across {seg_count} segments",
    )

    # STEP 5 — Map fields
    yield step("mapping", "Mapping fields segment by segment", "running")
    try:
        mapping = field_mapper.map_fields(parsed, tx_rules)
    except Exception as e:
        yield step("mapping", "Mapping fields segment by segment", "error", str(e))
        yield {"type": "error", "data": {"message": f"Field mapping error: {e}"}}
        return
    total_mapped = sum(
        len(seg.in_place) + len(seg.added) + len(seg.removed)
        for seg in mapping.segments
    )
    yield step("mapping", "Mapping fields segment by segment", "complete", f"Mapped {total_mapped} fields")

    # STEP 6 — Assemble F6 output
    yield step("assembling", "Assembling F6 output", "running")
    f6_output = f6_assembler.assemble(mapping)
    yield step("assembling", "Assembling F6 output", "complete", "F6 transaction assembled")

    # STEP 7 — Validate
    yield step("validating", "Running validation rules", "running")
    findings = mapping.findings
    error_count = sum(1 for f in findings if f.get("severity") == "ERROR")
    warn_count = sum(1 for f in findings if f.get("severity") == "WARN")
    yield step(
        "validating", "Running validation rules", "complete",
        f"{len(findings)} findings ({error_count} errors, {warn_count} warnings)",
    )

    # STEP 8 — Build audit trail
    yield step("auditing", "Building audit trail", "running")
    audit = audit_builder.build_audit(mapping)
    yield step("auditing", "Building audit trail", "complete", f"Audit: {len(audit['entries'])} entries")

    # Final result event
    yield {
        "type": "result",
        "data": {
            "transaction_type": tx_type,
            "f6_output": f6_output,
            "d0_input": d0_text,
            "audit": audit,
        },
    }
