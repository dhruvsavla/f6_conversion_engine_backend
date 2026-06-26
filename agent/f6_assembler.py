from .field_mapper import MappingResult


def assemble(result: MappingResult) -> str:
    """
    Assemble the F6 output string.
    For each segment occurrence (in original D.0 order):
      1. Non-removed D.0 fields (carried + transformed) in original order
      2. Added F6 fields (in rules order)
      3. Removed fields with ~~strikethrough~~ at end
    """
    lines = []

    for seg in result.segments:
        parts = [seg.name]

        for f in seg.in_place:
            parts.append(f"{f.field_id}={f.new_value}")

        for f in seg.added:
            val = f.new_value if f.new_value else " "
            parts.append(f"{f.field_id}={val}")

        for f in seg.removed:
            parts.append(f"~~{f.field_id}={f.old_value}~~")

        lines.append("|".join(parts))

    return "\n".join(lines)
