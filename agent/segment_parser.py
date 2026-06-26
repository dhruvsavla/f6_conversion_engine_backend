"""
NCPDP D.0 segment parser.

Supports two formats:
  pipe — newline-delimited, pipe-separated (UI paste / dev/test)
  hex  — NCPDP production byte stream using \x1c \x1d \x1e separators
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import List, Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ParsedField:
    field_id: str
    value: str
    raw_index: int = 0


@dataclass
class ParsedSegment:
    segment_id: str
    fields: List[ParsedField] = dc_field(default_factory=list)
    occurrence: int = 1   # 1 = first INS, 2 = second INS, etc.
    raw_index: int = 0    # position in the original stream


@dataclass
class ParsedTransaction:
    segments: List[ParsedSegment]  # ordered list — handles repeating segments
    fmt: str = "pipe"              # "pipe" or "hex"

    @property
    def total_fields(self) -> int:
        return sum(len(s.fields) for s in self.segments)

    @property
    def segment_order(self) -> List[str]:
        """Unique segment IDs in original order (backward compat)."""
        seen: dict = {}
        for seg in self.segments:
            seen.setdefault(seg.segment_id, True)
        return list(seen.keys())

    def get_segments(self, segment_id: str) -> List[ParsedSegment]:
        """All occurrences of a segment."""
        return [s for s in self.segments if s.segment_id == segment_id]

    def get_first(self, segment_id: str) -> Optional[ParsedSegment]:
        """First occurrence of a segment, or None."""
        for s in self.segments:
            if s.segment_id == segment_id:
                return s
        return None

    def get_field(self, segment_id: str, field_id: str) -> Optional[str]:
        """Value of a field from the first occurrence of a segment."""
        seg = self.get_first(segment_id)
        if not seg:
            return None
        for f in seg.fields:
            if f.field_id == field_id:
                return f.value
        return None


# ── Format detection ──────────────────────────────────────────────────────────

def detect_format(raw: bytes | str) -> str:
    """Return 'hex' if NCPDP separator bytes are present, 'pipe' otherwise."""
    if isinstance(raw, bytes):
        return "hex" if (b"\x1c" in raw or b"\x1e" in raw) else "pipe"
    return "hex" if ("\x1c" in raw or "\x1e" in raw) else "pipe"


# ── Hex parser ────────────────────────────────────────────────────────────────

def _parse_hex(raw: bytes | str) -> List[ParsedSegment]:
    """
    Parse NCPDP production byte stream.
    Decodes as latin-1 (NCPDP is 8-bit; utf-8 would raise on bytes > 0x7F).
    Separators: \x1e = segment, \x1c = field group, \x1d = field_id/value.
    """
    text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    result: List[ParsedSegment] = []
    counts: dict[str, int] = {}

    for raw_idx, chunk in enumerate(text.split("\x1e")):
        chunk = chunk.strip("\x00").strip()
        if not chunk:
            continue

        first_sep = chunk.find("\x1c")
        if first_sep == -1:
            segment_id = chunk
            groups: List[str] = []
        else:
            segment_id = chunk[:first_sep].strip()
            groups = chunk[first_sep + 1:].split("\x1c")

        if not segment_id:
            continue

        fields: List[ParsedField] = []
        for fi, group in enumerate(groups):
            if not group:
                continue
            if "\x1d" in group:
                fid, _, val = group.partition("\x1d")
            else:
                fid, val = group, ""
            fid = fid.strip()
            if fid:
                fields.append(ParsedField(field_id=fid, value=val, raw_index=fi))

        counts[segment_id] = counts.get(segment_id, 0) + 1
        result.append(ParsedSegment(
            segment_id=segment_id,
            fields=fields,
            occurrence=counts[segment_id],
            raw_index=raw_idx,
        ))

    return result


# ── Pipe parser ───────────────────────────────────────────────────────────────

def _parse_pipe(text: str) -> List[ParsedSegment]:
    """
    Parse newline + pipe delimited format: SEGMENT|field_id=value|...
    """
    result: List[ParsedSegment] = []
    counts: dict[str, int] = {}

    for raw_idx, line in enumerate(text.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        segment_id = parts[0].strip()
        if not segment_id:
            continue

        fields: List[ParsedField] = []
        for fi, part in enumerate(parts[1:]):
            if "=" in part:
                fid, _, val = part.partition("=")
                fid = fid.strip()
                if fid:
                    fields.append(ParsedField(field_id=fid, value=val, raw_index=fi))

        counts[segment_id] = counts.get(segment_id, 0) + 1
        result.append(ParsedSegment(
            segment_id=segment_id,
            fields=fields,
            occurrence=counts[segment_id],
            raw_index=raw_idx,
        ))

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def parse(raw: bytes | str) -> ParsedTransaction:
    """
    Parse NCPDP D.0 input — auto-detects hex vs pipe format.
    Accepts bytes (hex file upload) or str (UI paste or decoded text).
    """
    fmt = detect_format(raw)
    if fmt == "hex":
        segs = _parse_hex(raw)
    else:
        text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
        segs = _parse_pipe(text)

    if not segs:
        raise ValueError(
            "No segments found. Expected NCPDP hex stream or SEGMENT|field_id=value|... format."
        )
    return ParsedTransaction(segments=segs, fmt=fmt)


def parse_d0(d0_text: str) -> ParsedTransaction:
    """Backward-compatible alias — accepts pipe or hex as a string."""
    return parse(d0_text)
