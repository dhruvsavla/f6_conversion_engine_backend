"""
tools/field_consistency_check.py

Verifies that every field_id referenced in the validator, field mapper,
and engine code actually exists as a known field in the seeder.

This catches hallucinated or mislabeled field references before they
reach production. Run before every deploy or as part of CI.

Usage:
    python tools/field_consistency_check.py
    python tools/field_consistency_check.py --strict   # exit 1 on any mismatch
"""

import ast
import re
import sys
from pathlib import Path

# NCPDP field ID pattern: 3 digits OR letter+2-3 digits, then hyphen, then 1-4 alphanumeric
FIELD_ID_PATTERN = re.compile(r'\b([A-Z]?\d{2,3}-[A-Z0-9]{1,4})\b')

BACKEND_ROOT = Path(__file__).parent.parent

SEEDER_FILE = BACKEND_ROOT / "seeds" / "f6_standards_seeder.py"

FILES_TO_CHECK = [
    BACKEND_ROOT / "agent" / "f6_validator.py",
    BACKEND_ROOT / "agent" / "field_mapper.py",
    BACKEND_ROOT / "engine" / "agent.py",
]


def extract_known_field_ids(seeder_path: Path) -> set[str]:
    """Parse the seeder and return every field_id defined in a _rule() call."""
    source = seeder_path.read_text()
    tree = ast.parse(source)

    known: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "field_id" and isinstance(kw.value, ast.Constant):
                    known.add(kw.value.value)
    return known


def extract_field_id_references(filepath: Path) -> list[tuple[str, int, str]]:
    """Return (field_id, line_number, context_snippet) for every NCPDP-shaped string in the file."""
    if not filepath.exists():
        return []

    results = []
    lines = filepath.read_text().splitlines()
    for i, line in enumerate(lines, start=1):
        # Only scan inside string literals (lines containing a quote)
        if "'" not in line and '"' not in line:
            continue
        for match in FIELD_ID_PATTERN.finditer(line):
            fid = match.group(1)
            results.append((fid, i, line.strip()[:100]))
    return results


def run_check(strict: bool = False) -> int:
    print("=" * 70)
    print("  Field ID Consistency Check")
    print("=" * 70)

    known_ids = extract_known_field_ids(SEEDER_FILE)
    print(f"\nLoaded {len(known_ids)} known field IDs from seeder.\n")

    total_issues = 0

    for filepath in FILES_TO_CHECK:
        references = extract_field_id_references(filepath)
        unknown = [(fid, ln, ctx) for fid, ln, ctx in references if fid not in known_ids]

        rel = filepath.relative_to(BACKEND_ROOT)
        if not unknown:
            print(f"  OK   {rel} — {len(references)} field references, all grounded")
            continue

        # Deduplicate by field_id; keep first occurrence line number
        seen: dict[str, tuple[int, str]] = {}
        for fid, ln, ctx in unknown:
            if fid not in seen:
                seen[fid] = (ln, ctx)

        print(f"  WARN {rel} — {len(seen)} unknown field ID(s):")
        for fid, (ln, ctx) in seen.items():
            print(f"         line {ln}: {fid!r} — {ctx}")
            total_issues += 1

    print("\n" + "=" * 70)
    if total_issues == 0:
        print("  PASS — every field ID in checked files exists in the seeder.")
        print("=" * 70)
        return 0
    else:
        print(f"  FOUND {total_issues} ungrounded field ID reference(s).")
        print("  Each one is either:")
        print("    (a) a real NCPDP field missing from the seeder — add it, or")
        print("    (b) a hallucinated/mislabeled field — remove the check")
        print("=" * 70)
        return 1 if strict else 0


if __name__ == "__main__":
    strict_mode = "--strict" in sys.argv
    sys.exit(run_check(strict=strict_mode))
