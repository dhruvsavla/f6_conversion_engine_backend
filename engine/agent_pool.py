"""
engine/agent_pool.py

Pool of N ConversionAgent coroutines sharing one queue and one rule cache.

Lifecycle:
  startup  → pool.start()  → agents begin running
  request  → pool.submit() → job queued → agent picks it up
  shutdown → pool.stop()   → agents finish current jobs then exit

Environment variables (all optional):
  NCPDP_AGENT_COUNT      Number of concurrent agents (default: 8)
  NCPDP_MAX_QUEUE_DEPTH  Max jobs in queue before rejecting (default: 10000)
  NCPDP_JOB_TIMEOUT_S   Default job timeout in seconds (default: 30)
  NCPDP_CACHE_REFRESH_S  Rule cache refresh interval in seconds (default: 60)
"""

import asyncio
import logging
import os
import time
from typing import Optional

from engine.job_queue import ConversionQueue, ConversionJob, make_job, Priority
from engine.rule_cache import RuleCache, get_cache
from engine.metrics import AgentMetrics
from engine.agent import ConversionAgent

logger = logging.getLogger(__name__)

AGENT_COUNT     = int(os.environ.get('NCPDP_AGENT_COUNT',      '8'))
MAX_QUEUE_DEPTH = int(os.environ.get('NCPDP_MAX_QUEUE_DEPTH',  '10000'))
JOB_TIMEOUT_S   = float(os.environ.get('NCPDP_JOB_TIMEOUT_S',  '30'))
CACHE_REFRESH_S = float(os.environ.get('NCPDP_CACHE_REFRESH_S', '60'))


class AgentPool:
    """Manages a pool of ConversionAgent coroutines. One instance per application."""

    def __init__(self, n_agents: int = AGENT_COUNT):
        self.n_agents:    int = n_agents
        self._queue:      ConversionQueue = ConversionQueue(max_depth=MAX_QUEUE_DEPTH)
        self._cache:      RuleCache = get_cache()
        self._stop:       asyncio.Event = asyncio.Event()
        self._agents:     list[ConversionAgent] = []
        self._tasks:      list[asyncio.Task] = []
        self._metrics:    list[AgentMetrics] = []
        self._cache_task: Optional[asyncio.Task] = None
        self._started:    bool = False
        self._started_at: float = 0.0

    async def start(self) -> None:
        """Start all agents and the rule cache refresh loop. Called once on startup."""
        if self._started:
            logger.warning('AgentPool.start() called but pool already running')
            return

        logger.info(f'Starting agent pool with {self.n_agents} agents...')

        await self._cache.warm()

        for i in range(self.n_agents):
            metrics = AgentMetrics(agent_id=i)
            agent   = ConversionAgent(
                agent_id   = i,
                queue      = self._queue,
                cache      = self._cache,
                metrics    = metrics,
                stop_event = self._stop,
            )
            self._agents.append(agent)
            self._metrics.append(metrics)

        for agent in self._agents:
            task = asyncio.create_task(
                agent.run(),
                name=f'ncpdp-agent-{agent.agent_id}',
            )
            self._tasks.append(task)

        self._cache_task = asyncio.create_task(
            self._refresh_cache_loop(),
            name='ncpdp-cache-refresh',
        )

        self._started    = True
        self._started_at = time.monotonic()

        logger.info(
            f'Agent pool started: {self.n_agents} agents, '
            f'queue max depth {MAX_QUEUE_DEPTH}, '
            f'cache: {self._cache.total_rules} rules'
        )

    async def stop(self) -> None:
        """Graceful shutdown. Waits up to 30 seconds for in-flight jobs."""
        if not self._started:
            return

        logger.info('Stopping agent pool...')
        self._stop.set()

        if self._cache_task:
            self._cache_task.cancel()

        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning('Agent pool shutdown timed out — cancelling remaining tasks')
            for task in self._tasks:
                task.cancel()

        self._started = False
        logger.info('Agent pool stopped')

    async def submit(self, job: ConversionJob) -> str:
        """Submit a job to the queue. Returns job_id. Raises if pool not started."""
        if not self._started:
            raise RuntimeError('Agent pool is not running. Call pool.start() first.')
        return await self._queue.submit(job)

    async def _refresh_cache_loop(self) -> None:
        """Background task: check for rule set changes every N seconds."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(CACHE_REFRESH_S)
                reloaded = await self._cache.refresh_if_stale()
                if reloaded:
                    logger.info(f'Rule cache refreshed: {self._cache.total_rules} rules')
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'Cache refresh error: {e}', exc_info=True)

    def pool_stats(self) -> dict:
        """Return current pool status for the /api/engine/stats endpoint."""
        uptime          = time.monotonic() - self._started_at if self._started else 0
        total_processed = sum(m.jobs_processed for m in self._metrics)
        total_failed    = sum(m.jobs_failed    for m in self._metrics)
        total_timed_out = sum(m.jobs_timed_out for m in self._metrics)

        return {
            'pool': {
                'running':         self._started,
                'n_agents':        self.n_agents,
                'uptime_seconds':  round(uptime, 1),
                'total_processed': total_processed,
                'total_failed':    total_failed,
                'total_timed_out': total_timed_out,
                'error_rate_pct':  round(
                    (total_failed / total_processed * 100) if total_processed else 0, 2
                ),
            },
            'queue': self._queue.stats(),
            'cache': {
                'rule_set':    self._cache.rule_set_name,
                'total_rules': self._cache.total_rules,
                'is_warm':     self._cache.is_warm,
            },
            'agents': [m.to_dict() for m in self._metrics],
        }


_pool: Optional[AgentPool] = None


def get_pool() -> AgentPool:
    """Return the shared pool instance. Creates it if needed."""
    global _pool
    if _pool is None:
        _pool = AgentPool(n_agents=AGENT_COUNT)
    return _pool
