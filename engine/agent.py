"""
engine/agent.py

A single conversion agent (async worker coroutine).

Each agent:
1. Pulls a job from the queue
2. Runs the full conversion pipeline in a thread (asyncio.to_thread) so the
   event loop stays responsive for API requests
3. Writes results to DB
4. Updates batch progress if batch job
5. Records metrics
6. Loops back to step 1

Rules are loaded from the filesystem on each conversion (same as the existing
streaming endpoint). The rule_cache is consulted for metadata (rule set name).

Each agent is stateless between jobs — all state lives in the DB.
"""

import asyncio
import logging
import time

import db_ops
from engine.job_queue import ConversionQueue, ConversionJob
from engine.rule_cache import RuleCache
from engine.metrics import AgentMetrics

logger = logging.getLogger(__name__)

# Finding codes where WARN severity should still trigger LLM escalation,
# because the LLM can actually fill these fields with useful values.
LLM_ESCALATION_WARN_CODES = frozenset({
    'DEA',   # LLM knows NDC controlled-substance schedules
    'APT',   # LLM can infer Adjudicated Program Type from Medicare/Medicaid indicators
    'BNI',   # LLM can infer Benefit Network Indicator from plan context
    'NDC',   # LLM can attempt NDC correction from context
    'QP',    # LLM can infer Quantity Prescribed from days supply + quantity dispensed
    'BRD',   # LLM can infer Basis of Reimbursement from claim context
})


