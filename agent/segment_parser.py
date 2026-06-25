from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ParsedField:
    field_id: str
    value: str


@dataclass
class ParsedTransaction:
    segments: Dict[str, List[ParsedField]]
    segment_order: List[str]

    @property
    def total_fields(self) -> int:
        return sum(len(v) for v in self.segments.values())


def parse_d0(d0_text: str) -> ParsedTransaction:
    """Parse pipe-delimited D.0 format: SEGMENT|field_id=value|field_id=value|..."""
    segments: Dict[str, List[ParsedField]] = {}
    segment_order: List[str] = []

    for line in d0_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        seg_name = parts[0].strip()
        if not seg_name:
            continue

        if seg_name not in segments:
            segments[seg_name] = []
            segment_order.append(seg_name)

        for part in parts[1:]:
            if "=" in part:
                field_id, _, value = part.partition("=")
                field_id = field_id.strip()
                if field_id:
                    segments[seg_name].append(ParsedField(field_id=field_id, value=value))

    if not segments:
        raise ValueError(
            "No segments found. Expected: SEGMENT|field_id=value|..."
        )

    return ParsedTransaction(segments=segments, segment_order=segment_order)
