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
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load ANTHROPIC_API_KEY from .env before any anthropic import
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File, Form

from ingestion.extractor.chunker import Chunker
from ingestion.extractor.llm_extractor import LLMExtractor
from ingestion.extractor.pdf_loader import PDFLoader
from ingestion.extractor.rule_compiler import RuleCompiler
from ingestion.output.rule_writer import OUTPUT_DIR, FLAGGED_FILE, RuleWriter
from ingestion.review.diff_reporter import DiffReporter
from ingestion.validator.rule_validator import RuleValidator

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/ingest', tags=['ingestion'])

# In-memory job store — same pattern as batch_processor.BATCH_JOBS
INGEST_JOBS: dict[str, dict[str, Any]] = {}


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
async def get_flagged():
    """Return the contents of flagged_for_review.json if it exists."""
    if not FLAGGED_FILE.exists():
        return {'flagged': []}
    try:
        data = json.loads(FLAGGED_FILE.read_text(encoding='utf-8'))
        return {'flagged': data}
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(500, f'Could not read flagged file: {e}')
