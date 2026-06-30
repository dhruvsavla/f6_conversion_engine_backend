"""
engine/llm_resolver.py

Calls Claude to resolve NCPDP D.0 → F6 conversion issues.
Invoked ONLY when the deterministic engine escalates:
  - errors in findings
  - missing mandatory fields
  - UNKNOWN transaction type
  - explicit llm_assist=true metadata

Financial fields are always rejected. LOW confidence → UNRESOLVABLE.
API key is read from ANTHROPIC_API_KEY env var — never logged.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

_MODEL   = "claude-sonnet-4-6"
_API_URL = "https://api.anthropic.com/v1/messages"
_RPM     = int(os.environ.get("NCPDP_LLM_RPM", "50"))

# Financial fields that must never be modified by the LLM
FINANCIAL_FIELD_IDS: frozenset[str] = frozenset({
    "409-D9",   # Ingredient Cost Submitted
    "412-DC",   # Dispensing Fee Submitted
    "426-DQ",   # Patient Paid Amount Submitted
    "430-DU",   # Gross Amount Due / Usual and Customary Charge
    "423-DN",   # Other Amount Claimed
    "431-DV",   # Other Payer Amount Paid (COB)
    "442-E7",   # Quantity Dispensed
    "D5",       # Legacy alias — Ingredient Cost
    "D6",       # Legacy alias — Dispensing Fee
    "DQ",       # Legacy alias — Patient Paid Amount
    "D9",       # Legacy alias — Gross Amount Due
    "DU",       # Legacy alias — Usual and Customary
})


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class LLMDecision:
    field_id:       str
    field_name:     str
    segment_id:     str
    resolved_value: str
    original_value: str
    reasoning:      str
    confidence:     str    # HIGH | MEDIUM | LOW
    finding_code:   str
    action:         str    # RESOLVED | UNRESOLVABLE | INFERRED
    phi_was_masked: bool = True


# ── Rate limiter ───────────────────────────────────────────────────────────────

class LLMRateLimiter:
    """Token-bucket: allows `rpm` calls per 60 seconds."""

    def __init__(self, rpm: int = 50):
        self._rpm        = rpm
        self._tokens     = float(rpm)
        self._last       = time.monotonic()
        self._lock       = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now     = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(
                float(self._rpm),
                self._tokens + elapsed * (self._rpm / 60.0),
            )
            self._last = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / (self._rpm / 60.0)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ── Resolver ───────────────────────────────────────────────────────────────────

class LLMResolver:

    def __init__(self):
        self._limiter = LLMRateLimiter(rpm=_RPM)

    @staticmethod
    def is_enabled() -> bool:
        return os.environ.get("NCPDP_LLM_ENABLED", "true").lower() not in ("false", "0", "no")

    @staticmethod
    def _api_key() -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        return key

    @staticmethod
    def _build_prompt(masked_text: str, errors: list[dict], tx_type: str) -> str:
        error_lines = "\n".join(
            f"  - Field {e.get('field_id','?')} ({e.get('field_name','?')}) "
            f"in segment {e.get('segment','?')}: {e.get('message','?')} "
            f"[code={e.get('code','?')}]"
            for e in errors[:20]
        )

        return f"""You are an NCPDP D.0 to F6 medical billing conversion specialist.

Transaction type: {tx_type}

The deterministic conversion engine encountered the following issues:
{error_lines}

Source NCPDP D.0 transaction (PHI has been masked for HIPAA compliance):
<transaction>
{masked_text[:4000]}
</transaction>

Resolve each issue using NCPDP D.0 standards. Return a JSON array only — no other text.

Each element must be an object with these exact keys:
  "field_id"       — NCPDP field identifier (e.g. "310-CA")
  "field_name"     — human-readable name
  "segment_id"     — segment code (e.g. "CLM", "PAT", "INS")
  "resolved_value" — your proposed value, or "" if unresolvable
  "original_value" — what was in the source (or "")
  "reasoning"      — 1–2 sentences
  "confidence"     — "HIGH", "MEDIUM", or "LOW"
  "finding_code"   — the code from the issue list above (or "")
  "action"         — "RESOLVED", "UNRESOLVABLE", or "INFERRED"

Hard rules you must follow:
1. NEVER set resolved_value for financial fields (ingredient cost, dispensing fee, patient paid, gross amount due, usual and customary charge).
2. Set action to "UNRESOLVABLE" and resolved_value to "" whenever confidence is "LOW".
3. Do not hallucinate values — only infer from what the source transaction contains.
4. Do not reference PHI tokens (e.g. [PHI_...]) in resolved_value.

--- DOMAIN KNOWLEDGE ---

CONTROLLED SUBSTANCE KNOWLEDGE (finding code DEA):
When a DEA finding is present, inspect field 402-D2 (NDC) to determine drug schedule.

