"""
agent/d0_assembler.py

Assembles the D.0 pipe-delimited output string from a ReverseMappingResult.

Output format:
  SEGMENT|field_id=value|field_id=value
  (one line per segment, newline-separated)

Field ordering per segment:
  1. Active fields (carried + reverse-transformed), in rule/F6 order
  2. Restored deprecated D.0 fields, appended at end
  Dropped and warned fields are omitted entirely.
"""
from __future__ import annotations
from .reverse_field_mapper import ReverseMappingResult


class D0Assembler:

    def assemble(self, result: ReverseMappingResult) -> str:
        lines = []
        for seg in result.segments:
            parts = [seg.segment_id]
            for rmf in seg.d0_fields:
                parts.append(f'{rmf.field_id}={rmf.d0_value}')
            for rmf in seg.restored:
                parts.append(f'{rmf.field_id}={rmf.d0_value}')
            lines.append('|'.join(parts))
        return '\n'.join(lines)
