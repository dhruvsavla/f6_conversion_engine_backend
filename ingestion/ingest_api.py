"""
ingestion/ingest_api.py

FastAPI router that exposes the ingestion pipeline over HTTP so the React
frontend can upload a PDF and track extraction progress without touching the CLI.

Endpoints:
  POST /api/ingest/upload          — upload PDF, start background job
  GET  /api/ingest/status/{job_id} — poll job state
  GET  /api/ingest/review          — diff ingestion_output/ vs rules/
  POST /api/ingest/promote         — copy ingestion_output/ to rules/
  GET  /api/ingest/flagged         — return flagged_for_review.json contents
"""

from __future__ import annotations

import collections
import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Load ANTHROPIC_API_KEY from .env before any anthropic import
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from ingestion.extractor.chunker import Chunker
from ingestion.extractor.llm_extractor import LLMExtractor
from ingestion.extractor.pdf_loader import PDFLoader
from ingestion.extractor.rule_compiler import RuleCompiler
from ingestion.output.rule_writer import OUTPUT_DIR, FLAGGED_FILE, RuleWriter
from ingestion.review.diff_reporter import DiffReporter
from ingestion.validator.rule_validator import RuleValidator
import db_ops

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/ingest', tags=['ingestion'])

RESOLUTION_LOG = OUTPUT_DIR / 'resolution_log.json'

# In-memory job store — same pattern as batch_processor.BATCH_JOBS
INGEST_JOBS: dict[str, dict[str, Any]] = {}


# ── Resolution helpers ────────────────────────────────────────────────────────

def _read_flagged() -> list[dict]:
    if not FLAGGED_FILE.exists():
        return []
    try:
        data = json.loads(FLAGGED_FILE.read_text(encoding='utf-8'))
        for i, entry in enumerate(data):
            if '_id' not in entry:
                entry['_id'] = str(uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{entry.get('segment_id','')}-{entry.get('rule',{}).get('field_id','')}-{i}"
                ))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(500, f'Failed to read flagged rules file: {e}')


def _write_flagged(entries: list[dict]) -> None:
    FLAGGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAGGED_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding='utf-8')


def _append_resolution_log(entry: dict) -> None:
    existing = []
    if RESOLUTION_LOG.exists():
        try:
            existing = json.loads(RESOLUTION_LOG.read_text())
        except Exception:
            pass
    existing.append({**entry, 'resolved_at': datetime.now(timezone.utc).isoformat()})
    RESOLUTION_LOG.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding='utf-8')


def _find_entry(entries: list[dict], entry_id: str) -> tuple[int, Optional[dict]]:
    for i, e in enumerate(entries):
        if e.get('_id') == entry_id:
            return i, e
    return -1, None


def _is_auto_fixable(issues: list[str]) -> bool:
    return bool(issues) and all('WARN' in i and 'INVALID' not in i for i in issues)


# ── Background task ───────────────────────────────────────────────────────────

