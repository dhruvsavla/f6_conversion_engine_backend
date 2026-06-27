"""
ingestion/pipeline.py

CLI entry point for the NCPDP F6 rules ingestion pipeline.

Usage:
  python ingestion/pipeline.py --pdf path/to/pbm_f6_guide.pdf
  python ingestion/pipeline.py --pdf guide.pdf --dry-run
  python ingestion/pipeline.py --pdf guide.pdf --segment CLM
  python ingestion/pipeline.py --pdf guide.pdf --promote
  python ingestion/pipeline.py --review
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import sys
from pathlib import Path

# Ensure the project root (parent of ingestion/) is on sys.path so that
# `from ingestion.extractor...` absolute imports work when the script is
# run directly: `python ingestion/pipeline.py ...`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env from the project root so ANTHROPIC_API_KEY doesn't need to be
# exported manually — just set it in .env once.
from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / '.env')

from ingestion.extractor.chunker import Chunker
from ingestion.extractor.llm_extractor import LLMExtractor
from ingestion.extractor.pdf_loader import PDFLoader
from ingestion.extractor.rule_compiler import RuleCompiler
from ingestion.output.rule_writer import RuleWriter
from ingestion.review.diff_reporter import DiffReporter
from ingestion.validator.rule_validator import RuleValidator

logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── ANSI helpers ──────────────────────────────────────────────────────────────

def _green(s: str) -> str:  return f'\033[32m{s}\033[0m'
def _yellow(s: str) -> str: return f'\033[33m{s}\033[0m'
def _red(s: str) -> str:    return f'\033[31m{s}\033[0m'
def _bold(s: str) -> str:   return f'\033[1m{s}\033[0m'

_BAR = '█' * 16

def _step(n: int, total: int, label: str, detail: str = '') -> None:
    """Print a pipeline step header."""
    detail_str = f' {detail}' if detail else ''
    print(f'\nStep {n}/{total}  {label}{detail_str}')


def _progress_line(segment_id: str, rule_count: int, done: bool = True) -> None:
    status = _green('done') if done else _yellow('…')
    print(f'           [{segment_id:<8}] {_BAR} {status} — {rule_count} rules extracted')


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_extraction(args: argparse.Namespace) -> None:
    """Full 6-step extraction pipeline."""

    if not os.environ.get('ANTHROPIC_API_KEY'):
        print(_red('ERROR: ANTHROPIC_API_KEY environment variable is not set.'))
        print('       export ANTHROPIC_API_KEY=sk-ant-...')
        sys.exit(1)

    pdf_path = args.pdf
    if not Path(pdf_path).exists():
        print(_red(f'ERROR: PDF not found: {pdf_path}'))
        sys.exit(1)

    TOTAL_STEPS = 6

    # ── Step 1+2: Load PDF ────────────────────────────────────────────────────
    _step(1, TOTAL_STEPS, 'Loading PDF', f'.................. {Path(pdf_path).name}')
    loader = PDFLoader()
    doc = loader.load(pdf_path)
    print(f'           ({doc.total_pages} pages, {Path(pdf_path).stat().st_size / 1024 / 1024:.1f} MB)')

    _step(2, TOTAL_STEPS, 'Extracting text')
    full_text = doc.full_text()
    token_estimate = len(full_text) // 4
    print(f'           {token_estimate:,} tokens extracted')

    # ── Step 3: Chunking ──────────────────────────────────────────────────────
    _step(3, TOTAL_STEPS, 'Chunking by segment')
    chunker = Chunker()
    all_chunks = chunker.chunk(doc, target_segment=args.segment)

    # Group chunks by segment_id
    chunks_by_seg: dict[str, list] = collections.defaultdict(list)
    for chunk in all_chunks:
        chunks_by_seg[chunk.segment_id].append(chunk)

    seg_count = len(chunks_by_seg)
    print(f'           {seg_count} segment(s) identified')
    for seg_id, segs in chunks_by_seg.items():
        page_start = segs[0].page_start
        page_end   = segs[-1].page_end
        print(f'           {seg_id} ({page_end - page_start + 1} page(s), {len(segs)} chunk(s))')

    # ── Step 4: LLM extraction ────────────────────────────────────────────────
    _step(4, TOTAL_STEPS, f'Extracting rules via LLM ... {seg_count} API call group(s)')
    extractor = LLMExtractor(model=args.model, verbose=args.verbose)
    compiler  = RuleCompiler()

    transaction_type = args.transaction_type
    all_compiled: list[dict] = []
    seg_rule_map: dict[str, list[dict]] = {}

    for seg_id, seg_chunks in chunks_by_seg.items():
        try:
            raw_rules = extractor.extract_segment(seg_chunks, transaction_type)
        except Exception as e:
            print(_red(f'  [{seg_id}] FAILED: {e}'))
            continue

        compiled = compiler.compile_all(raw_rules, seg_id)
        seg_rule_map[seg_id] = compiled
        all_compiled.extend(compiled)
        _progress_line(seg_id, len(compiled))

    # Print hallucination summary so engineers can see whether prompts are working
    total_filtered = extractor.hallucination_count
    if total_filtered > 0:
        print(_yellow(
            f'\n  ⚠  {total_filtered} hallucinated field(s) auto-filtered during extraction'
        ))
        print('     (fields placed in the wrong segment by the LLM)')
        print('     Re-run with --verbose to see which fields were removed\n')
    else:
        print(_green('\n  ✓  No cross-segment hallucinations detected\n'))

    # ── Step 5: Validation ────────────────────────────────────────────────────
    _step(5, TOTAL_STEPS, f'Validating ................. {len(all_compiled)} rules')
    validator = RuleValidator()
    all_results = []
    for seg_id, rules in seg_rule_map.items():
        results = validator.validate_all(rules, seg_id)
        all_results.extend(results)

    valid_count   = sum(1 for r in all_results if r.status == 'VALID')
    warn_count    = sum(1 for r in all_results if r.status == 'WARN')
    invalid_count = sum(1 for r in all_results if r.status == 'INVALID')
    print(f'           {valid_count} valid, {warn_count} with warnings, {_red(str(invalid_count) + " flagged") if invalid_count else "0 flagged"}')

    # ── Step 6: Write output ──────────────────────────────────────────────────
    if args.dry_run:
        _step(6, TOTAL_STEPS, 'Dry run — printing to stdout (no files written)')
        import json
        for vr in all_results:
            status_str = _green('VALID') if vr.status == 'VALID' else _yellow('WARN') if vr.status == 'WARN' else _red('INVALID')
            print(f'  [{status_str}] {vr.segment_id}.{vr.rule.get("field_id","?")}  {vr.rule.get("field_name","")}')
            for issue in vr.issues:
                print(f'         {issue}')
        return

    _step(6, TOTAL_STEPS, 'Writing output')
    writer = RuleWriter()
    manifest = writer.write(all_results, transaction_type, source_pdf=str(pdf_path))

    for f in manifest['files_written']:
        print(f'           {f}')
    if manifest.get('flagged_file'):
        print(_yellow(f'           ⚠  {invalid_count} rule(s) flagged → {manifest["flagged_file"]}'))

    print(_bold('\nDone.') + ' Review output in ingestion_output/ before promoting.')
    print('Run: python ingestion/pipeline.py --review')
    print('Then: python ingestion/pipeline.py --promote')


def run_review() -> None:
    """Print a diff between ingestion_output/ and rules/."""
    reporter = DiffReporter()
    print(reporter.report())


def run_promote(force: bool) -> None:
    """Promote ingestion_output/ files to rules/."""
    writer = RuleWriter()
    try:
        promoted = writer.promote(force=force)
    except FileNotFoundError as e:
        print(_red(f'ERROR: {e}'))
        sys.exit(1)

    if promoted:
        print(_green(f'\n{len(promoted)} file(s) promoted to rules/.'))
        print('Restart the FastAPI server (or wait for the next request) — it re-reads rules/ automatically.')
    else:
        print('No files were promoted.')


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='pipeline.py',
        description='NCPDP F6 rules ingestion pipeline — PDF → JSON rule files',
    )
    p.add_argument('--pdf',              metavar='FILE',    help='Path to PBM F6 implementation guide PDF')
    p.add_argument('--segment',          metavar='SEG',     help='Only process a specific segment (e.g. CLM, INS)')
    p.add_argument('--transaction-type', metavar='TYPE',    default='RETAIL', help='Transaction type label for extracted rules (default: RETAIL)')
    p.add_argument('--dry-run',          action='store_true', help='Run extraction but do not write any files')
    p.add_argument('--promote',          action='store_true', help='Copy ingestion_output/ files to rules/')
    p.add_argument('--review',           action='store_true', help='Show diff without making any changes')
    p.add_argument('--force',            action='store_true', help='Overwrite existing rule files without asking')
    p.add_argument('--model',            default='claude-sonnet-4-6', help='Claude model to use (default: claude-sonnet-4-6)')
    p.add_argument('--verbose',          action='store_true', help='Print each LLM prompt and response for debugging')
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.review:
        run_review()
    elif args.promote:
        run_promote(force=args.force)
    elif args.pdf:
        run_extraction(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
