"""
engine/metrics.py

Per-agent metrics: throughput, latency percentiles, error rate.
"""

import time
import statistics
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentMetrics:
    agent_id:           int
    jobs_processed:     int   = 0
    jobs_succeeded:     int   = 0
    jobs_failed:        int   = 0
    jobs_timed_out:     int   = 0
    total_duration_ms:  float = 0.0
    durations_ms:       list  = field(default_factory=list)
    started_at:         float = field(default_factory=time.monotonic)
    last_job_at:        float = 0.0
    current_job_id:     Optional[str] = None

    _MAX_SAMPLES = 1000

    def record(self, duration_ms: float, success: bool, timed_out: bool = False):
        self.jobs_processed    += 1
        self.total_duration_ms += duration_ms
        self.last_job_at        = time.monotonic()
        self.current_job_id     = None

        if timed_out:
            self.jobs_timed_out += 1
        elif success:
            self.jobs_succeeded += 1
        else:
            self.jobs_failed    += 1

        self.durations_ms.append(duration_ms)
        if len(self.durations_ms) > self._MAX_SAMPLES:
            self.durations_ms = self.durations_ms[-self._MAX_SAMPLES:]

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.jobs_processed if self.jobs_processed else 0.0

    @property
    def p50_ms(self) -> float:
        return statistics.median(self.durations_ms) if self.durations_ms else 0.0

    @property
    def p95_ms(self) -> float:
        if not self.durations_ms:
            return 0.0
        s = sorted(self.durations_ms)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    @property
    def p99_ms(self) -> float:
        if not self.durations_ms:
            return 0.0
        s = sorted(self.durations_ms)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

    @property
    def error_rate(self) -> float:
        return self.jobs_failed / self.jobs_processed if self.jobs_processed else 0.0

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def to_dict(self) -> dict:
        return {
            'agent_id':        self.agent_id,
            'jobs_processed':  self.jobs_processed,
            'jobs_succeeded':  self.jobs_succeeded,
            'jobs_failed':     self.jobs_failed,
            'jobs_timed_out':  self.jobs_timed_out,
            'avg_duration_ms': round(self.avg_duration_ms, 1),
            'p50_ms':          round(self.p50_ms, 1),
            'p95_ms':          round(self.p95_ms, 1),
            'p99_ms':          round(self.p99_ms, 1),
            'error_rate_pct':  round(self.error_rate * 100, 2),
            'uptime_seconds':  round(self.uptime_seconds, 1),
            'is_busy':         self.current_job_id is not None,
            'current_job_id':  self.current_job_id,
        }
