"""
ingestion/extractor/llm_extractor.py

Calls Claude API to extract rules from text chunks.

Design decisions:
  - One API call per chunk (not batching) — gives better context isolation
  - Retry with exponential backoff on rate limits
  - Deduplication pass after all chunks for a segment are processed
  - All prompts imported from prompts.py — never hardcoded here
  - Raw LLM output is saved to disk before parsing for debugging
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from ingestion.extractor.chunker import TextChunk
from ingestion.extractor.prompts import SYSTEM_PROMPT, build_dedup_prompt, build_extraction_prompt

logger = logging.getLogger(__name__)

MAX_RETRIES    = 3
RETRY_DELAYS   = [2, 5, 15]    # seconds between retries (exponential-ish)
RAW_OUTPUT_DIR = Path('ingestion_output/raw_llm_responses')


@dataclass
class ExtractionResult:
    segment_id: str
    chunk_index: int
    rules: list[dict]
    raw_response: str
    model_used: str
    input_tokens: int
    output_tokens: int
    extraction_errors: list[str]    # non-fatal issues during parsing


class LLMExtractor:

    def __init__(self, model: str = 'claude-sonnet-4-6', verbose: bool = False):
        # anthropic.Anthropic() reads ANTHROPIC_API_KEY from the environment
        self.client  = anthropic.Anthropic()
        self.model   = model
        self.verbose = verbose
        self.hallucination_count = 0   # incremented each time a cross-segment field is filtered
        RAW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def extract_segment(
        self,
        chunks: list[TextChunk],
        transaction_type: str,
    ) -> list[dict]:
        """
        Process all chunks for one segment and return deduplicated rules.

        For segments with multiple chunks:
          1. Extract rules from each chunk independently
          2. Run a dedup/merge LLM pass over all extracted rules
          3. Return the merged list
        """
        segment_id   = chunks[0].segment_id if chunks else 'UNKNOWN'
        all_raw_rules: list[dict] = []

        for chunk in chunks:
            result = self._extract_chunk(chunk, transaction_type)
            all_raw_rules.extend(result.rules)
            if self.verbose:
                logger.info(
                    '  [%s chunk %d] %d rules, %d+%d tokens',
                    segment_id, chunk.chunk_index,
                    len(result.rules),
                    result.input_tokens, result.output_tokens,
                )
            if result.extraction_errors:
                for err in result.extraction_errors:
                    logger.warning('  [%s chunk %d] %s', segment_id, chunk.chunk_index, err)

        if not all_raw_rules:
            return []

        # Skip the dedup call if there was only one chunk — no duplicates possible
        if len(chunks) == 1:
            return all_raw_rules

        return self._deduplicate(all_raw_rules, segment_id)

    def _extract_chunk(
        self,
        chunk: TextChunk,
        transaction_type: str,
    ) -> ExtractionResult:
        """Call Claude API for a single chunk. Retry on transient failures."""
        user_prompt = build_extraction_prompt(chunk, transaction_type)

        if self.verbose:
            logger.info(
                '\n--- PROMPT [%s chunk %d] ---\n%s...',
                chunk.segment_id, chunk.chunk_index, user_prompt[:500],
            )

        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=[{'role': 'user', 'content': user_prompt}],
                )

                raw_text = response.content[0].text

                # Save raw response to disk so engineers can debug bad extractions
                raw_path = RAW_OUTPUT_DIR / f'{chunk.segment_id}_chunk{chunk.chunk_index}.json'
                raw_path.write_text(raw_text, encoding='utf-8')

                if self.verbose:
                    logger.info('\n--- RESPONSE ---\n%s...', raw_text[:500])

                rules, errors = self._parse_llm_response(raw_text, chunk.segment_id)

                return ExtractionResult(
                    segment_id=chunk.segment_id,
                    chunk_index=chunk.chunk_index,
                    rules=rules,
                    raw_response=raw_text,
                    model_used=self.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    extraction_errors=errors,
                )

            except anthropic.RateLimitError:
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning('Rate limited. Waiting %ds before retry %d…', delay, attempt + 1)
                    time.sleep(delay)
                else:
                    raise

            except anthropic.APIError as e:
                logger.error('API error on chunk %d: %s', chunk.chunk_index, e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAYS[attempt])
                else:
                    raise

        # Reached only if all retries are exhausted without raising
        return ExtractionResult(
            segment_id=chunk.segment_id,
            chunk_index=chunk.chunk_index,
            rules=[],
            raw_response='',
            model_used=self.model,
            input_tokens=0,
            output_tokens=0,
            extraction_errors=['Max retries exceeded'],
        )

    def _parse_llm_response(
        self,
        raw_text: str,
        segment_id: str,
    ) -> tuple[list[dict], list[str]]:
        """
        Parse the LLM response into a list of rule dicts.

        The LLM is instructed to return ONLY a JSON array, but sometimes it
        wraps the output in markdown code fences. Strip those before parsing.
        """
        errors: list[str] = []
        text = raw_text.strip()

        # Strip markdown code fences when present (```json … ```)
        if text.startswith('```'):
            lines = text.split('\n')
            end_idx = -1 if lines[-1].strip() == '```' else len(lines)
            text = '\n'.join(lines[1:end_idx]).strip()

        # Ensure the string begins with '['; if not, try to locate the array
        if not text.startswith('['):
            start = text.find('[')
            end   = text.rfind(']')
            if start != -1 and end != -1:
                text = text[start:end + 1]
                errors.append('Had to extract JSON array from response (non-JSON preamble detected)')
            else:
                errors.append(
                    f'Response is not a JSON array. Segment: {segment_id}. '
                    f'Got: {text[:100]}'
                )
                return [], errors

        try:
            rules = json.loads(text)
            if not isinstance(rules, list):
                errors.append(f'Parsed JSON is not an array: {type(rules).__name__}')
                return [], errors

            # ── Segment ownership filter (safety net) ───────────────────────
            # Remove any rule whose field_id is known to belong to a different
            # segment. This catches hallucinations that slip past the prompt.
            from ingestion.extractor.prompts import SEGMENT_OWNED_FIELDS

            owned = SEGMENT_OWNED_FIELDS.get(segment_id)
            if owned:
                # Build a reverse lookup: field_id → home segment
                all_owned: dict[str, str] = {}
                for seg, fields in SEGMENT_OWNED_FIELDS.items():
                    for fid in fields:
                        all_owned[fid] = seg

                filtered = []
                for rule in rules:
                    fid = rule.get('field_id', '')
                    home_seg = all_owned.get(fid)

                    if home_seg is None:
                        # Field not in any known segment — pass through with a warning
                        # (may be a new/custom field the ownership map doesn't cover yet)
                        filtered.append(rule)

                    elif home_seg != segment_id:
                        # Field belongs to a DIFFERENT segment — this is a hallucination
                        errors.append(
                            f'HALLUCINATION_FILTERED: field {fid} belongs to {home_seg}, '
                            f'not {segment_id}. Removed from output.'
                        )
                        logger.warning(
                            '[%s] Filtered hallucinated field %s (belongs to %s)',
                            segment_id, fid, home_seg,
                        )
                        self.hallucination_count += 1

                    else:
                        # Field correctly belongs to this segment
                        filtered.append(rule)

                removed = len(rules) - len(filtered)
                if removed > 0:
                    errors.append(
                        f'Removed {removed} hallucinated field(s) from {segment_id} output.'
                    )
                rules = filtered
            # ── End segment ownership filter ─────────────────────────────────

            return rules, errors

        except json.JSONDecodeError as e:
            errors.append(f'JSON parse error: {e}. Raw: {text[:200]}')
            return [], errors

    def _deduplicate(self, rules: list[dict], segment_id: str) -> list[dict]:
        """
        Run a second LLM call to merge rules extracted from multiple chunks.
        Falls back to the undeduped list if the call fails.
        """
        prompt = build_dedup_prompt(rules, segment_id)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': prompt}],
            )
            raw_text = response.content[0].text
            deduped, errors = self._parse_llm_response(raw_text, f'{segment_id}_dedup')
            if errors:
                logger.warning('Dedup errors for %s: %s', segment_id, errors)
                return rules   # safe fallback: return undeduped rather than empty
            return deduped
        except Exception as e:
            logger.error('Dedup call failed for %s: %s. Returning undeduped rules.', segment_id, e)
            return rules
