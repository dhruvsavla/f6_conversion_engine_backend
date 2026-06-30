"""
backend/agent/segment_parser.py

Hardened NCPDP Telecommunication Standard D.0 parser.
Supports production hex format and dev pipe format. Fault-tolerant.

NCPDP Separator Reference (D.0 §3.1 Telecommunications Standard):
  0x1C (FS) = Field Separator    — separates field groups from each other
                                   (also separates segment_id from first field)
  0x1D (GS) = Group Separator    — separates field_id from value within a group
  0x1E (RS) = Record/Segment Separator — separates segments

Hex stream structure (correct per spec):
  <segment_id><FS><field_id><GS><value><FS><field_id><GS><value><RS>
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from typing import List, Optional

logger = logging.getLogger(__name__)

# NCPDP separator constants (D.0 §3.1)
FS = '\x1c'   # Field Separator  — separates field groups from each other (also segment_id from first field)
GS = '\x1d'   # Group Separator  — separates field_id from value within a group
RS = '\x1e'   # Record/Segment Separator — separates segments

# Normalize legacy alphanumeric segment IDs to descriptive names.
# Applied immediately after segment_id is extracted, before any rule lookup.
SEGMENT_ALIASES: dict[str, str] = {
    'B1': 'CLM',
    'B2': 'CLM',
    'B3': 'PRV',
    'C4': 'PAT',
    'D5': 'DUR',
    'D3': 'DUR',
    'L1': 'COB',
    'F1': 'PA',
    'G1': 'CLN',
    'I1': 'CMP',
    'E1': 'CPN',
    'A1': 'RSP',
    'A4': 'PRI',
    'W1': 'WRK',
    'H1': 'DOC',
}

# CMP repeating ingredient fields — 488-RE marks the start of each ingredient group.
# These field IDs repeat once per ingredient (up to 25, controlled by 447-EC).
_CMP_INGREDIENT_FIELDS = frozenset({
    '488-RE', '489-TE', '448-ED', '449-EE', '490-UE', '362-2G', '363-2H',
})
_CMP_GROUP_MARKER = '488-RE'


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ParseError:
    """A non-fatal parse warning at segment or field level."""
    level: str          # 'segment' or 'field'
    segment_id: str
    field_id: str = ''
    raw_value: str = ''
    message: str = ''


@dataclass
class ParsedField:
    field_id: str
    value: str          # raw value — do NOT strip trailing spaces (some values are intentionally padded)
    raw_index: int = 0  # 0-based ordinal within its segment
    occurrence: int = 1 # ingredient group occurrence for CMP repeating fields (1-based)


@dataclass
class ParsedSegment:
    segment_id: str      # exactly as it appeared in the stream, e.g. 'B1', 'HDR'
    normalized_id: str   # after SEGMENT_ALIASES lookup, e.g. 'B1' → 'CLM'
    fields: List[ParsedField] = dc_field(default_factory=list)
    occurrence: int = 1  # 1 = first INS, 2 = second INS, etc.
    raw_index: int = 0   # 0-based ordinal position in the transaction
    parse_errors: List[ParseError] = dc_field(default_factory=list)


@dataclass
class ParsedTransaction:
    segments: List[ParsedSegment]   # ORDERED — preserves original stream order
    fmt: str = 'pipe'               # 'hex' or 'pipe'
    parse_errors: List[ParseError] = dc_field(default_factory=list)
    raw_input_length: int = 0

    # ── Convenience accessors ─────────────────────────────────────────────────

    def get_segments(self, segment_id: str) -> List[ParsedSegment]:
        """All segments matching normalized_id OR original segment_id."""
        return [
            s for s in self.segments
            if s.normalized_id == segment_id or s.segment_id == segment_id
        ]

    def get_first(self, segment_id: str) -> Optional[ParsedSegment]:
        """First occurrence of a segment, or None."""
        matches = self.get_segments(segment_id)
        return matches[0] if matches else None

    def get_field(self, segment_id: str, field_id: str, occurrence: int = 1) -> Optional[str]:
        """
        Return a field value from a specific segment occurrence (1-based).
        Returns None if segment or field is not found.
        """
        matches = self.get_segments(segment_id)
        if not matches or occurrence > len(matches):
            return None
        seg = matches[occurrence - 1]
        for f in seg.fields:
            if f.field_id == field_id:
                return f.value
        return None

    def has_segment(self, segment_id: str) -> bool:
        return bool(self.get_segments(segment_id))

    @property
    def total_fields(self) -> int:
        """Total field count across all segments (property for backward compat)."""
        return sum(len(s.fields) for s in self.segments)

    @property
    def segment_order(self) -> List[str]:
        """Unique normalized segment IDs in original order (backward compat)."""
        seen: dict = {}
        for seg in self.segments:
            seen.setdefault(seg.normalized_id, True)
        return list(seen.keys())

    def has_parse_errors(self) -> bool:
        if self.parse_errors:
            return True
        return any(s.parse_errors for s in self.segments)

    def all_errors(self) -> List[ParseError]:
        errors = list(self.parse_errors)
        for s in self.segments:
            errors.extend(s.parse_errors)
        return errors


# ── SegmentParser class ───────────────────────────────────────────────────────

class SegmentParser:
    """
    Fault-tolerant NCPDP parser. Never raises on malformed input.
    Errors are recorded in ParseError objects attached to the result.
    """

    def parse(self, raw: bytes | str) -> ParsedTransaction:
        """Main entry point. Auto-detects format. Never raises."""
        if isinstance(raw, bytes):
            fmt = self._detect_bytes(raw)
        else:
            fmt = self._detect_str(raw)

        try:
            if fmt == 'hex':
                return self._parse_hex(raw)
            else:
                return self._parse_pipe(raw)
        except Exception as e:
            logger.error(f'Unexpected parse failure: {e}', exc_info=True)
            tx = ParsedTransaction(segments=[], fmt=fmt)
            tx.parse_errors.append(ParseError(
                level='segment', segment_id='UNKNOWN',
                message=f'Fatal parse error: {e}'
            ))
            return tx

    # ── Format detection ──────────────────────────────────────────────────────

    def _detect_bytes(self, raw: bytes) -> str:
        # Only scan first 512 bytes for performance on large files
        sample = raw[:512]
        return 'hex' if (b'\x1c' in sample or b'\x1e' in sample) else 'pipe'

    def _detect_str(self, raw: str) -> str:
        sample = raw[:512]
        return 'hex' if ('\x1c' in sample or '\x1e' in sample) else 'pipe'

    # ── Hex parser ────────────────────────────────────────────────────────────

    def _parse_hex(self, raw: bytes | str) -> ParsedTransaction:
        """
        Parse NCPDP production byte stream.

        latin-1 decode: every byte (0x00–0xFF) maps to exactly one character.
        It NEVER raises UnicodeDecodeError. Patient names with accented bytes
        (0x80–0xFF, common in mainframe data) survive intact.
        """
        if isinstance(raw, bytes):
            text = raw.decode('latin-1')
            raw_len = len(raw)
        else:
            text = raw
            raw_len = len(raw)

        # Strip Windows BOM (﻿) — some editors prepend it to every file
        if text.startswith('﻿'):
            text = text[1:]

        # Strip null bytes — mainframe systems pad streams with \x00
        text = text.replace('\x00', '')

        tx = ParsedTransaction(segments=[], fmt='hex', raw_input_length=raw_len)
        occurrence_counter: dict[str, int] = {}

        # Split on RS (\x1e). filter(None, ...) drops empty strings from double-RS or trailing RS.
        chunks = [c for c in text.split(RS) if c.strip()]

        for raw_index, chunk in enumerate(chunks):
            seg, err = self._parse_hex_segment(chunk, raw_index, occurrence_counter)
            if seg:
                tx.segments.append(seg)
            if err:
                tx.parse_errors.append(err)

        return tx

    def _parse_hex_segment(
        self,
        chunk: str,
        raw_index: int,
        occ: dict[str, int],
    ) -> tuple[Optional[ParsedSegment], Optional[ParseError]]:
        # Segment ID is everything before the first FS (\x1c) per NCPDP spec
        first_fs = chunk.find(FS)
        if first_fs == -1:
            segment_id = chunk.strip()
            field_text = ''
        else:
            segment_id = chunk[:first_fs].strip()
            field_text = chunk[first_fs + 1:]

        if not segment_id:
            return None, ParseError(
                level='segment', segment_id='',
                message=f'Empty segment ID at chunk {raw_index}: {repr(chunk[:40])}'
            )

        normalized_id = SEGMENT_ALIASES.get(segment_id, segment_id)
        occ[normalized_id] = occ.get(normalized_id, 0) + 1

        seg = ParsedSegment(
            segment_id=segment_id,
            normalized_id=normalized_id,
            occurrence=occ[normalized_id],
            raw_index=raw_index,
        )

        if not field_text:
            return seg, None

        # FS (\x1c) separates field groups; within each group GS (\x1d) separates field_id from value
        field_groups = field_text.split(FS)
        for fi, group in enumerate(field_groups):
            if not group:
                # Double FS = empty field slot — skip silently
                continue
            pf, ferr = self._parse_hex_field(group, segment_id, fi)
            if pf:
                seg.fields.append(pf)
            if ferr:
                seg.parse_errors.append(ferr)

        if normalized_id == 'CMP':
            self._assign_cmp_ingredient_occurrences(seg)

        return seg, None

    @staticmethod
    def _assign_cmp_ingredient_occurrences(seg: ParsedSegment) -> None:
        """
        Tag CMP ingredient fields with their 1-based ingredient group occurrence.
        488-RE (Compound Product ID Qualifier) marks the start of each group.
        Header fields (450-EF, 451-EG, 447-EC) are not ingredient fields and
        retain the default occurrence=1.
        """
        ingredient_occ = 0
        for f in seg.fields:
            if f.field_id == _CMP_GROUP_MARKER:
                ingredient_occ += 1
            if f.field_id in _CMP_INGREDIENT_FIELDS:
                f.occurrence = ingredient_occ

    def _parse_hex_field(
        self, group: str, segment_id: str, fi: int
    ) -> tuple[Optional[ParsedField], Optional[ParseError]]:
        # GS (\x1d) separates field_id from value per NCPDP spec
        if GS in group:
            field_id, _, value = group.partition(GS)
            field_id = field_id.strip()
            # Do NOT strip value — some values use trailing spaces intentionally
        else:
            # No GS separator — treat entire group as field_id, empty value
            field_id = group.strip()
            value = ''

        if not field_id:
            return None, ParseError(
                level='field', segment_id=segment_id,
                raw_value=group,
                message=f'Empty field_id in group at index {fi}'
            )

        return ParsedField(field_id=field_id, value=value, raw_index=fi), None

    # ── Pipe parser ───────────────────────────────────────────────────────────

    def _parse_pipe(self, raw: bytes | str) -> ParsedTransaction:
        """
        Parse dev/UI pipe-delimited format:
          HDR|101-A1=610279|102-A2=D0
          INS|302-C2=ZH48291045|301-C1=RXGRP88
        """
        if isinstance(raw, bytes):
            text = raw.decode('utf-8', errors='replace')
            raw_len = len(raw)
        else:
            text = raw
            raw_len = len(raw)

        if text.startswith('﻿'):
            text = text[1:]

        tx = ParsedTransaction(segments=[], fmt='pipe', raw_input_length=raw_len)
        occurrence_counter: dict[str, int] = {}

        for raw_index, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            seg, err = self._parse_pipe_line(line, raw_index, occurrence_counter)
            if seg:
                tx.segments.append(seg)
            if err:
                tx.parse_errors.append(err)

        return tx

    def _parse_pipe_line(
        self,
        line: str,
        raw_index: int,
        occ: dict[str, int],
    ) -> tuple[Optional[ParsedSegment], Optional[ParseError]]:
        parts = line.split('|')
        segment_id = parts[0].strip()
        if not segment_id:
            return None, ParseError(
                level='segment', segment_id='',
                message=f'Empty segment ID on line {raw_index}: {repr(line[:40])}'
            )

        normalized_id = SEGMENT_ALIASES.get(segment_id, segment_id)
        occ[normalized_id] = occ.get(normalized_id, 0) + 1

        seg = ParsedSegment(
            segment_id=segment_id,
            normalized_id=normalized_id,
            occurrence=occ[normalized_id],
            raw_index=raw_index,
        )

        for fi, part in enumerate(parts[1:]):
            part_stripped = part.strip()
            if not part_stripped:
                continue

            if '=' in part_stripped:
                field_id, _, value = part_stripped.partition('=')
                field_id = field_id.strip()
                # Preserve value as-is (don't strip — trailing spaces matter for padding)
            else:
                field_id = part_stripped
                value = ''
                seg.parse_errors.append(ParseError(
                    level='field', segment_id=segment_id,
                    field_id=field_id, raw_value=part_stripped,
                    message=f'No "=" separator in field token: {repr(part_stripped)}'
                ))

            if field_id:
                seg.fields.append(ParsedField(field_id=field_id, value=value, raw_index=fi))

        if normalized_id == 'CMP':
            self._assign_cmp_ingredient_occurrences(seg)

        return seg, None


# ── Module-level public API (backward compat) ─────────────────────────────────

_parser = SegmentParser()


def detect_format(raw: bytes | str) -> str:
    """Return 'hex' if NCPDP separator bytes are present, 'pipe' otherwise."""
    if isinstance(raw, bytes):
        return _parser._detect_bytes(raw)
    return _parser._detect_str(raw)


def parse(raw: bytes | str) -> ParsedTransaction:
    """
    Parse NCPDP D.0 input — auto-detects hex vs pipe format.
    Accepts bytes (hex file upload) or str (UI paste or decoded text).
    """
    return _parser.parse(raw)


def parse_d0(d0_text: str) -> ParsedTransaction:
    """Backward-compatible alias — accepts pipe or hex as a string."""
    return _parser.parse(d0_text)
