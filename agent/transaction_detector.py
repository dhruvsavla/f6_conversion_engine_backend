"""
backend/agent/transaction_detector.py

Detects NCPDP transaction type from a ParsedTransaction.
Uses ParsedTransaction.get_field() and has_segment() — no dict access.

Detection priority (most specific first, RETAIL is the guaranteed fallback):
  REVERSAL > ELIGIBILITY > PRIOR_AUTH > COMPOUND > LTC > COB > MEDICARE_PART_D
  > CONTROLLED > SPECIALTY > RETAIL
"""
from __future__ import annotations

from .segment_parser import ParsedTransaction


class TransactionDetector:

    def detect(self, tx: ParsedTransaction, ruleset=None) -> str:
        """
        Detect transaction type. ruleset parameter accepted for backward compat
        but the detection logic is fully self-contained here.
        """
        tx_code  = (tx.get_field('HDR', '103-A3') or '').strip().upper()
        compound = (tx.get_field('CLM', '406-D6') or '').strip()
        pat_res  = (tx.get_field('PAT', '384-7E') or tx.get_field('PAT', '384-4X') or '').strip()
        scc      = (tx.get_field('CLM', '420-DK') or '').strip()
        level_of_service = (tx.get_field('CLM', '419-DJ') or '').strip()
        group_id = (tx.get_field('INS', '301-C1') or '').strip()

        # ── Reversal: B2 / 02 / 11 transaction codes ──────────────────────
        if tx_code in ('B2', '02', '11'):
            return 'REVERSAL'

        # ── Eligibility: E1 / 25 / E ──────────────────────────────────────
        if tx_code in ('E1', '25', 'E', 'E0'):
            return 'ELIGIBILITY'

        # ── Prior Authorization: PA / 21 / P1 ─────────────────────────────
        if tx_code in ('PA', '21', 'P1', 'P4'):
            return 'PRIOR_AUTH'

        # ── Compound: compound code = 2 OR CMP/I1 segment present ─────────
        if compound == '2' or tx.has_segment('CMP') or tx.has_segment('I1'):
            return 'COMPOUND'

        # ── LTC: patient residence is a long-term / facility code ──────────
        # Codes 03, 06, 09, 31–33, 99 indicate nursing home / extended care
        LTC_CODES = {'03', '06', '09', '31', '32', '33', '99'}
        if pat_res in LTC_CODES:
            return 'LTC'

        # ── COB: COB or L1 (legacy alias) segment present ─────────────────
        if tx.has_segment('COB') or tx.has_segment('L1'):
            return 'COB'

        # ── Medicare Part D: group ID prefix PDM / PDL ────────────────────
        # Part D plans typically issue group IDs starting with "PDM" or "PDL"
        if group_id.startswith(('PDM', 'PDL', 'PD-')):
            return 'MEDICARE_PART_D'

        # ── Controlled substance: DUR segment present ──────────────────────
        # DUR segment is required for Schedule II–V controlled substances.
        # SCC=08 is also sometimes used as a controlled-substance indicator.
        if tx.has_segment('DUR') or tx.has_segment('D5') or scc == '08':
            return 'CONTROLLED'

        # ── Specialty: Level of Service = 3 OR SCC 42/43 ──────────────────
        # Level of service 3 = specialty pharmacy (per many payer contracts).
        # SCC 42/43 = specialty drug submission.
        if level_of_service in ('3', '03') or scc in ('42', '43'):
            return 'SPECIALTY'

        # ── Default fallback ───────────────────────────────────────────────
        return 'RETAIL'


# ── Module-level backward-compat wrapper ─────────────────────────────────────

_detector = TransactionDetector()


def detect(parsed: ParsedTransaction, ruleset=None) -> str:
    """
    Module-level function matching the old interface.
    ruleset parameter accepted but not used — detection is now self-contained.
    """
    return _detector.detect(parsed, ruleset)
