"""
agent/reverse_orchestrator.py

Agentic pipeline for F6 → D.0 conversion. 8 steps, mirrors orchestrator.py.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path

from .f6_parser import F6Parser
from .reverse_rules_loader import ReverseRulesLoader
from .transaction_detector import detect as detect_tx_type
from .reverse_field_mapper import ReverseFieldMapper
from .d0_assembler import D0Assembler
from .reverse_audit_builder import build_reverse_audit

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent.parent / "rules"


@dataclass
class ReverseConversionResult:
    transaction_type: str
    d0_output:        str
    f6_input:         str
    agent_steps:      list[dict]
    audit:            dict


class ReverseOrchestrator:

    def __init__(self):
        self.parser       = F6Parser()
        self.rules_loader = ReverseRulesLoader()
        self.mapper       = ReverseFieldMapper()
        self.assembler    = D0Assembler()

    async def convert(self, f6_text: str) -> ReverseConversionResult:
        steps: list[dict] = []

        def step(sid: str, label: str, status: str, detail: str = '') -> dict:
            s = {'id': sid, 'label': label, 'status': status, 'detail': detail}
            steps.append(s)
            return s

        # Step 1 — Read rules
        step('reading_rules', 'Reading reverse rule files from rules/ folder', 'running')
        try:
            from . import rules_reader
            rs = rules_reader.load_all_from_db()
            steps[-1].update(status='complete',
                detail=f'Loaded {len(rs.files)} rule files, {rs.total_field_rules} forward field rules')
        except Exception as e:
            steps[-1].update(status='error', detail=str(e))
            raise

        # Step 2 — Parse F6
        step('parsing', 'Parsing F6 transaction', 'running')
        try:
            tx = self.parser.parse(f6_text)
            restored_count = sum(
                1 for seg in tx.segments
                for f in seg.fields
                if hasattr(f, 'is_restored_deprecated') and f.is_restored_deprecated
            )
            err_note = f', {len(tx.all_errors())} parse warnings' if tx.all_errors() else ''
            steps[-1].update(status='complete',
                detail=(
                    f'Parsed {len(tx.segments)} segments, '
                    f'{tx.total_fields} fields'
                    f'{f", {restored_count} ~~restored~~ fields" if restored_count else ""}'
                    f'{err_note}'
                ))
        except Exception as e:
            steps[-1].update(status='error', detail=str(e))
            raise

        # Step 3 — Detect transaction type
        step('detecting', 'Detecting transaction type', 'running')
        tx_type = detect_tx_type(tx)
        steps[-1].update(status='complete', detail=f'Detected: {tx_type}')

        # Step 4 — Build reverse rules
        step('planning', 'Building reverse rule set', 'running')
        try:
            reverse_rules = self.rules_loader.load(tx_type)
            total_rr = sum(len(v) for v in reverse_rules.values())
            steps[-1].update(status='complete',
                detail=f'{total_rr} reverse rules across {len(reverse_rules)} segments')
        except Exception as e:
            steps[-1].update(status='error', detail=str(e))
            raise

        # Step 5 — Map fields
        step('mapping', 'Applying reverse field mapping', 'running')
        try:
            mapping = self.mapper.map(tx, reverse_rules, tx_type)
            total = sum(
                len(seg.d0_fields) + len(seg.dropped) + len(seg.restored) + len(seg.warned)
                for seg in mapping.segments
            )
            steps[-1].update(status='complete', detail=f'Processed {total} fields')
        except Exception as e:
            steps[-1].update(status='error', detail=str(e))
            raise

        # Step 6 — Assemble D.0
        step('assembling', 'Assembling D.0 output', 'running')
        try:
            d0_output = self.assembler.assemble(mapping)
            steps[-1].update(status='complete', detail='D.0 transaction assembled')
        except Exception as e:
            steps[-1].update(status='error', detail=str(e))
            raise

        # Step 7 — Validate
        step('validating', 'Validating D.0 output', 'running')
        audit = build_reverse_audit(mapping)
        warns  = audit['summary']['warnings']
        errors = audit['summary']['errors']
        steps[-1].update(status='complete', detail=f'{warns} warnings, {errors} errors')

        # Step 8 — Build audit
        step('auditing', 'Building audit trail', 'running')
        steps[-1].update(status='complete', detail=f'{len(audit["entries"])} audit entries')

        return ReverseConversionResult(
            transaction_type=tx_type,
            d0_output=d0_output,
            f6_input=f6_text,
            agent_steps=steps,
            audit=audit,
        )
