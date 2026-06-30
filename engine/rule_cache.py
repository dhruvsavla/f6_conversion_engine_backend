"""
engine/rule_cache.py

Shared in-memory rule cache for the agent pool.

Loads all rules for the active rule set once from the DB, keyed by
(transaction_type, segment_id). Agents call get_rules() synchronously
during conversion — no DB access per conversion.

Cache refreshes when the active rule set changes (detected by polling
every RULE_SET_CHECK_INTERVAL seconds).
"""

import asyncio
import json
import logging
import time
from typing import Optional

import db_ops

logger = logging.getLogger(__name__)

RULE_SET_CHECK_INTERVAL = 60.0

SEGMENT_ORDER = [
    'HDR', 'PAT', 'INS', 'CLM', 'PRE', 'PRI', 'DUR',
    'COB', 'CMP', 'PA', 'CLN', 'WRK', 'FAC', 'NAR',
]


class RuleCache:
    """
    Shared cache of rules from the active rule set.

    Usage:
        cache = RuleCache()
        await cache.warm()                          # load on startup
        rules = cache.get_rules('RETAIL', 'CLM')   # instant lookup
        await cache.refresh_if_stale()             # called periodically
    """

    def __init__(self):
        self._lock:          asyncio.Lock = asyncio.Lock()
        self._rules:         dict         = {}   # (tx_type, seg_id) → list[rule_dict]
        self._rule_set_id:   Optional[str] = None
        self._rule_set_name: str           = ''
        self._loaded_at:     float         = 0.0
        self._total_rules:   int           = 0

    async def warm(self) -> None:
        """Load all rules from active rule set into memory. Called at startup."""
        async with self._lock:
            await self._load()
        logger.info(
            f'Rule cache warmed: {self._total_rules} rules '
            f'from "{self._rule_set_name}"'
        )

    async def refresh_if_stale(self) -> bool:
        """
        Check if rule set has changed. Reload if it has.
        Returns True if reloaded.
        """
        active = db_ops.get_active_rule_set()
        if not active:
            return False
        if active['id'] == self._rule_set_id:
            return False

        logger.info(
            f'Rule set changed: "{self._rule_set_name}" → "{active["name"]}". Reloading...'
        )
        async with self._lock:
            await self._load()
        logger.info(f'Rule cache reloaded: {self._total_rules} rules')
        return True

    async def force_reload(self) -> None:
        """Force immediate reload regardless of whether rule set changed."""
        async with self._lock:
            await self._load()
        logger.info(f'Rule cache force-reloaded: {self._total_rules} rules from "{self._rule_set_name}"')

    async def _load(self) -> None:
        """Load rules from DB into cache. Must be called with lock held."""
        active = db_ops.get_active_rule_set()
        if not active:
            logger.warning('No active rule set found. Cache will be empty.')
            return

        self._rule_set_id   = active['id']
        self._rule_set_name = active['name']
        self._rules         = {}
        self._total_rules   = 0
        self._loaded_at     = time.monotonic()

        from database import db
        with db() as conn:
            rows = conn.execute("""
                SELECT transaction_type, segment_id, rule_json
                FROM rules
                WHERE rule_set_id = ?
                ORDER BY transaction_type, segment_id, field_id
            """, (self._rule_set_id,)).fetchall()

        for row in rows:
            key = (row['transaction_type'], row['segment_id'])
            if key not in self._rules:
                self._rules[key] = []
            self._rules[key].append(json.loads(row['rule_json']))
            self._total_rules += 1

        logger.debug(
            f'Loaded {self._total_rules} rules '
            f'({len(self._rules)} (tx_type, segment) combos)'
        )

    def get_rules(self, transaction_type: str, segment_id: str) -> list[dict]:
        """Instant synchronous lookup. Returns [] if no rules for this combo."""
        return self._rules.get((transaction_type, segment_id), [])

    def get_all_segments_for_type(self, transaction_type: str) -> list[str]:
        """Return all segment IDs that have rules for a given transaction type."""
        segs    = [k[1] for k in self._rules if k[0] == transaction_type]
        ordered = [s for s in SEGMENT_ORDER if s in segs]
        others  = [s for s in segs if s not in SEGMENT_ORDER]
        return ordered + others

    @property
    def rule_set_name(self) -> str:
        return self._rule_set_name

    @property
    def total_rules(self) -> int:
        return self._total_rules

    @property
    def is_warm(self) -> bool:
        return self._total_rules > 0


_cache: Optional[RuleCache] = None


def get_cache() -> RuleCache:
    global _cache
    if _cache is None:
        _cache = RuleCache()
    return _cache
