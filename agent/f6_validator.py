"""
agent/f6_validator.py

Validates an F6 transaction against the active rule set.

Five check categories:
  structural  — required segments, ordering, occurrence limits
  mandatory   — mandatory_f6=true fields present and non-empty
  format      — field value format rules (length, pattern, known codes)
  business    — conditional rules (compound, SCC, Part D, pricing math)
  deprecated  — D.0-only fields that must not appear in F6
"""
from __future__ import annotations

import re
import json
import logging
from collections import Counter
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Optional

from .segment_parser import ParsedTransaction

logger = logging.getLogger(__name__)

# ── Known valid code sets ─────────────────────────────────────────────────────

VALID_GENDER_CODES       = {'0', '1', '2'}
VALID_DAW_CODES          = {'0','1','2','3','4','5','6','7','8','9'}
VALID_ORIGIN_CODES       = {'1','2','3','4','5'}
VALID_COMPOUND_CODES     = {'1','2'}
VALID_RELATIONSHIP_CODES = {'1','2','3','4','01','02','03','04'}
VALID_TRANSACTION_CODES  = {'B1','B2','E1','PA','11','25','21','01','02'}
VALID_PHARMACY_SVC_TYPE  = {'01','02','03','04','05','06','07','08','09','99'}
VALID_BENEFIT_STAGES     = {'1','2','3','4'}

DATE_RE  = re.compile(r'^\d{8}$')
NPI_RE   = re.compile(r'^\d{10}$')
NDC11_RE = re.compile(r'^\d{11}$')
BIN8_RE  = re.compile(r'^\d{8}$')
DEA_RE   = re.compile(r'^[A-Z]{2}\d{7}$')

# Fields that are D.0-only and must NOT appear in F6
DEPRECATED_FIELDS = {
    '990-MG': 'Legacy Routing Code — deprecated in F6, must not be transmitted',
}

# Required segments per transaction type
REQUIRED_SEGMENTS = {
    'RETAIL':         ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI'],
    'SPECIALTY':      ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI'],
    'CONTROLLED':     ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI'],
    'COB':            ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI', 'COB'],
    'REVERSAL':       ['HDR', 'CLM'],
    'COMPOUND':       ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI', 'CMP'],
    'LTC':            ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI'],
    'MEDICARE_PART_D':['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PRI'],
    'ELIGIBILITY':    ['HDR', 'INS', 'PAT'],
    'PRIOR_AUTH':     ['HDR', 'INS', 'PAT', 'CLM', 'PRE', 'PA'],
}

MAX_OCCURRENCES = {'INS': 3, 'DUR': 9, 'CMP': 25, 'COB': 3}

LTC_PATIENT_RESIDENCE_CODES = {'03', '06', '09', '31', '32', '33'}


@dataclass
class ValidationCheck:
    check_id:    str
    category:    str      # structural|mandatory|format|business|deprecated
    segment:     str
    field_id:    str
    field_name:  str
    status:      str      # PASS|WARN|ERROR|INFO|SKIP
    expected:    str
    actual:      str
    message:     str
    occurrence:  int = 1
    rule_source: str = ''


@dataclass
class ValidationReport:
    transaction_type: str
    overall_status:   str
    checks:           list[ValidationCheck]
    parse_errors:     list

    @property
    def summary(self) -> dict:
        total    = len(self.checks)
        passed   = sum(1 for c in self.checks if c.status == 'PASS')
        warnings = sum(1 for c in self.checks if c.status == 'WARN')
        errors   = sum(1 for c in self.checks if c.status == 'ERROR')
        score    = round((passed / total) * 100) if total else 0
        return {'total_checks': total, 'passed': passed,
                'warnings': warnings, 'errors': errors, 'score': score}

    @property
    def categories(self) -> dict:
        result = {}
        for cat in ('structural', 'mandatory', 'format', 'business', 'deprecated'):
            cc = [c for c in self.checks if c.category == cat]
            result[cat] = {
                'total':    len(cc),
                'passed':   sum(1 for c in cc if c.status == 'PASS'),
                'warnings': sum(1 for c in cc if c.status == 'WARN'),
                'errors':   sum(1 for c in cc if c.status == 'ERROR'),
            }
        return result


