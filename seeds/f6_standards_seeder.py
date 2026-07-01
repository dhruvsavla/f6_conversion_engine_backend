"""
seeds/f6_standards_seeder.py

Direct seeder for NCPDP Telecommunication Standard Version F6 rules.

Sources (all publicly available):
  - NCPDP F6 Editorial and Best Practices, Version 02, March 2026
  - HHS Final Rule, Federal Register 89 FR 100763, December 13, 2024
  - CMS 0056-IFR, August 21, 2025
  - NCPDP VF6 Implementation Reference Guide (compiled from public sources)

Confidence levels used in notes:
  CONFIRMED  — explicitly stated in the Official F6 Editorial (must implement)
  DERIVED    — inferred from HHS Final Rule or publicly confirmed field changes
  INFERRED   — reasonable inference from public documentation

Usage (run from backend/ directory, or let it auto-resolve):
  cd f6_conversion_engine_backend
  python seeds/f6_standards_seeder.py

Safe to run multiple times — detects existing rule set by name and skips.
Pass --activate to set this rule set as active (default: yes on first seed).
Pass --force to re-seed even if the rule set already exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
# Resolve backend root regardless of where the script is called from.
_backend_dir = Path(__file__).parent.parent.resolve()
if "NCPDP_DB_PATH" not in os.environ:
    os.environ["NCPDP_DB_PATH"] = str(_backend_dir / "ncpdp_converter.db")
sys.path.insert(0, str(_backend_dir))

from database import db  # noqa: E402 — must come after sys.path insert
import db_ops            # noqa: E402


# ── Rule set identity ─────────────────────────────────────────────────────────
RULE_SET_NAME    = "NCPDP F6 Official Standards (Public Sources)"
RULE_SET_VERSION = "F6-Editorial-v02-March-2026"
RULE_SET_DESC    = (
    "Base F6 rules seeded from publicly available NCPDP documents: "
    "F6 Editorial & Best Practices v02 (March 2026), HHS Final Rule (Dec 2024), "
    "CMS 0056-IFR (Aug 2025). "
    "CONFIRMED = Editorial must-implement. "
    "DERIVED = from Final Rule. INFERRED = public doc inference. "
    "Supplement with full NCPDP Implementation Guide via PDF pipeline when obtained."
)


# ═══════════════════════════════════════════════════════════════════════════════
# Rule builder helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _rule(field_id: str, field_name: str, segment_id: str, action: str,
          tx_type: str = "ALL", mandatory: bool = False,
          notes: str = "", confidence: str = "INFERRED", **kwargs) -> dict:
    base = {
        "field_id":         field_id,
        "field_name":       field_name,
        "segment_id":       segment_id,
        "action":           action,
        "transaction_type": tx_type,
        "mandatory_f6":     mandatory,
        "warn_if_empty":    kwargs.pop("warn_if_empty", False),
        "warn_code":        kwargs.pop("warn_code", ""),
        "warn_severity":    kwargs.pop("warn_severity", "WARN"),
        "warn_message":     kwargs.pop("warn_message", ""),
        "notes":            f"[{confidence}] {notes}",
    }
    base.update(kwargs)
    return base


def confirmed(**kw) -> dict:
    kw.setdefault("confidence", "CONFIRMED")
    return _rule(**kw)


def derived(**kw) -> dict:
    kw.setdefault("confidence", "DERIVED")
    return _rule(**kw)


def inferred(**kw) -> dict:
    kw.setdefault("confidence", "INFERRED")
    return _rule(**kw)


# ═══════════════════════════════════════════════════════════════════════════════
# HDR — Transaction Header Segment
# Source: F6 Editorial v02 §3.4; HHS Final Rule; public field reference
# ═══════════════════════════════════════════════════════════════════════════════
HDR_RULES = [

    confirmed(
        field_id="101-A1", field_name="BIN / IIN Number",
        segment_id="HDR", action="transform", mandatory=True,
        notes=(
            "Must be exactly 8 digits in F6. Zero-pad left from D.0 value. "
            "ISO/IEC 7812 expanded BIN from 6 to 8 digits. "
            "VALIDATION: len == 8 and all digits. "
            "Example: '610279' → '00610279'."
        ),
        transform="ZERO_PAD_LEFT",
        params={"length": 8},
        original_length=6,
        warn_if_empty=True,
        warn_code="BIN",
        warn_message="BIN/IIN (101-A1) must be 8 digits in F6.",
    ),

    confirmed(
        field_id="102-A2", field_name="Version / Release Number",
        segment_id="HDR", action="transform", mandatory=True,
        notes=(
            "Must equal 'F6' in all F6 transactions. D.0 value was 'D0'. "
            "Payer responds in D.0 format if 102-A2='D0' is received on F6 connection. "
            "VALIDATION: value.strip() == 'F6'."
        ),
        transform="SET_VALUE",
        value="F6",
    ),

    confirmed(
        field_id="103-A3", field_name="Transaction Code",
        segment_id="HDR", action="carry", mandatory=True,
        notes=(
            "Carry D.0 value unchanged. "
            "Valid F6 codes: B1 (Billing), B2 (Reversal), B3 (Rebill), E1 (Eligibility), "
            "S1/S2/S3 (Service), PA/P1 (Prior Auth), N1/N2/N3 (Info Reporting)."
        ),
    ),

    confirmed(
        field_id="104-A4", field_name="Processor Control Number",
        segment_id="HDR", action="carry", mandatory=False,
        notes=(
            "Secondary routing identifier defined by PBM/processor. Carry unchanged. "
            "EDITORIAL NOTE: Some F6 guide sections incorrectly reference PCN with wrong "
            "field ID. Correct field ID is 104-A4 per F6 Editorial v02 §2.1."
        ),
    ),

    confirmed(
        field_id="109-A9", field_name="Transaction Count",
        segment_id="HDR", action="transform", mandatory=True,
        notes=(
            "CRITICAL: Must be '1' in all F6 transmissions (Editorial §3.4.1). "
            "As of Version E7+, a transmission contains only ONE request transaction. "
            "If > 1 received: respond Header Response Status 501-F1='R', "
            "Transaction Response Status 112-AN='R', Reject Code 511-FB='A9'. "
            "Response Transaction Count must also be '1'."
        ),
        transform="SET_VALUE",
        value="1",
    ),

    confirmed(
        field_id="201-B1", field_name="Service Provider ID (NPI)",
        segment_id="HDR", action="carry", mandatory=True,
        notes="Dispensing pharmacy NPI (10-digit, Type 2). Carry unchanged.",
        warn_if_empty=True,
        warn_code="SPI",
        warn_message="Service Provider ID (201-B1) must be present.",
    ),

    confirmed(
        field_id="202-B2", field_name="Service Provider ID Qualifier",
        segment_id="HDR", action="carry", mandatory=True,
        notes="01=NPI. Carry unchanged.",
    ),

    confirmed(
        field_id="401-D1", field_name="Date of Service",
        segment_id="HDR", action="carry", mandatory=True,
        notes=(
            "Date prescription was filled or professional service rendered. "
            "Format: CCYYMMDD (8 digits). Carry unchanged. "
            "VALIDATION: Must be valid calendar date."
        ),
        warn_if_empty=True,
        warn_code="DOS",
        warn_message="Date of Service (401-D1) is required and must be CCYYMMDD.",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# PAT — Patient Segment (01)
# ═══════════════════════════════════════════════════════════════════════════════
PAT_RULES = [

    confirmed(
        field_id="304-C4", field_name="Patient Date of Birth",
        segment_id="PAT", action="carry", mandatory=False,
        notes="CCYYMMDD format. Carry unchanged. VALIDATION: valid calendar date.",
    ),

    confirmed(
        field_id="305-C5", field_name="Patient Gender Code",
        segment_id="PAT", action="carry", mandatory=False,
        notes="0=Unknown, 1=Male, 2=Female. Carry unchanged.",
    ),

    confirmed(
        field_id="310-CA", field_name="Patient First Name",
        segment_id="PAT", action="carry", mandatory=False,
        notes="Carry unchanged. Latin-1 encoding.",
    ),

    confirmed(
        field_id="311-CB", field_name="Patient Last Name",
        segment_id="PAT", action="carry", mandatory=False,
        notes="Carry unchanged. Latin-1 encoding.",
    ),

    derived(
        field_id="384-4X", field_name="Patient Residence",
        segment_id="PAT", action="carry", mandatory=False,
        notes=(
            "Carry unchanged. Critical for LTPAC and COB routing. "
            "01=Home, 03=Nursing Facility, 06=Long-Term Care Facility, "
            "09=Intermediate Care Facility (IDD), 31-33=Assisted Living variants. "
            "BUSINESS RULE: If LTC code (03/06/09/31/32/33), PST (147-U7) should be '05'."
        ),
    ),

    derived(
        field_id="357-NV", field_name="Patient ID Count",
        segment_id="PAT", action="add", mandatory=False,
        default_value="",
        notes=(
            "New in F6. Supports up to 9 patient identifier entries. "
            "Use 331-CX (qualifier) and 332-CY (value) per occurrence."
        ),
    ),

    confirmed(
        field_id="331-CX", field_name="Patient ID Qualifier",
        segment_id="PAT", action="carry", mandatory=False,
        notes=(
            "01=SSN, 09=Medicare Beneficiary ID, 15=NCPDP UPI, "
            "16=LexID UPI, 18=Evernorth UPI. Max 9 occurrences."
        ),
    ),

    confirmed(
        field_id="332-CY", field_name="Patient ID",
        segment_id="PAT", action="carry", mandatory=False,
        notes="Patient identifier value corresponding to 331-CX qualifier.",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# INS — Insurance Segment (04)
# ═══════════════════════════════════════════════════════════════════════════════
INS_RULES = [

    confirmed(
        field_id="302-C2", field_name="Cardholder ID",
        segment_id="INS", action="carry", mandatory=True,
        notes="Member insurance ID. Carry unchanged. Repeats per occurrence (max 3).",
    ),

    confirmed(
        field_id="301-C1", field_name="Group ID",
        segment_id="INS", action="carry", mandatory=False,
        notes="Insurance group number. Carry unchanged.",
    ),

    confirmed(
        field_id="306-C6", field_name="Patient Relationship Code",
        segment_id="INS", action="carry", mandatory=True,
        notes="01=Cardholder, 02=Spouse, 03=Dependent, 04=Other. Carry unchanged.",
    ),

    confirmed(
        field_id="308-C8", field_name="Other Coverage Code",
        segment_id="INS", action="carry", mandatory=False,
        notes=(
            "0=Not specified, 1=No other coverage, "
            "2=Other coverage exists (payment collected), "
            "3=Other coverage exists (payment not collected). Carry unchanged."
        ),
    ),

    derived(
        field_id="367-2N", field_name="Benefit Network Indicator",
        segment_id="INS", action="add", mandatory=False,
        default_value="",
        notes=(
            "New field in F6. Not present in D.0. "
            "Added as empty field in F6 output. Payer populates on response."
        ),
        warn_if_empty=True,
        warn_code="BNI",
        warn_severity="WARN",
        warn_message=(
            "Benefit Network Indicator (367-2N) is new in F6. "
            "Not present in D.0. Confirm value with payer."
        ),
    ),

    derived(
        field_id="694-ZJ", field_name="Medicare Part D Indicator",
        segment_id="INS", action="carry", mandatory=False,
        notes=(
            "Y=Medicare Part D claim. "
            "BUSINESS RULE: If 694-ZJ='Y', Benefit Stage Qualifier (392-MU) "
            "is required in COB segment. TrOOP tracking requires 393-MV as well."
        ),
    ),

    confirmed(
        field_id="990-MG", field_name="Legacy Routing Code",
        segment_id="INS", action="remove", mandatory=False,
        notes=(
            "DEPRECATED IN F6. Must NOT appear in F6 transactions. "
            "If received on F6: return Reject Code R8 (Syntax Error). "
            "Include '990-MG is not a valid Version F6 field' in 526-FQ. "
            "In diff output display as ~~990-MG=value~~ (strikethrough). "
            "SOURCE: F6 Editorial v02 §3.28.3."
        ),
        warn_code="DEPR_990MG",
        warn_severity="ERROR",
        warn_message=(
            "Field 990-MG (Legacy Routing Code) is deprecated in F6 and must not be "
            "transmitted. Return Reject Code R8."
        ),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# CLM — Claim Segment (07)
# ═══════════════════════════════════════════════════════════════════════════════
CLM_RULES = [

    confirmed(
        field_id="455-EM", field_name="Prescription / Service Reference Number",
        segment_id="CLM", action="carry", mandatory=True,
        notes="Rx number assigned by pharmacy. Carry unchanged.",
    ),

    confirmed(
        field_id="402-D2", field_name="Product / Service ID (NDC)",
        segment_id="CLM", action="transform", mandatory=True,
        notes=(
            "Must be exactly 11 digits with NO hyphens in F6. "
            "D.0 may have hyphens. Transform: remove hyphens. "
            "VALIDATION: exactly 11 numeric digits, no hyphens. "
            "Example: '00071-0155-23' → '00071015523'."
        ),
        transform="REMOVE_HYPHENS",
        warn_if_empty=True,
        warn_code="NDC",
        warn_message="NDC (402-D2) must be exactly 11 digits with no hyphens.",
    ),

    confirmed(
        field_id="403-D3", field_name="Product / Service ID Qualifier",
        segment_id="CLM", action="carry", mandatory=True,
        notes="03=NDC. Carry unchanged.",
    ),

    confirmed(
        field_id="407-D7", field_name="Quantity Dispensed",
        segment_id="CLM", action="carry", mandatory=True,
        notes="Amount dispensed. Carry unchanged.",
    ),

    confirmed(
        field_id="405-D5", field_name="Days Supply",
        segment_id="CLM", action="carry", mandatory=True,
        notes=(
            "Carry unchanged. F6 Editorial §3.7.1: use actual days between dosing. "
            "Do NOT use Days Supply to determine pharmacy type — use 147-U7 instead."
        ),
    ),

    confirmed(
        field_id="406-D6", field_name="Compound Code",
        segment_id="CLM", action="carry", mandatory=True,
        notes=(
            "1=Not compound, 2=Compound (requires CMP segment). "
            "Carry unchanged. BUSINESS RULE: If 2, Compound Segment (Seg 10) is required."
        ),
    ),

    confirmed(
        field_id="408-D8", field_name="DAW / Product Selection Code",
        segment_id="CLM", action="carry", mandatory=True,
        notes="0=No product selection (default). 1-9=specific DAW reason. VALIDATION: 0-9.",
    ),

    confirmed(
        field_id="418-DI", field_name="Quantity Prescribed",
        segment_id="CLM", action="carry", mandatory=True,
        notes=(
            "MANDATORY IN F6 — was optional in D.0. "
            "SOURCE: HHS Final Rule. Distinguishes refills from multiple dispensing "
            "events for a single fill, increasing patient safety."
        ),
        warn_if_empty=True,
        warn_code="QTY_RX",
        warn_severity="ERROR",
        warn_message=(
            "Quantity Prescribed (418-DI) is mandatory in F6 but is missing. "
            "SOURCE: HHS Final Rule."
        ),
    ),

    confirmed(
        field_id="414-DE", field_name="Fill Number",
        segment_id="CLM", action="carry", mandatory=True,
        notes="0=New Rx, 1-99=Refill number. Carry unchanged.",
    ),

    confirmed(
        field_id="419-DJ", field_name="Prescription Origin Code",
        segment_id="CLM", action="carry", mandatory=True,
        notes=(
            "1=Written, 2=Phone, 3=Fax, 4=Electronic, 5=Pharmacy. "
            "New Oct 2025: 6=No Associated Prescription (for claims without a prescriber). "
            "Carry unchanged."
        ),
    ),

    confirmed(
        field_id="409-D9", field_name="Ingredient Cost Submitted",
        segment_id="CLM", action="carry", mandatory=True,
        notes="Drug cost in cents (implied 2 decimal places). Carry unchanged.",
    ),

    confirmed(
        field_id="412-DC", field_name="Dispensing Fee Submitted",
        segment_id="CLM", action="carry", mandatory=True,
        notes="Pharmacy dispensing fee in cents. Carry unchanged.",
    ),

    confirmed(
        field_id="430-DU", field_name="Gross Amount Due",
        segment_id="CLM", action="carry", mandatory=True,
        notes="Total billed = Ingredient Cost + Dispensing Fee + taxes/fees. In cents.",
    ),

    confirmed(
        field_id="423-DN", field_name="Basis of Cost Determination",
        segment_id="CLM", action="carry", mandatory=True,
        notes=(
            "01=AWP, 02=Local Wholesaler, 03=Direct, 04=EAC, 05=Estimated Cost, "
            "06=ASP, 07=AMP, 08=MAC, 09=Other. Carry unchanged."
        ),
    ),

    confirmed(
        field_id="147-U7", field_name="Pharmacy Service Type",
        segment_id="CLM", action="add", mandatory=True,
        default_value="",
        notes=(
            "NEW MANDATORY FIELD IN F6. Not present in D.0. "
            "F6 Editorial §3.7.1: Use this field — NOT Days Supply — to identify pharmacy type. "
            "01=Community/Retail, 02=Compounding, 03=Home Infusion, 04=Institutional/Clinic, "
            "05=LTC, 06=Mail Order, 07=MCO, 08=Specialty (required when SCC=42 or 43). "
            "CASES: SCC=42/43 → PST must be 08. LTC patient residence → PST should be 05."
        ),
        warn_if_empty=True,
        warn_code="PST",
        warn_severity="ERROR",
        warn_message=(
            "Pharmacy Service Type (147-U7) is mandatory in F6 but is missing."
        ),
    ),

    confirmed(
        field_id="420-DK", field_name="Submission Clarification Code",
        segment_id="CLM", action="carry", mandatory=False,
        notes=(
            "Max 5 occurrences (354-NX count field). "
            "Same SCC value CANNOT repeat within the same claim. "
            "42=Specialty validated, 16/21/36=LTC short cycle codes."
        ),
    ),

    confirmed(
        field_id="354-NX", field_name="Submission Clarification Code Count",
        segment_id="CLM", action="carry", mandatory=False,
        notes="Count of SCC occurrences. Max 5. Carry unchanged.",
    ),

    confirmed(
        field_id="424-DO", field_name="Diagnosis Code",
        segment_id="CLM", action="carry", mandatory=False,
        notes="ICD-10 diagnosis code. Carry unchanged. Max 5 (491-VE count field).",
    ),

    confirmed(
        field_id="C90-KH", field_name="LTPAC Billing Methodology",
        segment_id="CLM", action="add", tx_type="LTC", mandatory=False,
        default_value="",
        notes=(
            "NEW IN F6. Identifies billing methodology for LTPAC claims. "
            "Values (from ECL): 1=Full quantity as dispensed on date of dispensing, "
            "2=Post-consumption billing (aggregated monthly), "
            "3=Pre-consumption billing (billed before all dispensings complete). "
            "Only applicable to LTC (Long Term Care) claims — "
            "patient residence must be 03/06/09/31/32/33. "
            "SOURCE: F6 Editorial v02 §3.7.2."
        ),
    ),

    confirmed(
        field_id="C92-KM", field_name="Number of LTPAC Dispensing Events",
        segment_id="CLM", action="add", tx_type="LTC", mandatory=False,
        default_value="",
        notes=(
            "NEW IN F6. Count of dispensing events comprising the claim. "
            "Only applicable to LTC claims. "
            "SOURCE: F6 Editorial v02 §3.7.2."
        ),
    ),

    confirmed(
        field_id="C91-KK", field_name="LTPAC Dispense Frequency",
        segment_id="CLM", action="add", tx_type="LTC", mandatory=False,
        default_value="",
        notes=(
            "NEW IN F6. Typical interval pattern of dispensing or resupply. "
            "Only applicable to LTC claims. "
            "SOURCE: F6 Editorial v02 §3.7.2."
        ),
    ),

    confirmed(
        field_id="995-E2", field_name="Route of Administration",
        segment_id="CLM", action="carry", mandatory=False,
        notes=(
            "SNOMED CT values from NLM VSAC (Value Set Authority Center), "
            "published biannually. Do NOT use a static hardcoded list. "
            "SOURCE: F6 Editorial v02 §3.7.3 and Appendix B."
        ),
    ),

    inferred(
        field_id="996-E3", field_name="Level of Service",
        segment_id="CLM", action="carry", mandatory=False,
        notes="Carry unchanged if present.",
    ),

    confirmed(
        field_id="B98-34", field_name="Reconciliation ID",
        segment_id="CLM", action="carry", mandatory=False,
        notes=(
            "NEW IN F6. Returned by payer on paid claim responses. "
            "REQUIRED on reversal (B2) transactions. "
            "If not received from payer, submit 'NOTAVAILABLE'. "
            "Payers MUST accept 'NOTAVAILABLE' — do NOT reject with DB1 or DH6. "
            "SOURCE: F6 Editorial v02 §3.28.2."
        ),
    ),

    inferred(
        field_id="461-EU", field_name="Prior Authorization Number Qualifier",
        segment_id="CLM", action="carry", mandatory=False,
        notes=(
            "Qualifier for the Prior Authorization Number (462-EV). "
            "Present on specialty (SCC=42/43) and prior auth claims. Carry unchanged."
        ),
    ),

    inferred(
        field_id="462-EV", field_name="Prior Authorization Number",
        segment_id="CLM", action="carry", mandatory=False,
        notes=(
            "Prior Authorization Number returned by payer. "
            "In F6 may appear in CLM or in the PA segment (498-GN). "
            "Required when SCC=42/43 for specialty claims. Carry unchanged."
        ),
    ),

    inferred(
        field_id="994-E1", field_name="Medicare Part D Coverage Code",
        segment_id="CLM", action="add", tx_type="MEDICARE_PART_D", mandatory=False,
        default_value="",
        notes=(
            "New in F6 CLM segment for Medicare Part D claims. "
            "[INFERRED] Sourced from rules/08_medicare_part_d.json — "
            "verify against full NCPDP Implementation Guide when available."
        ),
        warn_if_empty=True,
        warn_code="MPD",
        warn_severity="WARN",
        warn_message=(
            "Medicare Part D Coverage Code (994-E1) required for Part D claims in F6."
        ),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# PRE — Prescriber Segment (03)
# ═══════════════════════════════════════════════════════════════════════════════
PRE_RULES = [

    confirmed(
        field_id="411-DB", field_name="Prescriber NPI",
        segment_id="PRE", action="carry", mandatory=True,
        notes=(
            "10-digit NPI. Mandatory in F6. VALIDATION: exactly 10 numeric digits. "
            "For no-prescriber claims (Oct 2025+): use qualifier 18 with value '0'. "
            "SOURCE: F6 Editorial v02 §3.9.1."
        ),
        warn_if_empty=True,
        warn_code="NPI",
        warn_message="Prescriber NPI (411-DB) must be 10 digits and is required in F6.",
    ),

    confirmed(
        field_id="466-EZ", field_name="Prescriber ID Qualifier",
        segment_id="PRE", action="carry", mandatory=True,
        notes=(
            "01=NPI, 12=DEA Number, 13=State License. "
            "NEW Oct 2025: 18=No Prescriber ID / No Prescription Associated. "
            "When 18: 411-DB must be '0' and 419-DJ must be '6'. "
            "Payer may reject DO6 (Prescription Required). "
            "SOURCE: F6 Editorial v02 §3.9.1."
        ),
    ),

    confirmed(
        field_id="427-DR", field_name="Prescriber Last Name",
        segment_id="PRE", action="carry", mandatory=False,
        notes="Carry unchanged.",
    ),

    derived(
        field_id="364-2J", field_name="Prescriber First Name",
        segment_id="PRE", action="add", mandatory=False,
        default_value="",
        notes="Prescriber first name. New/expanded in F6. Carry if present in D.0.",
    ),

    confirmed(
        field_id="464-EX", field_name="Prescriber DEA Number",
        segment_id="PRE", action="carry", mandatory=False,
        notes=(
            "Format: 2 letters + 7 digits (e.g. AB1234567). "
            "VALIDATION: regex [A-Z]{2}\\d{7}. Carry unchanged. "
            "CORRECTED: field ID was previously misassigned as 441-E6, "
            "which is actually DUR Result of Service Code."
        ),
    ),

    inferred(
        field_id="835-5C", field_name="Prescriber Specialty",
        segment_id="PRE", action="add", mandatory=False,
        default_value="",
        notes=(
            "New in F6; not present in D.0. "
            "Optional — append to PRE segment if value is available. "
            "[INFERRED] Sourced from rules/01_retail.json — "
            "verify against full NCPDP Implementation Guide when available."
        ),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# PRI — Pricing Segment (11)
# ═══════════════════════════════════════════════════════════════════════════════
PRI_RULES = [

    inferred(
        field_id="426-DQ", field_name="Usual & Customary Charge",
        segment_id="PRI", action="carry", mandatory=False,
        notes="Carry unchanged.",
    ),

    confirmed(
        field_id="478-H7", field_name="Basis of Reimbursement",
        segment_id="PRI", action="cases",
        mandatory=False,
        notes=(
            "NEW in F6 PRI segment. Maps from D.0 CLM field 423-DN "
            "(Basis of Cost Determination). Codes are identical — direct map. "
            "If 423-DN is present and non-empty, copy the value. "
            "If 423-DN is absent, emit WARN BRD. "
            "SOURCE: NCPDP F6 Editorial v02 §3.11 PRI segment field reference."
        ),
        warn_if_empty=True,
        warn_code="BRD",
        warn_severity="WARN",
        warn_message=(
            "Basis of Reimbursement (478-H7) is empty. "
            "Could not map from 423-DN (Basis of Cost Determination) — "
            "field was absent in the source D.0 transaction."
        ),
        cases=[
            {
                "when": {
                    "operator": "present_and_nonempty",
                    "field": "CLM.423-DN",
                },
                "then": {
                    "action": "transform",
                    "transform": "COPY_FROM_FIELD",
                    "source_field": "CLM.423-DN",
                },
            },
            {
                "when": "default",
                "then": {
                    "action": "add",
                    "default_value": "",
                },
            },
        ],
    ),

    confirmed(
        field_id="479-H8", field_name="Other Amount Claimed Submitted Qualifier",
        segment_id="PRI", action="carry", mandatory=False,
        notes="Qualifier for 478-H7. Cannot repeat with same value. Max 3.",
    ),

    inferred(
        field_id="433-DX", field_name="Patient Paid Amount Submitted",
        segment_id="PRI", action="carry", mandatory=False,
        notes="Amount the patient paid. In cents. Carry unchanged.",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# DUR — Drug Utilization Review / PPS Segment (08)
# ═══════════════════════════════════════════════════════════════════════════════
DUR_RULES = [

    confirmed(
        field_id="473-7E", field_name="DUR/PPS Code Counter",
        segment_id="DUR", action="carry", mandatory=False,
        notes=(
            "Number of DUR conflict occurrences. Max 9. "
            "CONFIRMED: If > 9 conflicts exist, set 9th repetition 439-E4='CH' (Call Help Desk). "
            "SOURCE: F6 Editorial v02 §3.32.2."
        ),
    ),

    confirmed(
        field_id="439-E4", field_name="Reason for Service Code",
        segment_id="DUR", action="carry", mandatory=False,
        notes=(
            "DUR conflict type. DD=Drug-Drug, DC=Drug-Contraindication, DA=Drug Allergy, "
            "HD=High Dose, RF=Refill Too Soon. "
            "NEW in F6: MD=Medication Delivery Device Method Required (insulin delivery). "
            "CH=Call Help Desk (when > 9 alerts). Return in order of Clinical Significance."
        ),
    ),

    confirmed(
        field_id="440-E5", field_name="Professional Service Code",
        segment_id="DUR", action="carry", mandatory=False,
        notes=(
            "Pharmacist action taken. "
            "NEW in F6: UA=Unable to Confirm Medication Delivery Device Method "
            "and Patient Requires Immediate Access. Carry unchanged."
        ),
    ),

    confirmed(
        field_id="441-E6", field_name="Result of Service Code",
        segment_id="DUR", action="carry", mandatory=False,
        notes=(
            "Outcome of pharmacist action. "
            "NEW in F6 (insulin delivery): 5A=DME Pump, 5B=Non-DME/Disposable, "
            "5C=Unknown Delivery Device/Immediate Access. Carry unchanged."
        ),
    ),

    confirmed(
        field_id="544-FY", field_name="DUR/DUE Free Text Message",
        segment_id="DUR", action="carry", mandatory=False,
        notes=(
            "Max 9 repetitions x 360 bytes each. "
            "Must NOT be sent without Reason for Service Code (439-E4). "
            "SOURCE: F6 Editorial v02 §3.32.1."
        ),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# COB — Coordination of Benefits / Other Payments Segment (05)
# ═══════════════════════════════════════════════════════════════════════════════
COB_RULES = [

    confirmed(
        field_id="337-4C", field_name="Coordination of Benefits / Other Payments Count",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "Number of other payer occurrences. Max 3. "
            "Other Payer Coverage Type (338-5C) should NOT repeat within a single claim. "
            "SOURCE: F6 Editorial v02 §3.3 ECL table."
        ),
    ),

    confirmed(
        field_id="338-5C", field_name="Other Payer Coverage Type",
        segment_id="COB", action="carry", mandatory=True,
        notes="01=Primary, 02=Secondary, 03=Tertiary. Should NOT repeat. Carry unchanged.",
    ),

    confirmed(
        field_id="339-6C", field_name="Other Payer ID Qualifier",
        segment_id="COB", action="carry", mandatory=True,
        notes="03=BIN/IIN, 10=Payer Name. Can repeat within COB count. Carry unchanged.",
    ),

    confirmed(
        field_id="340-7C", field_name="Other Payer ID",
        segment_id="COB", action="carry", mandatory=False,
        notes="Other payer BIN/IIN or name (max 30 chars). When only name: 339-6C=10.",
    ),

    confirmed(
        field_id="443-E8", field_name="Other Payer Date",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "Payment or denial date from other payer. "
            "CONFIRMED: Does NOT have to match Date of Service (401-D1). "
            "If coverage identified after DOS, Other Payer Date will differ. "
            "SOURCE: F6 Editorial v02 §3.10.2."
        ),
    ),

    confirmed(
        field_id="341-HB", field_name="Other Payer Amount Paid Count",
        segment_id="COB", action="carry", mandatory=False,
        notes="Number of OPAP occurrences. Max 9. Carry unchanged.",
    ),

    confirmed(
        field_id="342-HC", field_name="Other Payer Amount Paid Qualifier",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "CONFIRMED OPAP reporting order (F6 Editorial v02 §3.10.4): "
            "1→12 (Regulatory Fee), 2→15 (Percentage Tax), 3→09 (Compound Prep), "
            "4→01 (Delivery), 5→02 (Shipping), 6→03 (Postage), 7→04 (Administrative), "
            "8→16 (Medication Synchronization), 9→17 (Adherence Packaging), "
            "10→05 (Incentive), 11→07 (Drug Benefit — END RESULT always required). "
            "Cannot repeat with same qualifier value."
        ),
    ),

    confirmed(
        field_id="431-DV", field_name="Other Payer Amount Paid",
        segment_id="COB", action="carry", mandatory=False,
        notes="Dollar amount paid by other payer. In cents. Carry unchanged.",
    ),

    confirmed(
        field_id="C47-9T", field_name="Other Payer Adjudicated Program Type",
        segment_id="COB", action="add", mandatory=False,
        default_value="",
        notes=(
            "CONFIRMED: Mandatory when COB/Other Payments Segment is present in the transaction. "
            "Populate from prior payer's Adjudicated Program Type (A28-ZR) response. "
            "Cannot be known at conversion time without the prior payer's response — "
            "add as empty field, emit WARN so LLM or user can fill. "
            "If downstream payer rejects with DI8, the program type is unsupported. "
            "SOURCE: F6 Editorial v02 §3.10.1."
        ),
        warn_if_empty=True,
        warn_code="APT",
        warn_severity="WARN",
        warn_message=(
            "Other Payer Adjudicated Program Type (C47-9T) is required when COB segment "
            "is present but cannot be determined from the D.0 input alone. "
            "Populate with the Adjudicated Program Type (A28-ZR) value returned by "
            "the prior payer in their response. "
            "Common values: 1=Medicare Part D, 2=Medicaid, 3=Commercial, 4=Medicare Part B."
        ),
    ),

    confirmed(
        field_id="D51-P7", field_name="Other Payer Percentage Tax Exempt Indicator",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "CONFIRMED mapping from response field 557-AV (NOT a direct copy): "
            "557-AV=1 (Plan Tax Exempt) → D51-P7=1. "
            "557-AV=5 (Religious Org) → D51-P7=2. "
            "557-AV=6 (Tax Exempt Certificate) → D51-P7=3. "
            "557-AV=7 (Previous Payer Exempt) → D51-P7=4. "
            "SOURCE: F6 Editorial v02 §3.10.3."
        ),
    ),

    confirmed(
        field_id="D52-P8", field_name="Other Payer Regulatory Fee Exempt Indicator",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "CONFIRMED: Direct map from response field D62-RM. "
            "D62-RM=1 → D52-P8=1 (Plan Exempt), D62-RM=2 → 2 (Religious Org), "
            "D62-RM=3 → 3 (Certificate), D62-RM=4 → 4 (Previous Payer Exempt). "
            "SOURCE: F6 Editorial v02 §3.10.3."
        ),
    ),

    confirmed(
        field_id="C50-9W", field_name="Benefit Stage Indicator Count",
        segment_id="COB", action="carry", mandatory=False,
        notes="Max 4 benefit stage occurrences. Carry unchanged.",
    ),

    confirmed(
        field_id="C51-9X", field_name="Benefit Stage Indicator",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "Must NOT repeat within same iteration. One value per iteration. "
            "Value 50=Paid under Part B benefit of Medicare health plan. "
            "Value 51=Paid under Part B for QMB dual eligible "
            "(do not collect cost-share; bill COB to Medicaid). "
            "Applies to MA/MAPD AND Medicare Part B FFS. "
            "SOURCE: F6 Editorial v02 §3.31.3."
        ),
    ),

    confirmed(
        field_id="392-MU", field_name="Benefit Stage Qualifier",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "Required when Medicare Part D Indicator (694-ZJ)='Y'. "
            "Used for Part D TrOOP accumulation tracking."
        ),
    ),

    confirmed(
        field_id="393-MV", field_name="Benefit Stage Amount",
        segment_id="COB", action="carry", mandatory=False,
        notes="Required when Medicare Part D Indicator (694-ZJ)='Y'. TrOOP amount.",
    ),

    confirmed(
        field_id="C49-9V", field_name="Other Payer Reconciliation ID",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "Reconciliation ID from prior payer's response. "
            "Submit 'NOTAVAILABLE' if not received. "
            "Payer may reject DH6 if required and absent. "
            "CONFIRMED: Do NOT use DH6 when 'NOTAVAILABLE' is submitted. "
            "Do NOT report C49-9V when COB occurrence shows rejected prior payer response "
            "(when Other Payer Reject Code 472-6E is populated). "
            "SOURCE: F6 Editorial v02 §§3.28.2, 2.1."
        ),
    ),

    confirmed(
        field_id="471-5E", field_name="Other Payer Reject Count",
        segment_id="COB", action="carry", mandatory=False,
        notes="Max 5 rejection codes from prior payer. Carry unchanged.",
    ),

    confirmed(
        field_id="472-6E", field_name="Other Payer Reject Code",
        segment_id="COB", action="carry", mandatory=False,
        notes=(
            "Rejection code from prior payer. Can repeat with same value. "
            "When populated: do NOT include C49-9V (Other Payer Reconciliation ID). "
            "SOURCE: F6 Editorial v02 §2.1."
        ),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# CMP — Compound Segment (10) — max 25 ingredient occurrences
# ═══════════════════════════════════════════════════════════════════════════════
CMP_RULES = [

    confirmed(
        field_id="447-EC", field_name="Compound Ingredient Component Count",
        segment_id="CMP", action="carry", mandatory=True, tx_type="COMPOUND",
        notes=(
            "Number of compound ingredients. Max 25. "
            "CONFIRMED field order (F6 Editorial v02 §3.2): "
            "Must come AFTER 450-EF (Dosage Form) and 451-EG (Unit Form Indicator)."
        ),
    ),

    confirmed(
        field_id="450-EF", field_name="Compound Dosage Form Description Code",
        segment_id="CMP", action="carry", mandatory=True, tx_type="COMPOUND",
        notes="Required per F6 field ordering in Compound segment. Carry unchanged.",
    ),

    confirmed(
        field_id="451-EG", field_name="Compound Dispensing Unit Form Indicator",
        segment_id="CMP", action="carry", mandatory=True, tx_type="COMPOUND",
        notes="Required per F6 field ordering in Compound segment. Carry unchanged.",
    ),

    confirmed(
        field_id="488-RE", field_name="Compound Product ID Qualifier",
        segment_id="CMP", action="carry", mandatory=True, tx_type="COMPOUND",
        notes="Per ingredient. Same qualifier can repeat across different ingredients.",
    ),

    confirmed(
        field_id="489-TE", field_name="Compound Product ID (NDC)",
        segment_id="CMP", action="carry", mandatory=True, tx_type="COMPOUND",
        notes="NDC per ingredient. Carry unchanged. Max 25 repetitions.",
    ),

    confirmed(
        field_id="448-ED", field_name="Compound Ingredient Quantity",
        segment_id="CMP", action="carry", mandatory=True, tx_type="COMPOUND",
        notes="Quantity per ingredient. Carry unchanged. Max 25 repetitions.",
    ),

    inferred(
        field_id="449-EE", field_name="Compound Ingredient Drug Cost",
        segment_id="CMP", action="carry", mandatory=False, tx_type="COMPOUND",
        notes="Cost per ingredient. Carry unchanged. Max 25 repetitions.",
    ),

    inferred(
        field_id="490-UE", field_name="Compound Ingredient Basis of Cost Determination",
        segment_id="CMP", action="carry", mandatory=False, tx_type="COMPOUND",
        notes="BOC per ingredient. Carry unchanged.",
    ),

    confirmed(
        field_id="362-2G", field_name="Compound Ingredient Modifier Code Count",
        segment_id="CMP", action="carry", mandatory=False, tx_type="COMPOUND",
        notes="Max 10 modifier codes per ingredient. Can repeat with same value.",
    ),

    confirmed(
        field_id="363-2H", field_name="Compound Ingredient Modifier Code",
        segment_id="CMP", action="carry", mandatory=False, tx_type="COMPOUND",
        notes="Modifier code per ingredient. Max 10. Carry unchanged.",
    ),

    inferred(
        field_id="C60-AG", field_name="Compound Level of Complexity",
        segment_id="CMP", action="carry", mandatory=False, tx_type="COMPOUND",
        notes="Compound complexity level. Carry unchanged.",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# REVERSAL — Reversal-specific rules (B2 transactions)
# ═══════════════════════════════════════════════════════════════════════════════
REVERSAL_RULES = [

    confirmed(
        field_id="B98-34", field_name="Reconciliation ID (Reversal)",
        segment_id="CLM", action="carry", mandatory=True, tx_type="REVERSAL",
        notes=(
            "REQUIRED on B2 (reversal) transactions in the Claim Segment. "
            "Submit 'NOTAVAILABLE' if Reconciliation ID was not received on the original paid claim. "
            "Payer should reject with DB1 if absent and 'NOTAVAILABLE' not submitted. "
            "Used with Service Provider ID (201-B1) for transaction matching. "
            "SOURCE: F6 Editorial v02 §3.28.2."
        ),
        warn_if_empty=True,
        warn_code="RECON_ID",
        warn_severity="ERROR",
        warn_message=(
            "Reconciliation ID (B98-34) is required on reversal (B2) transactions. "
            "Submit 'NOTAVAILABLE' if not received on original paid claim."
        ),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL — Cross-segment business rules
# stored with segment_id="GLOBAL", transaction_type="ALL"
# ═══════════════════════════════════════════════════════════════════════════════
GLOBAL_RULES = [

    confirmed(
        field_id="COMPOUND_CMP_REQUIRED",
        field_name="Compound Claim — CMP Segment Required",
        segment_id="GLOBAL", action="cases",
        notes=(
            "BUSINESS RULE: If Compound Code (406-D6)='2', "
            "Compound Segment (Seg 10/CMP) MUST be present."
        ),
        cases=[
            {
                "when": {"field": "CLM.406-D6", "operator": "eq", "value": "2"},
                "then": {
                    "action": "validate",
                    "check": "segment_present",
                    "segment": "CMP",
                    "error_code": "COMPOUND_CMP",
                    "error_message":
                        "Compound Code=2 but Compound Segment (CMP) is missing.",
                },
            }
        ],
    ),

    confirmed(
        field_id="LTC_PST_VALIDATION",
        field_name="LTC Patient — Pharmacy Service Type Check",
        segment_id="GLOBAL", action="cases",
        notes=(
            "CONFIRMED (F6 Editorial §3.7.1): "
            "If Patient Residence (384-4X) is an LTC code (03/06/09/31/32/33), "
            "Pharmacy Service Type (147-U7) should be '05' (LTC)."
        ),
        cases=[
            {
                "when": {
                    "operator": "in",
                    "field": "PAT.384-4X",
                    "value": ["03", "06", "09", "31", "32", "33"],
                },
                "then": {
                    "action": "validate_field",
                    "field": "CLM.147-U7",
                    "expected": "05",
                    "warn_code": "LTC_PST",
                    "warn_severity": "WARN",
                    "warn_message": (
                        "Patient Residence indicates LTC setting but "
                        "Pharmacy Service Type (147-U7) is not '05' (LTC)."
                    ),
                },
            }
        ],
    ),

    confirmed(
        field_id="SPECIALTY_SCC_PST",
        field_name="Specialty Claim — PST Must Be 08",
        segment_id="GLOBAL", action="cases",
        notes=(
            "If SCC (420-DK)=42 or 43, "
            "Pharmacy Service Type (147-U7) should be '08' (Specialty Pharmacy)."
        ),
        cases=[
            {
                "when": {
                    "operator": "in",
                    "field": "CLM.420-DK",
                    "value": ["42", "43"],
                },
                "then": {
                    "action": "validate_field",
                    "field": "CLM.147-U7",
                    "expected": "08",
                    "warn_code": "SPEC_PST",
                    "warn_severity": "WARN",
                    "warn_message": (
                        "SCC=42/43 (specialty) but Pharmacy Service Type "
                        "is not '08' (Specialty Pharmacy)."
                    ),
                },
            }
        ],
    ),

    confirmed(
        field_id="PART_D_BENEFIT_STAGE",
        field_name="Medicare Part D — Benefit Stage Required",
        segment_id="GLOBAL", action="cases",
        notes=(
            "CONFIRMED: If Medicare Part D Indicator (694-ZJ)='Y', "
            "Benefit Stage Qualifier (392-MU) and Benefit Stage Amount (393-MV) "
            "must be present in COB segment. Required for TrOOP tracking."
        ),
        cases=[
            {
                "when": {"field": "INS.694-ZJ", "operator": "eq", "value": "Y"},
                "then": {
                    "action": "validate",
                    "check": "fields_present",
                    "fields": ["COB.392-MU", "COB.393-MV"],
                    "error_code": "PART_D_BSTAGE",
                    "error_message": (
                        "Medicare Part D Indicator=Y but Benefit Stage Qualifier (392-MU) "
                        "or Benefit Stage Amount (393-MV) is missing from COB segment."
                    ),
                },
            }
        ],
    ),

    confirmed(
        field_id="DUR_FREE_TEXT_REQUIRES_RSC",
        field_name="DUR Free Text — Reason for Service Code Required",
        segment_id="GLOBAL", action="cases",
        notes=(
            "CONFIRMED: DUR/DUE Free Text Message (544-FY) must NOT be sent "
            "without Reason for Service Code (439-E4). "
            "SOURCE: F6 Editorial v02 §3.32.1."
        ),
        cases=[
            {
                "when": {"field": "DUR.544-FY", "operator": "present"},
                "then": {
                    "action": "validate",
                    "check": "field_present",
                    "field": "DUR.439-E4",
                    "error_code": "DUR_RSC",
                    "error_message": (
                        "DUR Free Text (544-FY) submitted without "
                        "Reason for Service Code (439-E4)."
                    ),
                },
            }
        ],
    ),

    confirmed(
        field_id="NO_PRESCRIBER_ID_ORIGIN_CODE",
        field_name="No Prescriber ID — Prescription Origin Must Be 6",
        segment_id="GLOBAL", action="cases",
        notes=(
            "CONFIRMED (F6 Editorial v02 §3.9.1, Oct 2025): "
            "When Prescriber ID Qualifier (466-EZ)='18' (No Prescriber), "
            "Prescription Origin Code (419-DJ) must be '6' "
            "(No Associated Prescription)."
        ),
        cases=[
            {
                "when": {"field": "PRE.466-EZ", "operator": "eq", "value": "18"},
                "then": {
                    "action": "validate_field",
                    "field": "CLM.419-DJ",
                    "expected": "6",
                    "warn_code": "NO_RX_ORIGIN",
                    "warn_severity": "ERROR",
                    "warn_message": (
                        "Prescriber ID Qualifier=18 (No Prescriber) but "
                        "Prescription Origin Code is not '6' (No Associated Prescription)."
                    ),
                },
            }
        ],
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Master rule list
# ═══════════════════════════════════════════════════════════════════════════════
ALL_RULES: list[dict] = (
    HDR_RULES
    + PAT_RULES
    + INS_RULES
    + CLM_RULES
    + PRE_RULES
    + PRI_RULES
    + DUR_RULES
    + COB_RULES
    + CMP_RULES
    + REVERSAL_RULES
    + GLOBAL_RULES
)


# ═══════════════════════════════════════════════════════════════════════════════
# Transaction type expansion
#
# Most rule blocks above (HDR/PAT/INS/CLM/PRE/PRI/DUR/COB/GLOBAL) leave
# tx_type at its "ALL" default because they apply to every transaction type.
# db_ops.list_rules() filters with a strict `transaction_type = ?` equality
# check (no "ALL" fallback), so a rule literally stored with
# transaction_type="ALL" is invisible to any concrete-type query such as
# transaction_type="LTC" or "MEDICARE_PART_D". Expand each "ALL" rule into
# one concrete row per transaction type so the validator can find it.
# ═══════════════════════════════════════════════════════════════════════════════
TRANSACTION_TYPES = [
    "RETAIL", "SPECIALTY", "CONTROLLED", "COB", "REVERSAL",
    "COMPOUND", "LTC", "MEDICARE_PART_D", "ELIGIBILITY", "PRIOR_AUTH",
]

# Per-transaction-type mandatory field overrides.
#
# Base rules are authored for the general billing case (RETAIL, SPECIALTY,
# CONTROLLED, COB, COMPOUND, LTC, MEDICARE_PART_D, PRIOR_AUTH) where a full
# claim with pricing, dispensing, and prescribing data is present.
#
# REVERSAL (B2) identifies a previously adjudicated claim to reverse — it
# carries only the fields needed to match the original claim; it must NOT
# require new pricing, dispensing, or prescribing data.
#
# ELIGIBILITY (E1) verifies coverage eligibility before a claim is written —
# it has no claim-specific data to carry at all.
#
# Format: { transaction_type: { segment_id: frozenset(field_ids forced non-mandatory) } }
#
# Fields that intentionally REMAIN mandatory on REVERSAL (not in override set):
#   402-D2  NDC               — identifies which drug claim to reverse
#   414-DE  Fill Number       — identifies which fill to reverse
#   455-EM  Prescription Ref# — identifies which prescription to reverse
#   B98-34  Reconciliation ID — required per F6 Editorial; NOTAVAILABLE fallback
MANDATORY_OVERRIDES: dict[str, dict[str, frozenset]] = {
    "REVERSAL": {
        "CLM": frozenset({
            "147-U7", "403-D3", "405-D5", "406-D6", "407-D7", "408-D8",
            "409-D9", "412-DC", "418-DI", "419-DJ", "423-DN", "430-DU",
        }),
        # Fix 3: 466-EZ is mandatory for billing types but not on a reversal
        # (no prescriber context needed to reverse a claim by reference number).
        "PRE": frozenset({"466-EZ"}),
        # Fix 4: 367-2N/694-ZJ have warn_if_empty for billing types; suppress
        # on reversals where network indicator context isn't available.
        "INS": frozenset({"367-2N", "694-ZJ"}),
    },
    "ELIGIBILITY": {
        "CLM": frozenset({
            "147-U7", "402-D2", "403-D3", "405-D5", "406-D6", "407-D7",
            "408-D8", "409-D9", "412-DC", "414-DE", "418-DI", "419-DJ",
            "423-DN", "430-DU", "455-EM",
        }),
        # Fix 3: 466-EZ doesn't apply to eligibility-only transactions.
        "PRE": frozenset({"466-EZ"}),
        # Fix 4: no network indicator context on a coverage eligibility check.
        "INS": frozenset({"367-2N", "694-ZJ"}),
    },
    # Fix 2: PST (147-U7) cannot be deterministically derived for prior auth
    # requests — the accompanying billing claim type isn't known here.
    # LLM resolver will infer from context if needed.
    "PRIOR_AUTH": {
        "CLM": frozenset({"147-U7"}),
    },
}

# Per-transaction-type warn-text overrides.
#
# A handful of "ALL" rules carry a tx-specific warn_message in their
# pre-DB rules/*.json form (e.g. CONTROLLED stresses traceability while
# LTC/MEDICARE_PART_D/PRIOR_AUTH use a terse "empty" message). The base
# rule below carries no warn text at all, so every tx type expanded from
# it would otherwise share one undifferentiated (or empty) message.
# REVERSAL, ELIGIBILITY, and COMPOUND have no dedicated entry — they fall
# back to RETAIL's text, same as a JSON file that doesn't redeclare a
# field inherits it from RETAIL via get_rules_for()'s merge.
#
# Format: { "SEG.field_id": { transaction_type: {warn_code, warn_message} } }
WARN_OVERRIDES: dict[str, dict[str, dict]] = {
    "PRE.364-2J": {
        "RETAIL": {
            "warn_code": "PF",
            "warn_message": "Prescriber First Name (364-2J) is absent. Recommended for traceability; confirm supplier source.",
        },
        "CONTROLLED": {
            "warn_code": "PF",
            "warn_message": "Prescriber First Name (364-2J) is new in F6. Required for controlled substance traceability.",
        },
        "SPECIALTY": {
            "warn_code": "PF",
            "warn_message": "Prescriber First Name (364-2J) is empty. Confirm supplier source for traceability.",
        },
        "COB": {
            "warn_code": "PF",
            "warn_message": "Prescriber First Name (364-2J) is empty. Confirm supplier source for traceability.",
        },
        "LTC": {
            "warn_code": "PFN",
            "warn_message": "Prescriber First Name (364-2J) empty.",
        },
        "MEDICARE_PART_D": {
            "warn_code": "PFN",
            "warn_message": "Prescriber First Name (364-2J) empty.",
        },
        "PRIOR_AUTH": {
            "warn_code": "PFN",
            "warn_message": "Prescriber First Name (364-2J) empty.",
        },
    },
}


def _expand_rules(rules: list[dict]) -> list[dict]:
    """Expand rules with transaction_type='ALL' into one row per concrete type."""
    expanded: list[dict] = []
    for r in rules:
        if r.get("transaction_type") == "ALL":
            for tx_type in TRANSACTION_TYPES:
                row = dict(r)
                row["transaction_type"] = tx_type

                seg_id   = row.get("segment_id", "")
                field_id = row.get("field_id", "")
                overridden_fields = MANDATORY_OVERRIDES.get(tx_type, {}).get(seg_id, frozenset())

                if field_id in overridden_fields:
                    row["mandatory_f6"]  = False
                    row["warn_if_empty"] = False
                    row["notes"] = (
                        row.get("notes", "")
                        + f" [OVERRIDE: not mandatory for {tx_type} —"
                          f" field does not apply to this transaction type]"
                    )
                    print(
                        f"[SEEDER] override: {seg_id:8s} {field_id:10s} "
                        f"mandatory_f6=False for {tx_type}"
                    )

                warn_overrides_for_field = WARN_OVERRIDES.get(f"{seg_id}.{field_id}")
                if warn_overrides_for_field is not None:
                    warn = warn_overrides_for_field.get(tx_type, warn_overrides_for_field.get("RETAIL", {}))
                    if warn:
                        row["warn_if_empty"] = True
                        row["warn_severity"] = "WARN"
                        row["warn_code"]     = warn["warn_code"]
                        row["warn_message"]  = warn["warn_message"]

                expanded.append(row)
            print(
                f"[SEEDER] expand:   {r.get('segment_id'):8s} {r.get('field_id'):10s} "
                f"ALL -> {len(TRANSACTION_TYPES)} types"
            )
        else:
            expanded.append(dict(r))
    return expanded


# ═══════════════════════════════════════════════════════════════════════════════
# Seeder
# ═══════════════════════════════════════════════════════════════════════════════

def _existing_rule_set_id() -> str | None:
    """Return the existing rule set ID if it already exists, else None."""
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM rule_sets WHERE name=%s", (RULE_SET_NAME,)
        ).fetchone()
        return row['id'] if row else None


def seed(activate: bool = True, force: bool = False) -> str:
    """
    Seed the F6 standards rule set into the database.

    Args:
        activate: If True, mark this rule set as the active one after seeding.
        force:    If True, delete and re-seed even if the rule set already exists.

    Returns:
        The rule set ID (existing or newly created).
    """
    existing_id = _existing_rule_set_id()

    if existing_id and not force:
        print(
            f"[SEEDER] Rule set '{RULE_SET_NAME}' already exists (id={existing_id}). "
            "Skipping. Pass --force to re-seed."
        )
        return existing_id

    if existing_id and force:
        with db() as conn:
            conn.execute("DELETE FROM rules WHERE rule_set_id=%s", (existing_id,))
            conn.execute("DELETE FROM rule_sets WHERE id=%s", (existing_id,))
        print(f"[SEEDER] Deleted existing rule set {existing_id} (--force).")

    rsid = db_ops.create_rule_set(
        name=RULE_SET_NAME,
        description=RULE_SET_DESC,
        version=RULE_SET_VERSION,
    )
    print(f"[SEEDER] Created rule set: {rsid}")

    expanded_rules = _expand_rules(ALL_RULES)

    db_ops.insert_rules_bulk(rsid, expanded_rules)
    print(f"[SEEDER] Inserted {len(expanded_rules)} rules ({len(ALL_RULES)} before tx-type expansion).")

    if activate:
        db_ops.activate_rule_set(rsid)
        print(f"[SEEDER] Activated rule set '{RULE_SET_NAME}'.")

    # Print breakdown by transaction type
    tx_counts: dict[str, int] = {}
    for r in expanded_rules:
        tx = r.get("transaction_type", "?")
        tx_counts[tx] = tx_counts.get(tx, 0) + 1
    print("[SEEDER] Rules by transaction type:")
    for tx, count in sorted(tx_counts.items()):
        print(f"         {tx:18s} {count:3d} rules")

    # Print breakdown by segment
    seg_counts: dict[str, int] = {}
    for r in expanded_rules:
        seg = r.get("segment_id", "?")
        seg_counts[seg] = seg_counts.get(seg, 0) + 1
    print("[SEEDER] Rules by segment:")
    for seg, count in sorted(seg_counts.items()):
        print(f"         {seg:10s} {count:3d} rules")

    # Print breakdown by confidence
    conf_counts: dict[str, int] = {}
    for r in expanded_rules:
        notes = r.get("notes", "")
        if notes.startswith("[CONFIRMED]"):
            conf_counts["CONFIRMED"] = conf_counts.get("CONFIRMED", 0) + 1
        elif notes.startswith("[DERIVED]"):
            conf_counts["DERIVED"] = conf_counts.get("DERIVED", 0) + 1
        else:
            conf_counts["INFERRED"] = conf_counts.get("INFERRED", 0) + 1
    print("[SEEDER] Rules by confidence:")
    for conf, count in sorted(conf_counts.items()):
        print(f"         {conf:12s} {count:3d} rules")

    print(f"[SEEDER] Done. Total: {len(expanded_rules)} rules in rule set {rsid}")
    return rsid


def main():
    parser = argparse.ArgumentParser(
        description="Seed NCPDP F6 Official Standards into the conversion engine DB."
    )
    parser.add_argument(
        "--no-activate", action="store_true",
        help="Seed the rule set but do NOT make it the active rule set.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Delete and re-seed the rule set even if it already exists.",
    )
    args = parser.parse_args()

    db_path = os.environ.get("NCPDP_DB_PATH", "./ncpdp_converter.db")
    print(f"[SEEDER] Using database: {db_path}")
    print(f"[SEEDER] Rule set name:  {RULE_SET_NAME}")
    print(f"[SEEDER] Rule set ver:   {RULE_SET_VERSION}")
    print(f"[SEEDER] Total rules:    {len(ALL_RULES)}")
    print()

    seed(activate=not args.no_activate, force=args.force)


if __name__ == "__main__":
    main()