class ConversionAgent:
    """A single agent. Runs as an asyncio coroutine."""

    def __init__(
        self,
        agent_id:   int,
        queue:      ConversionQueue,
        cache:      RuleCache,
        metrics:    AgentMetrics,
        stop_event: asyncio.Event,
    ):
        self.agent_id   = agent_id
        self.queue      = queue
        self.cache      = cache
        self.metrics    = metrics
        self.stop_event = stop_event
        self._log       = logging.getLogger(f'{__name__}.agent{agent_id}')

    async def run(self) -> None:
        """Main agent loop. Runs until stop_event is set."""
        self._log.info(f'Agent {self.agent_id} started')

        while not self.stop_event.is_set():
            try:
                try:
                    job: ConversionJob = await asyncio.wait_for(
                        self.queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                await self._process(job)

            except asyncio.CancelledError:
                self._log.info(f'Agent {self.agent_id} cancelled')
                break
            except Exception as e:
                self._log.error(
                    f'Agent {self.agent_id} unhandled exception in run loop: {e}',
                    exc_info=True,
                )
                await asyncio.sleep(0.1)

        self._log.info(f'Agent {self.agent_id} stopped')

    async def _process(self, job: ConversionJob) -> None:
        """Process one job end-to-end."""
        start = time.monotonic()
        self.metrics.current_job_id = job.job_id

        self._log.info(
            f'Agent {self.agent_id} processing job {job.job_id[:8]} '
            f'(conv={job.conversion_id[:8]}, dir={job.direction}, '
            f'priority={job.priority})'
        )

        try:
            await asyncio.wait_for(
                self._execute(job),
                timeout=job.timeout_seconds,
            )
            duration_ms = (time.monotonic() - start) * 1000
            self.metrics.record(duration_ms, success=True)
            self._log.info(
                f'Agent {self.agent_id} completed job {job.job_id[:8]} '
                f'in {duration_ms:.1f}ms'
            )

        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000
            self.metrics.record(duration_ms, success=False, timed_out=True)
            error_msg = (
                f'Conversion timed out after {job.timeout_seconds:.0f}s '
                f'(agent {self.agent_id})'
            )
            self._log.error(f'Job {job.job_id[:8]} timed out')
            db_ops.fail_conversion(job.conversion_id, error_msg)
            if job.batch_id:
                db_ops.update_batch_progress(job.batch_id)

        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            self.metrics.record(duration_ms, success=False)
            error_msg = f'Agent {self.agent_id}: {type(e).__name__}: {e}'
            self._log.error(f'Job {job.job_id[:8]} failed: {e}', exc_info=True)
            db_ops.fail_conversion(job.conversion_id, error_msg)
            if job.batch_id:
                db_ops.update_batch_progress(job.batch_id)

        finally:
            self.queue.task_done(job.job_id)

    async def _execute(self, job: ConversionJob) -> None:
        db_ops.mark_conversion_processing(job.conversion_id)
        if job.direction == 'D0_TO_F6':
            await self._execute_forward(job)
        else:
            await self._execute_reverse(job)

    async def _execute_forward(self, job: ConversionJob) -> None:
        """
        D.0 → F6 hybrid conversion.

        Phase 1: deterministic pipeline (always runs). Each sub-phase writes
        its status incrementally to agent_steps so the DB-polling SSE endpoint
        can emit running→complete events for the live pipeline visualizer.
        Step IDs match STEP_DEFS in PipelineSteps.jsx.

        Phase 2: LLM escalation when Phase 1 produces errors, missing mandatory
                 fields, or UNKNOWN tx type — and LLM is enabled.
        """
        from agent import (
            rules_reader, segment_parser, transaction_detector,
            field_mapper, f6_assembler, audit_builder,
        )

        rule_set_name = self.cache.rule_set_name or 'default'
        cid           = job.conversion_id

        def _s(step_id: str, order: int, label: str, status: str, detail: str = '') -> None:
            db_ops.upsert_agent_step(cid, {
                'id': step_id, 'step_order': order, 'label': label,
                'status': status, 'detail': detail,
            })

        # ── Phase 1: deterministic pipeline ──────────────────────────────────

        _s('reading_rules', 0, 'Reading rule files', 'running')
        ruleset = await asyncio.to_thread(rules_reader.load_all_from_db)
        _s('reading_rules', 0, 'Reading rule files', 'complete',
           f'{len(ruleset.files)} files')

        _s('parsing', 1, 'Parsing D.0 segments', 'running')
        parsed = await asyncio.to_thread(segment_parser.parse_d0, job.input_text)
        _s('parsing', 1, 'Parsing D.0 segments', 'complete',
           f'{len(parsed.segments)} segments')

        _s('detecting', 2, 'Detecting transaction type', 'running')
        tx_type = await asyncio.to_thread(transaction_detector.detect, parsed, ruleset)
        _s('detecting', 2, 'Detecting transaction type', 'complete',
           f'Detected: {tx_type}')

        _s('planning', 3, 'Loading applicable rules', 'running')
        tx_rules = await asyncio.to_thread(ruleset.get_rules_for, tx_type)
        _s('planning', 3, 'Loading applicable rules', 'complete',
           f'{tx_type} rules loaded')

        _s('mapping', 4, 'Mapping fields', 'running')
        mapping = await asyncio.to_thread(field_mapper.map_fields, parsed, tx_rules)
        total_mapped = sum(len(seg.all_fields()) for seg in mapping.segments)
        _s('mapping', 4, 'Mapping fields', 'complete',
           f'{total_mapped} fields')

        _s('assembling', 5, 'Assembling F6 output', 'running')
        f6_output = await asyncio.to_thread(f6_assembler.assemble, mapping)
        _s('assembling', 5, 'Assembling F6 output', 'complete',
           'F6 assembled')

        # findings already computed inside mapping by field_mapper
        _s('validating', 6, 'Running validation rules', 'running')
        _s('validating', 6, 'Running validation rules', 'complete',
           f'{len(mapping.findings)} findings')

        _s('auditing', 7, 'Building audit trail', 'running')
        audit = await asyncio.to_thread(audit_builder.build_audit, mapping)
        _s('auditing', 7, 'Building audit trail', 'complete',
           f'{len(audit["entries"])} entries')

        # ── Phase 2: LLM escalation ───────────────────────────────────────────
        llm_decisions: list = []
        llm_used = False

        all_findings     = audit.get('findings', [])
        phase1_errors    = [f for f in all_findings if f.get('severity') in ('ERROR', 'CRITICAL')]
        phase1_warnings  = [f for f in all_findings if f.get('severity') == 'WARN']
        actionable_warns = [
            f for f in phase1_warnings
            if f.get('code', '') in LLM_ESCALATION_WARN_CODES
        ]

        llm_assist = (job.metadata or {}).get('llm_assist')
        is_reversal = '103-A3=B2' in job.input_text
        should_escalate = (
            not is_reversal
            and (len(phase1_errors) > 0 or len(actionable_warns) > 0
                 or tx_type == 'UNKNOWN' or llm_assist is True)
        ) and llm_assist is not False

        if should_escalate:
            from engine.llm_resolver import get_resolver
            from engine.llm_merger import merge_llm_decisions
            from engine.phi_masker import mask_transaction, unmask_llm_output

            resolver = get_resolver()
            if resolver.is_enabled():
                _s('llm', 8, 'LLM resolution', 'running')
                try:
                    masked_text, mask_map = await asyncio.to_thread(
                        mask_transaction, job.input_text
                    )
                    llm_findings  = phase1_errors + actionable_warns
                    raw_decisions = await resolver.resolve(
                        masked_text=masked_text,
                        errors=llm_findings,
                        tx_type=tx_type,
                    )
                    safe_decisions = unmask_llm_output(
                        [d.__dict__ for d in raw_decisions], mask_map
                    )
                    from engine.llm_resolver import LLMDecision
                    clean_decisions = [
                        LLMDecision(**{k: v for k, v in d.items()
                                       if k in LLMDecision.__dataclass_fields__})
                        for d in safe_decisions
                    ]
                    if clean_decisions:
                        f6_output, llm_audit_entries, summary_delta = merge_llm_decisions(
                            f6_output, clean_decisions
                        )
                        audit['entries'] = audit.get('entries', []) + llm_audit_entries
                        audit['summary']['modified'] = (
                            audit['summary'].get('modified', 0)
                            + summary_delta.get('modified', 0)
                        )
                        llm_decisions = clean_decisions
                        llm_used = True

                    _s('llm', 8, 'LLM resolution', 'complete',
                       f'{len(llm_decisions)} decisions')

                except Exception as exc:
                    _s('llm', 8, 'LLM resolution', 'error', str(exc)[:120])
                    self._log.error('LLM escalation failed (non-fatal): %s', exc)

        # ── Persist ───────────────────────────────────────────────────────────
        # Steps already written incrementally via _s() above; no bulk write needed.
        active_rs = db_ops.get_active_rule_set()
        db_ops.complete_conversion(
            conversion_id    = cid,
            transaction_type = tx_type,
            f6_output        = f6_output,
            summary          = audit['summary'],
            rule_set_version = active_rs['name'] if active_rs else rule_set_name,
        )
        db_ops.insert_audit_entries(cid, audit['entries'])
        db_ops.insert_audit_findings(cid, audit['findings'])

        if llm_decisions:
            db_ops.insert_llm_decisions(cid, llm_decisions)

        if job.batch_id:
            db_ops.update_batch_progress(job.batch_id)

    async def _execute_reverse(self, job: ConversionJob) -> None:
        """F6 → D.0 conversion. Runs the existing reverse pipeline in a thread."""
        rule_set_name = self.cache.rule_set_name or 'default'

        def _pipeline(input_text: str):
            import asyncio as _asyncio
            from agent.reverse_orchestrator import ReverseOrchestrator
            return _asyncio.run(ReverseOrchestrator().convert(input_text))

        result = await asyncio.to_thread(_pipeline, job.input_text)

        active_rs = db_ops.get_active_rule_set()
        db_ops.complete_conversion(
            conversion_id    = job.conversion_id,
            transaction_type = result.transaction_type,
            d0_output        = result.d0_output,
            summary          = result.audit['summary'],
            rule_set_version = active_rs['name'] if active_rs else rule_set_name,
        )
        db_ops.insert_audit_entries(job.conversion_id, result.audit['entries'])
        db_ops.insert_audit_findings(job.conversion_id, result.audit['findings'])
        db_ops.insert_agent_steps(job.conversion_id, result.agent_steps)

        if job.batch_id:
            db_ops.update_batch_progress(job.batch_id)
