"""
ingestion/review/diff_reporter.py

Shows a human-readable diff between newly extracted rules (ingestion_output/)
and the current live rules (rules/) without modifying anything.

Output format:

  CLM Segment — 44 live rules, 47 extracted
  ─────────────────────────────────────────
  + 418-DI  Quantity Prescribed         [ADDED]
  + 600-28  Pharmacy Service Type       [ADDED]
  ~ 102-A2  Version / Release Number    [MODIFIED: action carry→transform]
  - 999-ZZ  Legacy Field                [REMOVED]
  = 101-A1  BIN / IIN Number            [UNCHANGED]

  3 flagged for review → ingestion_output/flagged_for_review.json
"""

from __future__ import annotations

import json
from pathlib import Path


OUTPUT_DIR = Path('ingestion_output')
RULES_DIR  = Path('rules')


class DiffReporter:

    def report(self) -> str:
        if not OUTPUT_DIR.exists():
            return 'ingestion_output/ does not exist. Run extraction first.'

        lines: list[str] = []
        any_diff = False

        for extracted_file in sorted(OUTPUT_DIR.glob('*.json')):
            if extracted_file.name in ('manifest.json', 'flagged_for_review.json'):
                continue

            try:
                extracted_data = json.loads(extracted_file.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError) as e:
                lines.append(f'  ERROR reading {extracted_file.name}: {e}')
                continue

            live_file = RULES_DIR / extracted_file.name
            live_data: dict = {}
            if live_file.exists():
                try:
                    live_data = json.loads(live_file.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError):
                    pass

            for segment_id, extracted_rules in extracted_data.get('segments', {}).items():
                live_rules = live_data.get('segments', {}).get(segment_id, [])
                live_map = {r['field_id']: r for r in live_rules if 'field_id' in r}
                ext_map  = {r['field_id']: r for r in extracted_rules if 'field_id' in r}

                added     = [fid for fid in ext_map if fid not in live_map]
                removed   = [fid for fid in live_map if fid not in ext_map]
                changed   = [
                    fid for fid in ext_map
                    if fid in live_map and ext_map[fid] != live_map[fid]
                ]

                if not (added or removed or changed):
                    continue   # nothing changed in this segment

                any_diff = True
                lines.append(
                    f'\n{segment_id} Segment — '
                    f'{len(live_rules)} live rule(s), {len(extracted_rules)} extracted'
                )
                lines.append('─' * 55)

                for fid in sorted(added):
                    name = ext_map[fid].get('field_name', '')
                    lines.append(f'  + {fid:<10}  {name:<35}  [ADDED]')

                for fid in sorted(removed):
                    name = live_map[fid].get('field_name', '')
                    lines.append(f'  - {fid:<10}  {name:<35}  [REMOVED]')

                for fid in sorted(changed):
                    live_action = live_map[fid].get('action', '?')
                    ext_action  = ext_map[fid].get('action', '?')
                    name        = ext_map[fid].get('field_name', '')
                    if live_action != ext_action:
                        change_desc = f'action {live_action}→{ext_action}'
                    else:
                        change_desc = 'values changed'
                    lines.append(f'  ~ {fid:<10}  {name:<35}  [MODIFIED: {change_desc}]')

        flagged_file = OUTPUT_DIR / 'flagged_for_review.json'
        if flagged_file.exists():
            try:
                flagged = json.loads(flagged_file.read_text(encoding='utf-8'))
                lines.append(
                    f'\n  ⚠  {len(flagged)} rule(s) flagged for review '
                    f'→ {flagged_file}'
                )
            except (json.JSONDecodeError, OSError):
                pass

        if not any_diff:
            lines.append('No differences found between ingestion_output/ and rules/.')

        return '\n'.join(lines)
