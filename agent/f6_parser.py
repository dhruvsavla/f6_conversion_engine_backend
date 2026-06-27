"""
agent/f6_parser.py

Parses F6 transaction text into a ParsedTransaction.

Extends SegmentParser with one addition for pipe format:
  ~~field_id=value~~ tokens are extracted as "restored_deprecated" fields —
  they were REMOVED from D.0 during forward conversion and their original value
  was preserved in the diff-view output. The reverse assembler puts them back.

Hex format is handled identically to the base parser (strikethrough is a
display-layer artifact that never appears in production byte streams).
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass

from .segment_parser import (
    SegmentParser, ParsedTransaction, ParsedSegment, ParsedField,
    ParseError, SEGMENT_ALIASES,
)

logger = logging.getLogger(__name__)

STRIKETHROUGH_RE = re.compile(r'^~~(.+?)~~$')


@dataclass
class F6ParsedField(ParsedField):
    """ParsedField extended with F6-specific metadata."""
    is_restored_deprecated: bool = False
    # True when this field came from ~~field_id=value~~ syntax.
    # The reverse assembler will restore this field to the D.0 output.


class F6Parser(SegmentParser):
    """
    Extends SegmentParser to handle F6 pipe input with ~~strikethrough~~ tokens.
    Hex format parsing is inherited unchanged.
    """

    def _parse_pipe_line(
        self,
        line: str,
        raw_index: int,
        occ: dict,
    ):
        parts = line.split('|')
        if not parts:
            return None, None

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

            # ~~field_id=value~~ — deprecated D.0 field preserved in diff output
            st_match = STRIKETHROUGH_RE.match(part_stripped)
            if st_match:
                inner = st_match.group(1)
                if '=' in inner:
                    fid, _, val = inner.partition('=')
                    fid = fid.strip()
                    if fid:
                        seg.fields.append(F6ParsedField(
                            field_id=fid,
                            value=val,
                            raw_index=fi,
                            is_restored_deprecated=True,
                        ))
                continue

            # Normal field token
            if '=' in part_stripped:
                fid, _, val = part_stripped.partition('=')
                fid = fid.strip()
                if fid:
                    seg.fields.append(F6ParsedField(
                        field_id=fid,
                        value=val,
                        raw_index=fi,
                        is_restored_deprecated=False,
                    ))
            else:
                fid = part_stripped
                seg.fields.append(F6ParsedField(
                    field_id=fid,
                    value='',
                    raw_index=fi,
                    is_restored_deprecated=False,
                ))
                seg.parse_errors.append(ParseError(
                    level='field', segment_id=segment_id,
                    field_id=fid, raw_value=part_stripped,
                    message=f'No "=" separator in field token: {repr(part_stripped)}'
                ))

        return seg, None


# Module-level convenience instance
_parser = F6Parser()


def parse_f6(f6_text: str) -> ParsedTransaction:
    """Parse F6 transaction text (pipe or hex). Auto-detects format."""
    return _parser.parse(f6_text)
