from typing import Any, Dict

from .rules_reader import RuleSet
from .segment_parser import ParsedTransaction


def detect(parsed: ParsedTransaction, ruleset: RuleSet) -> str:
    """Detect the NCPDP transaction type from parsed D.0 segments using rules."""
    hdr = _fmap(parsed, "HDR")
    clm = _fmap(parsed, "CLM")
    pat = _fmap(parsed, "PAT")
    ins = _fmap(parsed, "INS")

    tx_code = hdr.get("103-A3", "").strip().upper()
    compound_code = clm.get("406-D6", "").strip()
    patient_residence = pat.get("384-7E", "").strip()
    group_id = ins.get("301-C1", "").strip()

    priority = ruleset.global_config.get(
        "transaction_detection_priority",
        ["REVERSAL", "ELIGIBILITY", "PRIOR_AUTH", "COMPOUND",
         "LTC", "COB", "MEDICARE_PART_D", "CONTROLLED", "SPECIALTY", "RETAIL"],
    )

    for tx_type in priority:
        rules = ruleset.rules_by_tx.get(tx_type)
        if not rules:
            continue
        detection = rules.get("detection", {})
        if _matches(detection, tx_code, compound_code, patient_residence, group_id, parsed):
            return tx_type

    return "RETAIL"


def _fmap(parsed: ParsedTransaction, seg: str) -> Dict[str, str]:
    return {f.field_id: f.value for f in parsed.segments.get(seg, [])}


def _matches(
    detection: Dict[str, Any],
    tx_code: str,
    compound_code: str,
    patient_residence: str,
    group_id: str,
    parsed: ParsedTransaction,
) -> bool:
    if "transaction_code" in detection:
        codes = [c.upper() for c in detection["transaction_code"]]
        if tx_code not in codes:
            return False

    if "compound_code" in detection:
        if compound_code not in detection["compound_code"]:
            return False

    if "compound_code_not" in detection:
        if compound_code in detection["compound_code_not"]:
            return False

    if "patient_residence" in detection:
        if patient_residence not in detection["patient_residence"]:
            return False

    if "patient_residence_not" in detection:
        if patient_residence in detection["patient_residence_not"]:
            return False

    if "group_id_prefix" in detection:
        if not group_id.startswith(detection["group_id_prefix"]):
            return False

    if "segment_present" in detection:
        for seg in detection["segment_present"]:
            if seg not in parsed.segments:
                return False

    if "segment_not_present" in detection:
        for seg in detection["segment_not_present"]:
            if seg in parsed.segments:
                return False

    # Check that a specific field_id is present in a segment
    if "field_present" in detection:
        for seg_name, field_ids in detection["field_present"].items():
            seg_field_ids = {f.field_id for f in parsed.segments.get(seg_name, [])}
            ids = field_ids if isinstance(field_ids, list) else [field_ids]
            if not any(fid in seg_field_ids for fid in ids):
                return False

    # Check submission clarification codes
    if "submission_clarification_code" in detection:
        clm = _fmap(parsed, "CLM")
        code = clm.get("420-DK", "").strip()
        if code not in detection["submission_clarification_code"]:
            return False

    return True
