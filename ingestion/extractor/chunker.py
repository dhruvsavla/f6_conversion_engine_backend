"""
ingestion/extractor/chunker.py

Splits the full PDF text into segment-focused chunks.

The key insight: NCPDP implementation guides are organized BY SEGMENT.
There will be a section titled "Claim Segment" or "4.7 CLM Segment Fields"
that describes all rules for CLM. We want one chunk per segment section.

Chunking strategy:
  1. Detect segment section boundaries using regex patterns
  2. Each chunk = all text between two segment headings
  3. If a segment section is too large (> MAX_CHUNK_TOKENS), split it
     into overlapping sub-chunks so no rule is cut across chunk boundaries
  4. Each chunk carries metadata: segment_id, page_range, chunk_index

Why not just split on token count?
  Splitting mid-section causes the LLM to see incomplete rules.
  A field description that starts on page 47 and ends on page 48 must
  stay in one chunk or the condition logic will be missed.
"""

import re
from dataclasses import dataclass, field

MAX_CHUNK_TOKENS = 6000        # ~24,000 chars. Leave room for system prompt.
OVERLAP_TOKENS   = 500         # ~2,000 chars overlap between sub-chunks.

# Patterns that indicate the start of a new segment section in NCPDP guides.
# Ordered from most specific to least specific.
SEGMENT_HEADING_PATTERNS = [
    # "4.7 Claim Segment (CLM)"  or  "Section 4.7 - CLM"
    r'(?im)^\s*(?:section\s+)?[\d.]+\s+(?:claim|header|insurance|patient|prescriber|pricing|compound|cob|dur|prior auth|reversal|eligibility|ltc|compound)\s+segment',
    # "Claim Segment Fields"
    r'(?im)^\s*(?:HDR|INS|PAT|CLM|PRE|PRI|DUR|COB|CMP|PA|RSP|WRK|CLN|DOC)\s+(?:segment|fields)',
    # "Segment: CLM"
    r'(?im)^\s*segment[:\s]+(?:HDR|INS|PAT|CLM|PRE|PRI|DUR|COB|CMP|PA|RSP|WRK|CLN|DOC)',
]

# Map common section title keywords to normalized segment IDs
SEGMENT_TITLE_MAP = {
    'header':           'HDR',
    'hdr':              'HDR',
    'insurance':        'INS',
    'ins':              'INS',
    'patient':          'PAT',
    'pat':              'PAT',
    'claim':            'CLM',
    'clm':              'CLM',
    'prescriber':       'PRE',
    'pre':              'PRE',
    'pricing':          'PRI',
    'pri':              'PRI',
    'drug utilization': 'DUR',
    'dur':              'DUR',
    'coordination':     'COB',
    'cob':              'COB',
    'compound':         'CMP',
    'cmp':              'CMP',
    'prior auth':       'PA',
    'pa':               'PA',
    'response':         'RSP',
    'workers':          'WRK',
    'clinical':         'CLN',
    'documentation':    'DOC',
}


@dataclass
class TextChunk:
    segment_id: str             # e.g. 'CLM', 'INS' — which segment this chunk covers
    chunk_index: int            # 0-based index within this segment's chunks
    total_chunks: int           # total chunks for this segment
    page_start: int
    page_end: int
    text: str
    token_estimate: int         # len(text) // 4
    source_pdf: str


class Chunker:

    def chunk(self, doc, target_segment: str = None) -> list[TextChunk]:
        """
        Split document into segment-focused chunks.
        If target_segment is set, only return chunks for that segment.
        """
        full_text = doc.full_text()
        segment_sections = self._detect_segment_boundaries(full_text, doc)

        chunks = []
        for seg_id, section_text, page_start, page_end in segment_sections:
            if target_segment and seg_id != target_segment.upper():
                continue
            seg_chunks = self._split_section(seg_id, section_text, page_start, page_end, doc.file_path)
            chunks.extend(seg_chunks)

        # If no segment boundaries detected, treat entire doc as one chunk
        if not chunks:
            chunks = self._split_section('UNKNOWN', full_text, 1, doc.total_pages, doc.file_path)

        return chunks

    def _detect_segment_boundaries(self, text: str, doc) -> list[tuple]:
        """Find where each segment section starts and ends in the full text."""
        boundaries = []
        for pattern in SEGMENT_HEADING_PATTERNS:
            for match in re.finditer(pattern, text):
                seg_id = self._classify_heading(match.group())
                if seg_id:
                    boundaries.append((match.start(), match.end(), seg_id))

        if not boundaries:
            return []

        # Sort by position, deduplicate overlapping matches from multiple patterns
        boundaries.sort(key=lambda x: x[0])
        deduped = [boundaries[0]]
        for b in boundaries[1:]:
            if b[0] > deduped[-1][1] + 100:  # >100 chars gap = distinct heading
                deduped.append(b)

        # Extract text between consecutive boundaries
        sections = []
        for i, (start, end, seg_id) in enumerate(deduped):
            next_start = deduped[i + 1][0] if i + 1 < len(deduped) else len(text)
            section_text = text[start:next_start]
            page_start = self._estimate_page(text, start)
            page_end   = self._estimate_page(text, next_start)
            sections.append((seg_id, section_text, page_start, page_end))

        return sections

    def _classify_heading(self, heading_text: str) -> str | None:
        """Map a heading string to a normalized segment ID."""
        lower = heading_text.lower()
        for keyword, seg_id in SEGMENT_TITLE_MAP.items():
            if keyword in lower:
                return seg_id
        return None

    def _estimate_page(self, full_text: str, position: int) -> int:
        """Find the most recent PAGE marker before this position."""
        text_before = full_text[:position]
        markers = list(re.finditer(r'--- PAGE (\d+) ---', text_before))
        return int(markers[-1].group(1)) if markers else 1

    def _split_section(
        self,
        seg_id: str,
        text: str,
        page_start: int,
        page_end: int,
        source_pdf: str,
    ) -> list[TextChunk]:
        """Split a section into overlapping sub-chunks if it exceeds MAX_CHUNK_TOKENS."""
        max_chars     = MAX_CHUNK_TOKENS * 4
        overlap_chars = OVERLAP_TOKENS * 4

        if len(text) <= max_chars:
            return [TextChunk(
                segment_id=seg_id,
                chunk_index=0,
                total_chunks=1,
                page_start=page_start,
                page_end=page_end,
                text=text,
                token_estimate=len(text) // 4,
                source_pdf=source_pdf,
            )]

        # Split with overlap so field descriptions spanning chunk boundaries aren't lost
        raw_chunks = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                # Prefer breaking at a paragraph boundary to avoid mid-rule splits
                boundary = text.rfind('\n\n', start, end)
                if boundary > start + max_chars // 2:
                    end = boundary
            raw_chunks.append(text[start:end])
            start = end - overlap_chars

        total = len(raw_chunks)
        return [
            TextChunk(
                segment_id=seg_id,
                chunk_index=i,
                total_chunks=total,
                page_start=page_start,
                page_end=page_end,
                text=chunk_text,
                token_estimate=len(chunk_text) // 4,
                source_pdf=source_pdf,
            )
            for i, chunk_text in enumerate(raw_chunks)
        ]
