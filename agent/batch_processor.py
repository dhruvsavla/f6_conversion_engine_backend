"""
backend/agent/batch_processor.py

Local, in-memory batch processor for multi-claim files.
Uses FastAPI BackgroundTasks — no Celery, Redis, or external queue needed.

Claim separation: double newline (\n\n) in pipe format.
One failed claim does not abort the rest of the batch.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from . import f6_assembler, field_mapper, rules_reader, segment_parser, transaction_detector

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent.parent / "rules"

# In-memory job store. Keyed by UUID string.
# Lives for the lifetime of the process — no persistence across restarts.
BATCH_JOBS: dict[str, dict[str, Any]] = {}


def start_batch_job(raw_text: str) -> str:
    """
    Reserve a job slot and return its UUID immediately.
    Called synchronously inside the HTTP handler so the ID is available
    before the background task even starts.
    """
    job_id = str(uuid.uuid4())
    BATCH_JOBS[job_id] = {
        "status": "pending",
        "progress": 0,       # 0-100 integer, updated after every claim
        "total": 0,
        "successful": 0,
        "failed": 0,
        "errors": [],        # per-claim error messages for debugging
    }
    return job_id


def process_batch_background(job_id: str, raw_text: str) -> None:
    """
    Executed by FastAPI BackgroundTasks after the HTTP response is sent.
    Mutates BATCH_JOBS[job_id] in place so polling clients see live updates.
    """
    job = BATCH_JOBS[job_id]
    job["status"] = "processing"

    # Load the ruleset once up front — reloading per claim would be very slow
    try:
        ruleset = rules_reader.load_all_from_db()
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"Failed to load rules: {e}")
        logger.error("Batch %s: rule load failed: %s", job_id, e)
        return

    # Normalize line endings first — pasted text often carries \r\n (Windows),
    # which turns the blank-line separator into \r\n\r\n and defeats split("\n\n")
    normalized = raw_text.replace('\r\n', '\n').replace('\r', '\n')
    chunks = [c.strip() for c in normalized.split("\n\n") if c.strip()]
    total = len(chunks)
    job["total"] = total

    if total == 0:
        job["status"] = "completed"
        return

    for i, chunk in enumerate(chunks):
        try:
            parsed   = segment_parser.parse_d0(chunk)
            tx_type  = transaction_detector.detect(parsed, ruleset)
            tx_rules = ruleset.get_rules_for(tx_type)
            mapping  = field_mapper.map_fields(parsed, tx_rules)
            f6_assembler.assemble(mapping)   # validates end-to-end; output discarded
            job["successful"] += 1
        except Exception as e:
            job["failed"] += 1
            # Keep error messages bounded — truncate after 200 chars so the job
            # dict doesn't grow unboundedly on a batch with many bad claims
            job["errors"].append(f"Claim {i + 1}: {str(e)[:200]}")
            logger.warning("Batch %s claim %d/%d failed: %s", job_id, i + 1, total, e)

        # Progress is an integer 0-100, updated every claim so the frontend
        # always has a fresh value regardless of poll timing
        job["progress"] = int((i + 1) / total * 100)

    job["status"] = "completed"