Schedule II — DEA REQUIRED, strict enforcement:
  Opioids: oxycodone (00591-0503, 00228-2880), hydrocodone combinations (00603-3880),
           fentanyl patches (00406-3510), morphine (00054-0199), methadone (00054-8554)
  Stimulants: amphetamine/Adderall (00555-0766), methylphenidate (00028-0016),
              lisdexamfetamine/Vyvanse (59148-0006)

Schedule III-V — DEA REQUIRED, less strict:
  Buprenorphine (00054-0177), testosterone (00009-0348), pregabalin/Lyrica (00071-1014),
  tramadol (52959-0694), diazepam/Valium (00140-0006), alprazolam/Xanax (00009-0029),
  zolpidem/Ambien (00024-5401), carisoprodol/Soma (00037-2001)

NOT controlled — DEA NOT required:
  Statins: atorvastatin (00071-0155, 00093-7194, labeler 00071), rosuvastatin (00310-0272)
  ACE inhibitors, ARBs, beta blockers, SSRIs, antibiotics, metformin, levothyroxine,
  omeprazole, amlodipine — none of these are scheduled.

If NDC matches or resembles a controlled substance: return action "RESOLVED", resolved_value "",
  reasoning explaining DEA is required and the finding is confirmed as an ERROR.
If NDC is clearly not controlled (e.g. atorvastatin, a statin): return action "RESOLVED",
  resolved_value "NOT_CONTROLLED", confidence "HIGH", reasoning explaining why DEA is not needed.

ADJUDICATED PROGRAM TYPE (finding code APT, field C47-9T in COB segment):
Infer from transaction context:
  - Group ID (301-C1) contains "MEDICARE" or Medicare Part D indicator (694-ZJ="Y")
    or patient age 65+ from DOB → value "1" (Medicare Part D), confidence HIGH
  - Group ID contains "MEDICAID" or "MCD" → value "2" (Medicaid), confidence HIGH
  - Other commercial indicators → value "3" (Commercial), confidence MEDIUM
  - Unknown / no indicators → UNRESOLVABLE

NDC KNOWLEDGE (finding code NDC):
When NDC (402-D2) is fewer than 11 digits, attempt to identify correct 11-digit form.
  - 6 digits with no padding: likely only the labeler code — cannot reconstruct full NDC
  - 9 digits: missing 2 digits — cannot determine which section is truncated
  In these cases: action "UNRESOLVABLE", explain why full NDC cannot be reconstructed.
  NEVER guess a full NDC — an incorrect NDC could cause the wrong drug to be dispensed.

Return ONLY the JSON array."""

    async def resolve(
        self,
        masked_text: str,
        errors:      list[dict],
        tx_type:     str,
    ) -> list[LLMDecision]:
        if not self.is_enabled() or not errors:
            return []

        await self._limiter.acquire()

        payload = {
            "model":      _MODEL,
            "max_tokens": 2048,
            "messages": [
                {"role": "user", "content": self._build_prompt(masked_text, errors, tx_type)},
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _API_URL,
                    json=payload,
                    headers={
                        "x-api-key":           self._api_key(),
                        "anthropic-version":   "2023-06-01",
                        "content-type":        "application/json",
                    },
                )
                resp.raise_for_status()
                body = resp.json()

        except httpx.HTTPStatusError as exc:
            logger.error("LLM API HTTP %d — check ANTHROPIC_API_KEY", exc.response.status_code)
            return []
        except Exception as exc:
            logger.error("LLM API call failed: %s: %s", type(exc).__name__, exc)
            return []

        raw = body.get("content", [{}])[0].get("text", "")

        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                items = [items]
        except json.JSONDecodeError:
            m = re.search(r'\[.*?\]', raw, re.DOTALL)
            if not m:
                logger.warning("LLM returned non-JSON response")
                return []
            try:
                items = json.loads(m.group(0))
            except Exception:
                logger.warning("LLM JSON extraction failed")
                return []

        decisions: list[LLMDecision] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            field_id   = str(item.get("field_id") or "").strip()
            confidence = str(item.get("confidence") or "LOW").upper()
            action     = str(item.get("action") or "UNRESOLVABLE").upper()

            if field_id in FINANCIAL_FIELD_IDS:
                logger.info("LLM attempted to modify financial field %s — rejected", field_id)
                continue

            if confidence == "LOW":
                action = "UNRESOLVABLE"

            decisions.append(LLMDecision(
                field_id       = field_id,
                field_name     = str(item.get("field_name") or ""),
                segment_id     = str(item.get("segment_id") or ""),
                resolved_value = str(item.get("resolved_value") or "") if action != "UNRESOLVABLE" else "",
                original_value = str(item.get("original_value") or ""),
                reasoning      = str(item.get("reasoning") or ""),
                confidence     = confidence,
                finding_code   = str(item.get("finding_code") or ""),
                action         = action,
                phi_was_masked = True,
            ))

        return decisions


# ── Singleton ──────────────────────────────────────────────────────────────────

_resolver: Optional[LLMResolver] = None


def get_resolver() -> LLMResolver:
    global _resolver
    if _resolver is None:
        _resolver = LLMResolver()
    return _resolver
