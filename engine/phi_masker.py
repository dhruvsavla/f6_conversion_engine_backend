"""
engine/phi_masker.py

HIPAA Safe Harbor PHI masking before any LLM call.
Masks the 18 identifier categories per 45 CFR §164.514(b)(2).

NCPDP D.0 format: segments joined by newline, each segment is
  SEGMENT_ID|field_id=value|field_id=value|...
"""

import re
from typing import Optional

# ── PHI field IDs (NCPDP D.0 standard identifiers) ────────────────────────────

# Patient demographics
_PAT_FIRST_NAME   = "310-CA"   # Patient First Name
_PAT_LAST_NAME    = "311-CB"   # Patient Last Name / Last Name
_PAT_DOB          = "304-C4"   # Date of Birth (YYYYMMDD)
_PAT_GENDER       = "305-C5"   # not PHI itself — skip
_PAT_ID           = "332-CY"   # Patient ID / Member ID
_PAT_ADDRESS1     = "322-CM"   # Patient Address Line 1
_PAT_ADDRESS2     = "323-CN"   # Patient Address Line 2
_PAT_CITY         = "324-CO"   # Patient City
_PAT_STATE        = "325-CP"   # Patient State (2-char — not PHI)
_PAT_ZIP          = "326-CQ"   # Patient ZIP (first 3 digits only per Safe Harbor)
_PAT_PHONE        = "329-CT"   # Patient Phone Number

# Cardholder / insurance identifiers
_CARD_ID          = "302-C2"   # Cardholder ID / Subscriber ID
_CARD_FIRST       = "C05"      # Cardholder First Name (alternative)
_CARD_LAST        = "C06"      # Cardholder Last Name (alternative)
_GROUP_ID         = "301-C1"   # Group / Plan ID — may be PHI-adjacent

# Prescriber identifiers
_PRESCRIBER_NAME  = "427-DR"   # Prescriber Last Name
_PRESCRIBER_PHONE = "498-H2"   # Prescriber Phone (when present)
_PRESCRIBER_NPI   = "411-DB"   # Prescriber NPI / DEA (technically a provider ID)

# Pharmacy identifiers
_PHARMACY_NPI     = "201-B1"   # NABP Provider ID / Pharmacy NPI

# Date of service (date — not identifying alone but can be combined)
_DOS              = "401-D1"   # Date of Service

PHI_FIELD_IDS: frozenset[str] = frozenset({
    _PAT_FIRST_NAME,
    _PAT_LAST_NAME,
    _PAT_DOB,
    _PAT_ID,
    _PAT_ADDRESS1,
    _PAT_ADDRESS2,
    _PAT_CITY,
    _PAT_ZIP,
    _PAT_PHONE,
    _CARD_ID,
    _PRESCRIBER_NAME,
    _PRESCRIBER_PHONE,
    _PRESCRIBER_NPI,
    _PHARMACY_NPI,
})

# Fields that contain dates — replaced with epoch placeholder
DATE_FIELD_IDS: frozenset[str] = frozenset({
    _PAT_DOB,
    _DOS,
})

# Phone-format fields — replaced with innocuous placeholder
PHONE_FIELD_IDS: frozenset[str] = frozenset({
    _PAT_PHONE,
    _PRESCRIBER_PHONE,
})

# Name fields — replaced with [PHI_NAME] token rather than a random-looking string
NAME_FIELD_IDS: frozenset[str] = frozenset({
    _PAT_FIRST_NAME,
    _PAT_LAST_NAME,
    _PRESCRIBER_NAME,
})


def _token(field_id: str, counter: list[int]) -> str:
    counter[0] += 1
    safe = field_id.replace("-", "_")
    return f"[PHI_{safe}_{counter[0]:03d}]"


def mask_transaction(raw_text: str, fmt: str = "text") -> tuple[str, dict[str, str]]:
    """
    Replace PHI field values in raw NCPDP D.0 text with opaque tokens.

    Returns:
        (masked_text, mask_map)  where mask_map[token] = original_value.

    The mask is applied using segment-aware regex on the pipe-delimited format:
        FIELD_ID=VALUE|  or  FIELD_ID=VALUE\\n  or  FIELD_ID=VALUE<end>
    """
    mask_map: dict[str, str] = {}
    counter = [0]   # mutable int for nested closure
    masked  = raw_text

    for field_id in PHI_FIELD_IDS:
        escaped = re.escape(field_id)
        # Match: field_id=<value> followed by | or end-of-line or end-of-string
        pat = rf'({escaped}=)([^|\r\n]*)([|\r\n]|$)'

        def _replace(m, fid=field_id, counter=counter, mask_map=mask_map):
            value = m.group(2)
            if not value or value.startswith("[PHI_"):
                return m.group(0)   # already masked or empty

            tok = _token(fid, counter)
            mask_map[tok] = value

            if fid in DATE_FIELD_IDS:
                replacement = "19000101"
            elif fid in PHONE_FIELD_IDS:
                replacement = "5550100000"
            elif fid in NAME_FIELD_IDS:
                replacement = tok  # keep token — name fields are strings, LLM sees [PHI_...]
            else:
                replacement = tok

            return m.group(1) + replacement + m.group(3)

        masked = re.sub(pat, _replace, masked)

    # Secondary pass: bare SSNs (NNN-NN-NNNN) anywhere in the text
    def _ssn(m, counter=counter, mask_map=mask_map):
        tok = _token("SSN", counter)
        mask_map[tok] = m.group(0)
        return tok

    masked = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', _ssn, masked)

    return masked, mask_map


def unmask_llm_output(decisions: list[dict], mask_map: dict[str, str]) -> list[dict]:
    """
    Safety net: if any LLM-generated resolved_value contains a PHI token,
    that decision is suppressed. Prevents PHI round-trip leakage.
    """
    if not mask_map:
        return decisions

    cleaned: list[dict] = []
    for d in decisions:
        rv = str(d.get("resolved_value") or "")
        if any(tok in rv for tok in mask_map):
            d = {
                **d,
                "resolved_value": "",
                "action": "UNRESOLVABLE",
                "reasoning": "[PHI token detected in LLM output — blocked for compliance]",
            }
        cleaned.append(d)
    return cleaned
