"""
ingestion/output/rule_writer.py

Writes validated rules to ingestion_output/ for human review.
After review, `--promote` copies them to the live rules/ folder.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

OUTPUT_DIR    = Path('ingestion_output')
RULES_DIR     = Path('rules')
FLAGGED_FILE  = OUTPUT_DIR / 'flagged_for_review.json'
MANIFEST_FILE = OUTPUT_DIR / 'manifest.json'

# Maps segment_id to the rule file it belongs in (matches the existing rules/ naming)
SEGMENT_FILE_MAP: dict[str, str] = {
    'HDR': '00_global',
    'INS': '01_retail',
    'CLM': '01_retail',
    'PAT': '01_retail',
    'PRE': '01_retail',
    'PRI': '01_retail',
    'DUR': '01_retail',
    'COB': '04_cob',
    'CMP': '06_compound',
    'PA':  '10_prior_auth',
    'CLN': '02_specialty',
    'DOC': '02_specialty',
}


class RuleWriter:

    def write(
        self,
        validated_results: list,
        transaction_type: str,
        source_pdf: str,
    ) -> dict:
        """
        Write valid/warn rules to ingestion_output/ and invalid ones to the
        flagged file. Returns a summary dict for the manifest.
        """
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        from ingestion.validator.rule_validator import RuleValidator
        validator = RuleValidator()

        valid_rules:   list = []
        invalid_rules: list = [r for r in validated_results if r.status == 'INVALID']

        for vr in validated_results:
            if vr.status == 'INVALID':
                continue   # already in invalid_rules

            # Run ownership check; suspect rules are demoted to INVALID
            clean, suspect = validator.validate_segment_ownership(
                [vr.rule], vr.segment_id
            )

            if suspect:
                suspect_rule = suspect[0]
                invalid_rules.append(type(vr)(
                    rule=suspect_rule,
                    status='INVALID',
                    issues=[f'INVALID: {suspect_rule.get("_review_reason", "Wrong segment")}'],
                    segment_id=vr.segment_id,
                ))
            else:
                valid_rules.append(vr)

        # Group valid rules by target output file
        rules_by_file: dict[str, dict] = {}
        for vr in valid_rules:
            file_stem = SEGMENT_FILE_MAP.get(vr.segment_id, f'99_{vr.segment_id.lower()}')
            if file_stem not in rules_by_file:
                rules_by_file[file_stem] = {
                    'transaction_type': transaction_type,
                    '_meta': {
                        'source_pdf': source_pdf,
                        'extracted_at': datetime.utcnow().isoformat() + 'Z',
                        'extraction_warnings': [],
                    },
                    'segments': {},
                }
            seg = vr.segment_id
            if seg not in rules_by_file[file_stem]['segments']:
                rules_by_file[file_stem]['segments'][seg] = []
            rules_by_file[file_stem]['segments'][seg].append(vr.rule)
            if vr.issues:
                rules_by_file[file_stem]['_meta']['extraction_warnings'].extend(vr.issues)

        written_files: list[str] = []
        for file_stem, content in rules_by_file.items():
            out_path = OUTPUT_DIR / f'{file_stem}.json'
            out_path.write_text(
                json.dumps(content, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
            written_files.append(str(out_path))

        # Write flagged rules separately so reviewers can find them easily
        if invalid_rules:
            flagged = [
                {
                    'rule':       r.rule,
                    'segment_id': r.segment_id,
                    'issues':     r.issues,
                }
                for r in invalid_rules
            ]
            FLAGGED_FILE.write_text(
                json.dumps(flagged, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )

        manifest = {
            'source_pdf':       source_pdf,
            'transaction_type': transaction_type,
            'extracted_at':     datetime.utcnow().isoformat() + 'Z',
            'valid_rules':      len(valid_rules),
            'invalid_rules':    len(invalid_rules),
            'files_written':    written_files,
            'flagged_file':     str(FLAGGED_FILE) if invalid_rules else None,
        }
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2), encoding='utf-8')

        return manifest

    def promote(self, force: bool = False) -> list[str]:
        """
        Copy JSON files from ingestion_output/ to rules/.
        Creates a timestamped backup of the existing rules/ folder first.
        """
        if not OUTPUT_DIR.exists():
            raise FileNotFoundError('ingestion_output/ does not exist. Run extraction first.')

        # Backup existing rules so promotion is always reversible
        if RULES_DIR.exists():
            backup_dir = Path(f'rules_backup_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}')
            shutil.copytree(RULES_DIR, backup_dir)
            print(f'Backed up existing rules to {backup_dir}/')

        RULES_DIR.mkdir(parents=True, exist_ok=True)
        promoted: list[str] = []

        skip_files = {'manifest.json', FLAGGED_FILE.name}

        for src in sorted(OUTPUT_DIR.glob('*.json')):
            if src.name in skip_files:
                continue
            dst = RULES_DIR / src.name
            if dst.exists() and not force:
                answer = input(f'{dst} already exists. Overwrite? [y/N] ').strip()
                if answer.lower() != 'y':
                    print(f'  Skipped {dst.name}')
                    continue
            shutil.copy2(src, dst)
            promoted.append(str(dst))
            print(f'  {src.name} → rules/{src.name}')

        return promoted