def _run_ingestion(
    job_id: str,
    pdf_path: str,
    transaction_type: str,
    segment_filter: str | None,
    dry_run: bool,
    model: str,
) -> None:
    """
    Runs the full 6-step pipeline in the background.
    Mutates INGEST_JOBS[job_id] so the frontend polling sees live updates.
    Deletes the temp PDF file when finished regardless of outcome.
    """
    job = INGEST_JOBS[job_id]

    def _update(step: int, label: str, progress: int, **kw: Any) -> None:
        job.update(current_step=step, step_label=label, progress=progress, **kw)

    try:
        # Step 1+2: Load PDF and extract text
        _update(1, 'Loading PDF and extracting text…', 5)
        loader = PDFLoader()
        doc = loader.load(pdf_path)
        job['pdf_pages']   = doc.total_pages
        job['token_estimate'] = doc.total_tokens_estimate
        _update(2, 'Text extracted', 15, pdf_pages=doc.total_pages,
                token_estimate=doc.total_tokens_estimate)

        # Step 3: Chunk by segment
        _update(3, 'Chunking by segment…', 20)
        chunker    = Chunker()
        all_chunks = chunker.chunk(doc, target_segment=segment_filter or None)

        chunks_by_seg: dict[str, list] = collections.defaultdict(list)
        for c in all_chunks:
            chunks_by_seg[c.segment_id].append(c)

        job['segments_found'] = [
            {
                'segment_id': sid,
                'chunk_count': len(clist),
                'page_start': clist[0].page_start,
                'page_end':   clist[-1].page_end,
            }
            for sid, clist in chunks_by_seg.items()
        ]
        _update(3, 'Chunking complete', 25)

        # Step 4: LLM extraction (one segment group at a time)
        extractor = LLMExtractor(model=model)
        compiler  = RuleCompiler()
        seg_count = len(chunks_by_seg)
        compiled_by_seg: dict[str, list[dict]] = {}
        extraction_progress: list[dict] = []

        for i, (seg_id, seg_chunks) in enumerate(chunks_by_seg.items()):
            _update(4, f'Extracting {seg_id} rules via LLM…',
                    25 + int((i / seg_count) * 45))
            try:
                raw_rules = extractor.extract_segment(seg_chunks, transaction_type)
                compiled  = compiler.compile_all(raw_rules, seg_id)
            except Exception as e:
                logger.warning('LLM extraction failed for %s: %s', seg_id, e)
                compiled = []

            compiled_by_seg[seg_id] = compiled
            extraction_progress.append({'segment_id': seg_id, 'rule_count': len(compiled)})
            job['extraction_progress'] = extraction_progress

        _update(4, 'LLM extraction complete', 70)

        # Step 5: Validate
        _update(5, 'Validating rules…', 75)
        validator   = RuleValidator()
        all_results = []
        for seg_id, rules in compiled_by_seg.items():
            all_results.extend(validator.validate_all(rules, seg_id))

        valid_count   = sum(1 for r in all_results if r.status == 'VALID')
        warn_count    = sum(1 for r in all_results if r.status == 'WARN')
        invalid_count = sum(1 for r in all_results if r.status == 'INVALID')
        job['validation'] = {
            'total': len(all_results),
            'valid': valid_count,
            'warn':  warn_count,
            'invalid': invalid_count,
        }
        _update(5, 'Validation complete', 85)

        # Step 6: Write output (skip on dry run)
        if dry_run:
            job['dry_run_rules'] = [
                {
                    'segment_id': r.segment_id,
                    'field_id':   r.rule.get('field_id', ''),
                    'field_name': r.rule.get('field_name', ''),
                    'action':     r.rule.get('action', ''),
                    'status':     r.status,
                    'issues':     r.issues,
                }
                for r in all_results
            ]
            _update(6, 'Dry run — no files written', 100, status='completed')
        else:
            _update(6, 'Writing output files…', 90)
            writer   = RuleWriter()
            manifest = writer.write(all_results, transaction_type, source_pdf=pdf_path)
            job['files_written'] = manifest['files_written']
            job['flagged_file']  = manifest.get('flagged_file')
            _update(6, 'Output written', 100, status='completed')

    except Exception as e:
        logger.exception('Ingestion job %s failed: %s', job_id, e)
        job['status']     = 'error'
        job['step_label'] = f'Error: {e}'
        job['progress']   = 0
    finally:
        # Always clean up the temp PDF regardless of success or failure
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except OSError:
            pass


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post('/upload')
async def upload_pdf(
    background_tasks: BackgroundTasks,
    pdf: UploadFile = File(...),
    transaction_type: str  = Form(default='RETAIL'),
    segment:          str  = Form(default=''),
    dry_run:          bool = Form(default=False),
    model:            str  = Form(default='claude-sonnet-4-6'),
):
    """
    Accept a PDF upload and start an extraction job in the background.
    Returns immediately with {job_id} for the frontend to poll.
    """
    if not pdf.filename or not pdf.filename.lower().endswith('.pdf'):
        raise HTTPException(400, 'Uploaded file must be a .pdf')

    # Save the PDF to a temp file — pdfplumber needs a file path, not a stream
    suffix = f'_{pdf.filename}'
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        content = await pdf.read()
        import os
        os.write(tmp_fd, content)
        os.close(tmp_fd)
    except Exception as e:
        raise HTTPException(500, f'Failed to save uploaded file: {e}')

    job_id = str(uuid.uuid4())
    INGEST_JOBS[job_id] = {
        'status':               'running',
        'current_step':         0,
        'step_label':           'Queued…',
        'progress':             0,
        'pdf_name':             pdf.filename,
        'pdf_pages':            None,
        'token_estimate':       None,
        'transaction_type':     transaction_type,
        'segment_filter':       segment or None,
        'dry_run':              dry_run,
        'model':                model,
        'segments_found':       [],
        'extraction_progress':  [],
        'validation':           None,
        'files_written':        [],
        'flagged_file':         None,
        'dry_run_rules':        None,
        'error':                None,
    }

    background_tasks.add_task(
        _run_ingestion,
        job_id, tmp_path, transaction_type,
        segment or None, dry_run, model,
    )

    return {'job_id': job_id, 'pdf_name': pdf.filename}


