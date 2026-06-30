"""
engine/job_queue.py

Priority queue for conversion jobs.

Priority levels (lower number = higher priority):
  0 = URGENT  — reversals (B2), eligibility (E1), real-time single claims
  1 = NORMAL  — standard single D.0→F6 or F6→D.0 conversions
  2 = BULK    — batch items (can wait behind urgent/normal work)
"""

import asyncio
import time
import uuid
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from engine.exceptions import MaxQueueDepthError

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    URGENT = 0   # reversals, eligibility — must complete fast
    NORMAL = 1   # standard single conversions
    BULK   = 2   # batch items — lower priority than individual requests


@dataclass(order=True)
class ConversionJob:
    """
    A single unit of work for one agent.

    Fields are in comparison order — (priority, submitted_at) first so the
    priority queue breaks ties by arrival time (FIFO within same priority).
    """
    priority:              int
    submitted_at:          float
    job_id:                str   = field(compare=False)
    conversion_id:         str   = field(compare=False)
    direction:             str   = field(compare=False)   # D0_TO_F6 | F6_TO_D0
    input_text:            str   = field(compare=False)
    batch_id:              Optional[str] = field(compare=False, default=None)
    supplier_id:           Optional[str] = field(compare=False, default=None)
    transaction_type_hint: Optional[str] = field(compare=False, default=None)
    timeout_seconds:       float = field(compare=False, default=30.0)
    metadata:              dict  = field(compare=False, default_factory=dict)


@dataclass
class JobResult:
    job_id:        str
    conversion_id: str
    success:       bool
    agent_id:      int
    duration_ms:   float
    error:         Optional[str] = None
    warnings:      int = 0
    errors:        int = 0


class ConversionQueue:
    """Thread-safe priority queue with job tracking and depth control."""

    def __init__(self, max_depth: int = 10_000):
        self._queue:   asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._max:     int = max_depth
        self._pending: dict[str, ConversionJob] = {}
        self._lock:    asyncio.Lock = asyncio.Lock()

    async def submit(self, job: ConversionJob) -> str:
        """Submit a job. Raises MaxQueueDepthError if queue is full. Returns job_id."""
        async with self._lock:
            if self._queue.qsize() >= self._max:
                raise MaxQueueDepthError(
                    f'Queue at capacity ({self._max}). '
                    f'Try again or increase NCPDP_MAX_QUEUE_DEPTH.'
                )
            self._pending[job.job_id] = job

        await self._queue.put(job)
        logger.debug(
            f'Job {job.job_id[:8]} queued '
            f'(priority={job.priority}, depth={self._queue.qsize()})'
        )
        return job.job_id

    async def get(self) -> ConversionJob:
        """Block until a job is available. Called by agents."""
        return await self._queue.get()

    def task_done(self, job_id: str):
        """Called by agent after processing (success or failure)."""
        self._pending.pop(job_id, None)
        self._queue.task_done()

    def depth(self) -> int:
        return self._queue.qsize()

    def pending_count(self) -> int:
        return len(self._pending)

    def stats(self) -> dict:
        by_priority = {0: 0, 1: 0, 2: 0}
        for job in self._pending.values():
            by_priority[job.priority] = by_priority.get(job.priority, 0) + 1
        return {
            'depth':   self._queue.qsize(),
            'pending': len(self._pending),
            'urgent':  by_priority[0],
            'normal':  by_priority[1],
            'bulk':    by_priority[2],
        }


def make_job(
    conversion_id: str,
    input_text:    str,
    direction:     str = 'D0_TO_F6',
    batch_id:      Optional[str] = None,
    supplier_id:   Optional[str] = None,
    priority:      Priority = Priority.NORMAL,
    timeout:       float = 30.0,
    metadata:      dict = None,
) -> ConversionJob:
    """Factory for ConversionJob with auto-priority from direction."""
    if direction == 'D0_TO_F6':
        if '103-A3=B2' in input_text or '|B2|' in input_text:
            priority = Priority.URGENT
    if batch_id is not None:
        priority = Priority.BULK

    return ConversionJob(
        priority=int(priority),
        submitted_at=time.monotonic(),
        job_id=str(uuid.uuid4()),
        conversion_id=conversion_id,
        direction=direction,
        input_text=input_text,
        batch_id=batch_id,
        supplier_id=supplier_id,
        timeout_seconds=timeout,
        metadata=metadata or {},
    )
