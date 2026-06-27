"""
backend/agent/f6_assembler.py

Assembles the final F6 output string from a MappingResult.

For each segment occurrence (in original D.0 order):
  1. In-place fields (carried + transformed + modified) sorted by raw_index
     to restore the original D.0 field order within the segment
  2. Added F6 fields (in rule/add order)
  3. Removed D.0 fields rendered as ~~field_id=value~~ at end of segment
"""
from models.schemas import MappingResult


def assemble(result: MappingResult) -> str:
    lines = []

    for seg in result.segments:
        # Use normalized_id for the segment label so 'B1' → 'CLM' etc.
        parts = [seg.normalized_id]

        # In-place fields: sort by raw_index to restore D.0 field ordering
        for f in seg.in_place_fields():
            parts.append(f'{f.field_id}={f.new_value}')

        # Added F6 fields (already in rules order from the mapper)
        for f in seg.added:
            val = f.new_value if f.new_value else ' '
            parts.append(f'{f.field_id}={val}')

        # Deprecated D.0 fields at the end — strikethrough notation
        for f in seg.removed:
            parts.append(f'~~{f.field_id}={f.old_value}~~')

        lines.append('|'.join(parts))

    return '\n'.join(lines)