@router.get('/status/{job_id}')
async def get_status(job_id: str):
    """Return the current state of an ingestion job."""
    job = INGEST_JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"Ingestion job '{job_id}' not found.")
    return job


@router.get('/review')
async def review():
    """Return the diff between ingestion_output/ and rules/ as plain text."""
    reporter = DiffReporter()
    return {'diff': reporter.report()}


@router.post('/promote')
async def promote():
    """Promote all files from ingestion_output/ to rules/."""
    writer = RuleWriter()
    try:
        # force=True because the user already reviewed and clicked Promote in the UI
        promoted = writer.promote(force=True)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    return {'promoted': promoted, 'count': len(promoted)}


@router.get('/flagged')
def get_flagged():
    """Return all flagged rules enriched with current re-validation state."""
    entries  = _read_flagged()
    validator = RuleValidator()
    enriched = []
    for entry in entries:
        rule      = entry.get('rule', {})
        seg_id    = entry.get('segment_id', 'UNKNOWN')
        issues    = entry.get('issues', [])
        recheck   = validator.validate(rule, seg_id)
        enriched.append({
            **entry,
            '_issue_count':    len(issues),
            '_has_invalid':    any('INVALID' in i for i in issues),
            '_current_status': recheck.status,
            '_current_issues': recheck.issues,
            '_auto_fixable':   _is_auto_fixable(issues),
        })
    return {
        'flagged':         enriched,
        'total':           len(enriched),
        'invalid_count':   sum(1 for e in enriched if e['_has_invalid']),
        'warn_only_count': sum(1 for e in enriched if not e['_has_invalid']),
        'file_path':       str(FLAGGED_FILE),
        'file_exists':     FLAGGED_FILE.exists(),
    }


@router.get('/flagged/count')
def get_flagged_count():
    """Lightweight count for sidebar badge — no validation re-run."""
    entries = _read_flagged()
    return {'total': len(entries), 'has_flagged': len(entries) > 0}