class F6Validator:

    def validate(
        self,
        tx: ParsedTransaction,
        transaction_type: str,
        rule_set_id: Optional[str] = None,
    ) -> ValidationReport:
        checks: list[ValidationCheck] = []

        # Load rules from DB
        import db_ops
        rs_id = rule_set_id
        if not rs_id:
            active = db_ops.get_active_rule_set()
            rs_id  = active['id'] if active else None

        # Build segment → rules lookup
        seg_rule_map: dict[str, list[dict]] = {}
        if rs_id:
            for row in db_ops.list_rules(rule_set_id=rs_id, transaction_type=transaction_type):
                sid = row['segment_id']
                try:
                    rule_data = json.loads(row.get('rule_json') or '{}')
                except Exception:
                    rule_data = {}
                rule_data.setdefault('mandatory_f6', bool(row.get('mandatory_f6')))
                rule_data.setdefault('field_id',   row['field_id'])
                rule_data.setdefault('field_name', row.get('field_name', row['field_id']))
                rule_data.setdefault('action',     row.get('action', 'carry'))
                seg_rule_map.setdefault(sid, []).append(rule_data)

        checks += self._check_structural(tx, transaction_type)
        checks += self._check_mandatory(tx, transaction_type, seg_rule_map)
        checks += self._check_format(tx)
        checks += self._check_business(tx, transaction_type)
        checks += self._check_deprecated(tx)

        errors   = sum(1 for c in checks if c.status == 'ERROR')
        warnings = sum(1 for c in checks if c.status == 'WARN')
        if errors:
            overall = 'INVALID'
        elif warnings:
            overall = 'VALID_WITH_WARNINGS'
        else:
            overall = 'VALID'

        return ValidationReport(
            transaction_type=transaction_type,
            overall_status=overall,
            checks=checks,
            parse_errors=[str(e) for e in tx.all_errors()],
        )

    # ── Category 1: Structural ────────────────────────────────────────────────

    def _check_structural(self, tx: ParsedTransaction, tx_type: str) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        required = REQUIRED_SEGMENTS.get(tx_type, REQUIRED_SEGMENTS['RETAIL'])
        present  = {s.normalized_id for s in tx.segments}

        for seg_id in required:
            ok = seg_id in present
            checks.append(ValidationCheck(
                check_id=f'STRUCT_SEG_{seg_id}', category='structural',
                segment=seg_id, field_id='', field_name=f'{seg_id} Segment',
                status='PASS' if ok else 'ERROR',
                expected=f'{seg_id} segment present',
                actual='present' if ok else 'MISSING',
                message=(f'{seg_id} segment is present' if ok
                         else f'{seg_id} is required for {tx_type} but is missing'),
            ))

        # Occurrence limits
        occ_counts = Counter(s.normalized_id for s in tx.segments)
        for seg_id, count in occ_counts.items():
            max_occ = MAX_OCCURRENCES.get(seg_id)
            if max_occ:
                ok = count <= max_occ
                checks.append(ValidationCheck(
                    check_id=f'STRUCT_OCC_{seg_id}', category='structural',
                    segment=seg_id, field_id='', field_name=f'{seg_id} Occurrences',
                    status='PASS' if ok else 'ERROR',
                    expected=f'≤{max_occ} occurrences',
                    actual=f'{count} occurrence(s)',
                    message=(f'{seg_id} occurrence count ({count}) is within limit'
                             if ok else
                             f'{seg_id} appears {count} times but maximum is {max_occ}'),
                ))

        # HDR must be first
        if tx.segments:
            first = tx.segments[0].normalized_id
            ok    = first == 'HDR'
            checks.append(ValidationCheck(
                check_id='STRUCT_HDR_FIRST', category='structural',
                segment='HDR', field_id='', field_name='Segment Order',
                status='PASS' if ok else 'ERROR',
                expected='HDR must be the first segment',
                actual=f'First segment: {first}',
                message=('HDR is correctly the first segment' if ok
                         else f'HDR must be first but found {first}'),
            ))

        return checks

    # ── Category 2: Mandatory fields ─────────────────────────────────────────

    def _check_mandatory(
        self,
        tx: ParsedTransaction,
        tx_type: str,
        seg_rule_map: dict[str, list[dict]],
    ) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []

        for seg_id, seg_rules in seg_rule_map.items():
            for rule in seg_rules:
                if not rule.get('mandatory_f6'):
                    continue
                if rule.get('action') == 'remove':
                    continue  # deprecated field — shouldn't be in F6

                field_id   = rule.get('field_id', '')
                field_name = rule.get('field_name', field_id)
                segs       = tx.get_segments(seg_id)
                occ_list   = segs if segs else [None]

                for occ_idx, _ in enumerate(occ_list, 1):
                    val = tx.get_field(seg_id, field_id, occurrence=occ_idx)
                    occ_suffix = f' (occurrence {occ_idx})' if len(occ_list) > 1 else ''

                    if val is None:
                        status, actual = 'ERROR', 'MISSING'
                        msg = (f'{field_name} ({field_id}) is mandatory in F6 '
                               f'but absent from {seg_id}{occ_suffix}')
                    elif val.strip() == '':
                        status, actual = 'WARN', 'EMPTY'
                        msg = (f'{field_name} ({field_id}) is mandatory but '
                               f'present with empty value{occ_suffix}')
                    else:
                        status, actual = 'PASS', val
                        msg = f'{field_name} ({field_id}) is present{occ_suffix}'

                    checks.append(ValidationCheck(
                        check_id=f'MAND_{seg_id}_{field_id.replace("-","_")}_{occ_idx}',
                        category='mandatory', segment=seg_id,
                        field_id=field_id, field_name=field_name,
                        status=status, expected='non-empty value',
                        actual=actual, message=msg,
                        occurrence=occ_idx, rule_source=f'{tx_type} mandatory rule',
                    ))

        return checks

    # ── Category 3: Format checks ─────────────────────────────────────────────

    def _check_format(self, tx: ParsedTransaction) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []

        def add(cid, seg, fid, fname, status, expected, actual, msg, occ=1):
            checks.append(ValidationCheck(
                check_id=cid, category='format',
                segment=seg, field_id=fid, field_name=fname,
                status=status, expected=expected,
                actual=actual, message=msg, occurrence=occ,
            ))

        # Version must be F6
        ver = tx.get_field('HDR', '102-A2')
        if ver is not None:
            ok = ver.strip() == 'F6'
            add('FMT_HDR_VERSION', 'HDR', '102-A2', 'Version / Release Number',
                'PASS' if ok else 'ERROR', '"F6"', repr(ver.strip()),
                'Version is correctly "F6"' if ok
                else f'Version must be "F6" but found "{ver.strip()}" — not a valid F6 transaction')

        # BIN/IIN must be 8 digits
        bin_val = tx.get_field('HDR', '101-A1')
        if bin_val is not None:
            ok = bool(BIN8_RE.match(bin_val.strip()))
            add('FMT_HDR_BIN', 'HDR', '101-A1', 'BIN / IIN Number',
                'PASS' if ok else 'ERROR', '8-digit numeric',
                f'{bin_val.strip()} ({len(bin_val.strip())} chars)',
                'BIN/IIN is correctly 8 digits (zero-padded)' if ok
                else f'BIN/IIN must be 8 digits but found "{bin_val.strip()}"')

        # Prescriber NPI — 10 digits
        npi = tx.get_field('PRE', '411-DB')
        if npi is not None:
            ok = bool(NPI_RE.match(npi.strip()))
            add('FMT_PRE_NPI', 'PRE', '411-DB', 'Prescriber NPI',
                'PASS' if ok else 'ERROR', '10-digit numeric', npi.strip(),
                'NPI is correctly 10 digits' if ok
                else f'NPI must be exactly 10 digits but found "{npi.strip()}"')

        # NDC — 11 digits, no hyphens
        ndc = tx.get_field('CLM', '402-D2')
        if ndc is not None:
            s = ndc.strip()
            if '-' in s:
                add('FMT_CLM_NDC', 'CLM', '402-D2', 'Product/Service ID (NDC)',
                    'ERROR', '11-digit NDC, no hyphens', s,
                    f'NDC contains hyphens ("{s}"). F6 requires hyphens removed. Expected: "{s.replace("-","")}"')
            elif not NDC11_RE.match(s):
                add('FMT_CLM_NDC', 'CLM', '402-D2', 'Product/Service ID (NDC)',
                    'ERROR', '11-digit numeric', s,
                    f'NDC must be exactly 11 digits but found "{s}" ({len(s)} digits)')
            else:
                add('FMT_CLM_NDC', 'CLM', '402-D2', 'Product/Service ID (NDC)',
                    'PASS', '11-digit numeric', s, 'NDC is 11 digits and correctly formatted')

        # Date of birth
        dob = tx.get_field('PAT', '304-C4')
        if dob is not None:
            ok, msg = self._validate_date(dob.strip())
            add('FMT_PAT_DOB', 'PAT', '304-C4', 'Patient Date of Birth',
                'PASS' if ok else 'ERROR', 'CCYYMMDD format', dob.strip(), msg)

        # Date of service
        dos = tx.get_field('HDR', '401-D1')
        if dos is not None:
            ok, msg = self._validate_date(dos.strip())
            add('FMT_HDR_DOS', 'HDR', '401-D1', 'Date of Service',
                'PASS' if ok else 'ERROR', 'CCYYMMDD format', dos.strip(), msg)

        # Patient gender
        gender = tx.get_field('PAT', '305-C5')
        if gender is not None:
            ok = gender.strip() in VALID_GENDER_CODES
            add('FMT_PAT_GENDER', 'PAT', '305-C5', 'Patient Gender Code',
                'PASS' if ok else 'WARN',
                f'one of {sorted(VALID_GENDER_CODES)}', gender.strip(),
                'Gender code is valid' if ok
                else f'Unrecognized gender code "{gender.strip()}"')

        # DAW code
        daw = tx.get_field('CLM', '408-D8')
        if daw is not None:
            ok = daw.strip() in VALID_DAW_CODES
            add('FMT_CLM_DAW', 'CLM', '408-D8', 'DAW / Product Selection Code',
                'PASS' if ok else 'WARN', '0–9', daw.strip(),
                'DAW code is valid' if ok else f'Unrecognized DAW code "{daw.strip()}"')

        # Origin code
        origin = tx.get_field('CLM', '419-DJ')
        if origin is not None:
            ok = origin.strip() in VALID_ORIGIN_CODES
            add('FMT_CLM_ORIGIN', 'CLM', '419-DJ', 'Prescription Origin Code',
                'PASS' if ok else 'WARN',
                f'one of {sorted(VALID_ORIGIN_CODES)}', origin.strip(),
                'Origin code is valid' if ok
                else f'Unrecognized origin code "{origin.strip()}" (1=written,2=phone,3=fax,4=electronic,5=pharmacy)')

        # Compound code
        cmpd = tx.get_field('CLM', '406-D6')
        if cmpd is not None:
            ok = cmpd.strip() in VALID_COMPOUND_CODES
            add('FMT_CLM_COMPOUND', 'CLM', '406-D6', 'Compound Code',
                'PASS' if ok else 'WARN', '1 or 2', cmpd.strip(),
                'Compound code is valid' if ok
                else f'Compound code "{cmpd.strip()}" must be 1 (not compound) or 2 (compound)')

        # Pharmacy Service Type (new in F6)
        pst = tx.get_field('CLM', '147-U7')
        if pst is not None and pst.strip():
            ok = pst.strip() in VALID_PHARMACY_SVC_TYPE
            add('FMT_CLM_PST', 'CLM', '147-U7', 'Pharmacy Service Type',
                'PASS' if ok else 'WARN',
                f'one of {sorted(VALID_PHARMACY_SVC_TYPE)}', pst.strip(),
                'Pharmacy Service Type code is valid' if ok
                else f'Unrecognized Pharmacy Service Type "{pst.strip()}"')

        # Transaction code
        tc = tx.get_field('HDR', '103-A3')
        if tc is not None:
            ok = tc.strip() in VALID_TRANSACTION_CODES
            add('FMT_HDR_TC', 'HDR', '103-A3', 'Transaction Code',
                'PASS' if ok else 'WARN',
                f'one of {sorted(VALID_TRANSACTION_CODES)}', tc.strip(),
                'Transaction code is recognized' if ok
                else f'Unrecognized transaction code "{tc.strip()}"')

        # Patient relationship
        rel = tx.get_field('INS', '306-C6')
        if rel is not None:
            VALID_REL = {'1','2','3','4','01','02','03','04'}
            ok = rel.strip() in VALID_REL
            add('FMT_INS_REL', 'INS', '306-C6', 'Patient Relationship Code',
                'PASS' if ok else 'WARN', f'one of {sorted(VALID_REL)}', rel.strip(),
                'Relationship code is valid' if ok
                else f'Unrecognized relationship code "{rel.strip()}"')

        return checks

    def _validate_date(self, value: str) -> tuple[bool, str]:
        if not DATE_RE.match(value):
            return False, f'"{value}" is not in CCYYMMDD format (must be 8 digits)'
        try:
            datetime(int(value[:4]), int(value[4:6]), int(value[6:8]))
            return True, f'Date "{value}" is valid'
        except ValueError:
            return False, f'"{value}" is not a valid calendar date'

    # ── Category 4: Business rules ────────────────────────────────────────────

    def _check_business(self, tx: ParsedTransaction, tx_type: str) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []

        def add(cid, seg, fid, fname, status, expected, actual, msg):
            checks.append(ValidationCheck(
                check_id=cid, category='business',
                segment=seg, field_id=fid, field_name=fname,
                status=status, expected=expected, actual=actual, message=msg,
            ))

        # Compound: if compound code = 2, CMP segment must be present
        cmpd = tx.get_field('CLM', '406-D6')
        if cmpd and cmpd.strip() == '2':
            has_cmp = tx.has_segment('CMP')
            add('BIZ_COMPOUND_CMP', 'CMP', '', 'CMP Segment Required',
                'PASS' if has_cmp else 'ERROR',
                'CMP segment present when Compound Code = 2',
                'CMP present' if has_cmp else 'CMP MISSING',
                'CMP segment is correctly present for compound claim'
                if has_cmp else
                'Compound Code = 2 but CMP segment is missing')

        # SCC = 42/43: PA number required, PST should be 08
        scc = tx.get_field('CLM', '420-DK')
        if scc and scc.strip() in ('42', '43'):
            pa_num = tx.get_field('CLM', '461-EU') or tx.get_field('PA', '462-EV')
            ok = bool(pa_num and pa_num.strip())
            add('BIZ_SPECIALTY_PA', 'CLM', '461-EU', 'PA Number (Specialty)',
                'PASS' if ok else 'ERROR',
                'PA number required when SCC = 42 or 43',
                pa_num.strip() if pa_num else 'MISSING',
                'Prior Authorization Number is present for specialty claim'
                if ok else
                f'SCC = {scc.strip()} requires a PA Number (461-EU) but it is missing')

            pst = tx.get_field('CLM', '147-U7')
            ok_pst = pst and pst.strip() == '08'
            add('BIZ_SPECIALTY_PST', 'CLM', '147-U7', 'Pharmacy Service Type (Specialty)',
                'PASS' if ok_pst else 'WARN', '"08" when SCC = 42 or 43',
                pst.strip() if pst else 'MISSING',
                'Pharmacy Service Type is correctly 08 (Specialty)'
                if ok_pst else
                f'SCC = {scc.strip()} indicates specialty but PST = "{(pst or "").strip()}" (expected "08")')

        # Medicare Part D: benefit stage required in COB
        med_d = tx.get_field('INS', '694-ZJ')
        if med_d and med_d.strip() == 'Y':
            bs = tx.get_field('COB', '392-MU')
            ok = bool(bs and bs.strip())
            add('BIZ_PART_D_BENEFIT_STAGE', 'COB', '392-MU', 'Benefit Stage Qualifier',
                'PASS' if ok else 'ERROR',
                'Benefit Stage Qualifier required when Medicare Part D = Y',
                bs.strip() if bs else 'MISSING',
                'Benefit Stage Qualifier is present'
                if ok else
                'Medicare Part D = Y but Benefit Stage Qualifier (392-MU) is missing from COB')

        # LTC patient residence: PST should be 05
        pat_res = tx.get_field('PAT', '384-4X')
        if pat_res and pat_res.strip() in LTC_PATIENT_RESIDENCE_CODES:
            pst = tx.get_field('CLM', '147-U7')
            ok  = pst and pst.strip() == '05'
            add('BIZ_LTC_PST_05', 'CLM', '147-U7', 'Pharmacy Service Type (LTC)',
                'PASS' if ok else 'WARN',
                '"05" (LTC) when patient residence is an LTC code',
                pst.strip() if pst else 'MISSING',
                'Pharmacy Service Type is correctly 05 (LTC)'
                if ok else
                f'Patient Residence "{pat_res.strip()}" is LTC but PST = "{(pst or "").strip()}" (expected "05")')

        # Pricing math: ingredient + dispensing should equal gross amount
        ing   = tx.get_field('PRI', '409-D9')
        disp  = tx.get_field('PRI', '412-DC')
        gross = tx.get_field('PRI', '430-DU')
        if ing and disp and gross:
            try:
                expected_g = int(ing.strip()) + int(disp.strip())
                actual_g   = int(gross.strip())
                ok = expected_g == actual_g
                add('BIZ_PRICING_MATH', 'PRI', '430-DU', 'Gross Amount Due (Math)',
                    'PASS' if ok else 'WARN',
                    f'Ingredient ({ing.strip()}) + Dispensing ({disp.strip()}) = {expected_g}',
                    gross.strip(),
                    f'Gross Amount ({gross.strip()}) correctly equals Ingredient + Dispensing'
                    if ok else
                    f'Pricing mismatch: {ing.strip()} + {disp.strip()} = {expected_g} but Gross Amount = {gross.strip()}')
            except (ValueError, TypeError):
                pass  # non-numeric — caught by format checks

        # COB other payer amount must be numeric
        opa = tx.get_field('COB', '431-DV')
        if opa is not None:
            try:
                float(opa.strip())
                ok = True
            except ValueError:
                ok = False
            add('BIZ_COB_OPA_NUMERIC', 'COB', '431-DV', 'Other Payer Amount Paid',
                'PASS' if ok else 'ERROR', 'numeric value (cents)', opa.strip(),
                'Other Payer Amount is numeric'
                if ok else f'Other Payer Amount "{opa.strip()}" is not a valid number')

        # DEA Number — required only for Schedule II-V controlled substances.
        # Cannot determine drug schedule from NDC alone without a lookup table,
        # so emit WARN (not ERROR). The LLM layer escalates to ERROR when it
        # confirms the NDC is a scheduled drug.
        dea = tx.get_field('PRE', '464-EX')
        ndc = tx.get_field('CLM', '402-D2')
        if not dea or not dea.strip():
            checks.append(ValidationCheck(
                check_id    = 'BIZ_DEA_ABSENT',
                category    = 'business',
                segment     = 'PRE',
                field_id    = '464-EX',
                field_name  = 'Prescriber DEA Number',
                status      = 'WARN',
                expected    = 'DEA required for Schedule II-V controlled substances',
                actual      = 'MISSING',
                message     = (
                    f'Prescriber DEA Number (464-EX) is absent. '
                    f'If NDC {ndc.strip() if ndc else "unknown"} is a Schedule II-V '
                    f'controlled substance, DEA is required and this claim will reject. '
                    f'The LLM resolver will verify the NDC schedule and escalate to ERROR '
                    f'if the drug is controlled.'
                ),
                rule_source = 'business — controlled substance check',
            ))
        elif dea.strip():
            ok = bool(DEA_RE.match(dea.strip()))
            checks.append(ValidationCheck(
                check_id    = 'BIZ_DEA_FORMAT',
                category    = 'business',
                segment     = 'PRE',
                field_id    = '464-EX',
                field_name  = 'Prescriber DEA Number',
                status      = 'PASS' if ok else 'ERROR',
                expected    = '2 letters + 7 digits (e.g. AB1234567)',
                actual      = dea.strip(),
                message     = (
                    'DEA number format is valid'
                    if ok else
                    f'DEA number "{dea.strip()}" does not match expected format. '
                    f'Must be 2 uppercase letters followed by 7 digits.'
                ),
                rule_source = 'business — DEA format check',
            ))

        return checks

    # ── Category 5: Deprecated fields ────────────────────────────────────────

    def _check_deprecated(self, tx: ParsedTransaction) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []

        for seg in tx.segments:
            for f in seg.fields:
                if f.field_id in DEPRECATED_FIELDS:
                    checks.append(ValidationCheck(
                        check_id=f'DEPR_{seg.normalized_id}_{f.field_id.replace("-","_")}',
                        category='deprecated', segment=seg.normalized_id,
                        field_id=f.field_id, field_name=f.field_id,
                        status='ERROR',
                        expected='field must not appear in F6',
                        actual=f'{f.field_id}={f.value}',
                        message=(f'{f.field_id} is deprecated in F6 and must not be transmitted. '
                                 f'Reason: {DEPRECATED_FIELDS[f.field_id]}'),
                        occurrence=seg.occurrence,
                    ))

        if not any(c.category == 'deprecated' for c in checks):
            checks.append(ValidationCheck(
                check_id='DEPR_NONE_FOUND', category='deprecated',
                segment='', field_id='', field_name='Deprecated Field Check',
                status='PASS', expected='No deprecated D.0 fields',
                actual='None found',
                message='No deprecated D.0-only fields detected',
            ))

        return checks
