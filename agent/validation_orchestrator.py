"""
agent/validation_orchestrator.py

6-step pipeline that runs F6 validation and returns a ValidationResult.
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field as dc_field
from typing import Optional

from .f6_parser import parse_f6
from .f6_validator import F6Validator, ValidationReport

logger = logging.getLogger(__name__)

STEPS = [
    ('parsing',    'Parsing F6 transaction'),
    ('detecting',  'Detecting transaction type'),
    ('loading',    'Loading active rule set'),
    ('validating', 'Running validation checks'),
    ('scoring',    'Computing score & categories'),
    ('persisting', 'Persisting to database'),
]


@dataclass
class ValidationResult:
    transaction_type: str
    overall_status:   str
    report:           ValidationReport
    agent_steps:      list[dict] = dc_field(default_factory=list)
    rule_set_id:      Optional[str] = None
    rule_set_name:    Optional[str] = None
    validation_id:    Optional[str] = None


class ValidationOrchestrator:

    def __init__(self):
        self._validator = F6Validator()

    async def validate(
        self,
        f6_text: str,
        rule_set_id: Optional[str] = None,
        persist: bool = True,
    ) -> ValidationResult:
        steps: list[dict] = []

        def emit(step_id: str, label: str, status: str, detail: str = '') -> dict:
            s = {'id': step_id, 'label': label, 'status': status, 'detail': detail}
            steps.append(s)
            return s

        # ── Step 1: Parsing ───────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            tx = parse_f6(f6_text)
            elapsed = round((time.monotonic() - t0) * 1000)
            emit('parsing', 'Parsing F6 transaction', 'complete',
                 f'{tx.total_fields} fields across {len(tx.segments)} segments ({elapsed}ms)')
        except Exception as exc:
            emit('parsing', 'Parsing F6 transaction', 'error', str(exc))
            raise RuntimeError(f'F6 parse failed: {exc}') from exc

        # ── Step 2: Detect transaction type ───────────────────────────────────
        try:
            from .transaction_detector import detect as detect_tx_type
            tx_type = detect_tx_type(tx)
            emit('detecting', 'Detecting transaction type', 'complete', tx_type)
        except Exception as exc:
            tx_type = 'RETAIL'
            emit('detecting', 'Detecting transaction type', 'complete',
                 f'Detection failed ({exc}), defaulting to RETAIL')

        # ── Step 3: Load rule set ─────────────────────────────────────────────
        import db_ops
        active_rs = None
        rs_id     = rule_set_id
        try:
            active_rs = db_ops.get_active_rule_set()
            if not rs_id and active_rs:
                rs_id = active_rs['id']
            name = active_rs['name'] if active_rs else '(none)'
            emit('loading', 'Loading active rule set', 'complete',
                 f'Rule set: {name}')
        except Exception as exc:
            emit('loading', 'Loading active rule set', 'warn', str(exc))

        # ── Step 4: Validate ──────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            report = self._validator.validate(tx, tx_type, rule_set_id=rs_id)
            elapsed = round((time.monotonic() - t0) * 1000)
            emit('validating', 'Running validation checks', 'complete',
                 f'{len(report.checks)} checks completed in {elapsed}ms')
        except Exception as exc:
            emit('validating', 'Running validation checks', 'error', str(exc))
            raise RuntimeError(f'Validation failed: {exc}') from exc

        # ── Step 5: Score ─────────────────────────────────────────────────────
        summary = report.summary
        cats    = report.categories
        emit('scoring', 'Computing score & categories', 'complete',
             f'Score {summary["score"]}/100 · {summary["errors"]} errors · {summary["warnings"]} warnings')

        # ── Step 6: Persist ───────────────────────────────────────────────────
        val_id: Optional[str] = None
        if persist:
            try:
                checks_payload = [
                    {
                        'check_id':    c.check_id,
                        'category':    c.category,
                        'segment':     c.segment,
                        'field_id':    c.field_id,
                        'field_name':  c.field_name,
                        'status':      c.status,
                        'expected':    c.expected,
                        'actual':      c.actual,
                        'message':     c.message,
                        'occurrence':  c.occurrence,
                        'rule_source': c.rule_source,
                    }
                    for c in report.checks
                ]
                val_id = db_ops.save_validation(
                    transaction_type=tx_type,
                    overall_status=report.overall_status,
                    summary=summary,
                    categories=cats,
                    checks=checks_payload,
                    rule_set_id=rs_id,
                    parse_errors=report.parse_errors,
                )
                emit('persisting', 'Persisting to database', 'complete', f'Saved as {val_id[:8]}')
            except Exception as exc:
                emit('persisting', 'Persisting to database', 'warn',
                     f'Save failed (validation still valid): {exc}')

        return ValidationResult(
            transaction_type=tx_type,
            overall_status=report.overall_status,
            report=report,
            agent_steps=steps,
            rule_set_id=rs_id,
            rule_set_name=active_rs['name'] if active_rs else None,
            validation_id=val_id,
        )