@router.post('/resolve')
def resolve_rule(body: dict = Body(...)):
    """
    Approve (re-validate + merge) or reject one flagged rule.
    Returns 422 if approved rule still fails validation.
    """
    entry_id   = body.get('entry_id', '')
    resolution = body.get('resolution', '')
    if resolution not in ('approve', 'reject'):
        raise HTTPException(400, 'resolution must be "approve" or "reject"')

    entries = _read_flagged()
    idx, entry = _find_entry(entries, entry_id)
    if idx == -1:
        raise HTTPException(404, f'Flagged rule "{entry_id}" not found')

    if resolution == 'reject':
        reason  = body.get('rejection_reason', 'No reason provided')
        removed = entries.pop(idx)
        _write_flagged(entries)
        _append_resolution_log({
            'resolution':       'reject',
            'entry_id':         entry_id,
            'field_id':         removed.get('rule', {}).get('field_id', ''),
            'segment_id':       removed.get('segment_id', ''),
            'rejection_reason': reason,
        })
        return {'status': 'rejected', 'message': 'Rule rejected and removed.', 'remaining': len(entries)}

    # Approve flow
    corrected_rule   = body.get('corrected_rule')
    segment_id       = body.get('segment_id', entry.get('segment_id', ''))
    transaction_type = body.get('transaction_type', 'RETAIL')

    if corrected_rule is None:
        raise HTTPException(400, 'corrected_rule is required for approve')

    validator = RuleValidator()
    result    = validator.validate(corrected_rule, segment_id)

    if result.status == 'INVALID':
        return JSONResponse(status_code=422, content={
            'status':  'validation_failed',
            'message': 'The corrected rule still has validation errors.',
            'issues':  result.issues,
        })

    active_rs = db_ops.get_active_rule_set()
    if not active_rs:
        raise HTTPException(500, 'No active rule set in DB. Cannot merge.')

    db_ops.insert_rules_bulk(active_rs['id'], [{
        **corrected_rule,
        'transaction_type': transaction_type,
        'segment_id':       segment_id,
    }])

    removed = entries.pop(idx)
    _write_flagged(entries)
    _append_resolution_log({
        'resolution':       'approve',
        'entry_id':         entry_id,
        'field_id':         corrected_rule.get('field_id', ''),
        'segment_id':       segment_id,
        'transaction_type': transaction_type,
        'validator_status': result.status,
        'issues_at_merge':  result.issues,
    })

    suffix = ' (merged with warnings)' if result.issues else ''
    return {
        'status':           'approved',
        'message':          f'Rule {corrected_rule.get("field_id","")} merged into "{active_rs["name"]}"{suffix}.',
        'validator_status': result.status,
        'warnings':         result.issues,
        'merged_into':      active_rs['name'],
        'remaining':        len(entries),
    }


@router.post('/resolve-all')
def resolve_all_auto(body: dict = Body(...)):
    """Batch-approve all flagged rules that currently pass re-validation (WARN-only or VALID)."""
    transaction_type = body.get('transaction_type', 'RETAIL')
    entries   = _read_flagged()
    validator = RuleValidator()
    active_rs = db_ops.get_active_rule_set()
    if not active_rs:
        raise HTTPException(500, 'No active rule set in DB.')

    approved  = []
    remaining = []
    for entry in entries:
        rule      = entry.get('rule', {})
        segment_id = entry.get('segment_id', 'UNKNOWN')
        result    = validator.validate(rule, segment_id)
        if result.status in ('VALID', 'WARN'):
            db_ops.insert_rules_bulk(active_rs['id'], [{
                **rule, 'transaction_type': transaction_type, 'segment_id': segment_id,
            }])
            approved.append(entry.get('_id', ''))
            _append_resolution_log({
                'resolution':       'approve_auto',
                'entry_id':         entry.get('_id', ''),
                'field_id':         rule.get('field_id', ''),
                'segment_id':       segment_id,
                'transaction_type': transaction_type,
                'validator_status': result.status,
            })
        else:
            remaining.append(entry)

    _write_flagged(remaining)
    return {
        'status':    'batch_complete',
        'approved':  len(approved),
        'remaining': len(remaining),
        'message':   f'Auto-approved {len(approved)} rule(s). {len(remaining)} still need manual review.',
    }


@router.get('/history')
def get_resolution_history(limit: int = 50):
    """Return past approve/reject decisions from resolution_log.json."""
    if not RESOLUTION_LOG.exists():
        return {'history': [], 'total': 0}
    try:
        history = json.loads(RESOLUTION_LOG.read_text())
        history.sort(key=lambda x: x.get('resolved_at', ''), reverse=True)
        return {'history': history[:limit], 'total': len(history)}
    except Exception as e:
        raise HTTPException(500, f'Failed to read resolution log: {e}')


@router.delete('/flagged')
def clear_flagged(confirm: str = ''):
    """Clear all flagged rules. Requires ?confirm=yes to prevent accidents."""
    if confirm != 'yes':
        raise HTTPException(400, 'Pass ?confirm=yes to clear all flagged rules')
    entries = _read_flagged()
    for entry in entries:
        _append_resolution_log({
            'resolution': 'cleared',
            'entry_id':   entry.get('_id', ''),
            'field_id':   entry.get('rule', {}).get('field_id', ''),
            'segment_id': entry.get('segment_id', ''),
        })
    _write_flagged([])
    return {'status': 'cleared', 'removed': len(entries)}
