# NCPDP F6 Rules Ingestion Pipeline

Converts PBM F6 implementation guide PDFs into the JSON rule files
that the FastAPI conversion engine reads.

## One-time Setup

```bash
cd ingestion
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Basic Usage

```bash
# Extract rules from a PDF
python ingestion/pipeline.py --pdf path/to/pbm_f6_guide.pdf

# Review what changed vs current live rules
python ingestion/pipeline.py --review

# Promote to live rules/ folder
python ingestion/pipeline.py --promote

# Extract only the CLM segment (faster for testing)
python ingestion/pipeline.py --pdf guide.pdf --segment CLM

# Dry run — print to stdout, write nothing
python ingestion/pipeline.py --pdf guide.pdf --dry-run

# Debug mode — print all LLM prompts and responses
python ingestion/pipeline.py --pdf guide.pdf --verbose
```

## Workflow

```
1. Get PDF from PBM (Caremark, OptumRx, Express Scripts, etc.)
2. Run:     python ingestion/pipeline.py --pdf pbm_guide.pdf
3. Review:  python ingestion/pipeline.py --review
4. Fix any rules in ingestion_output/flagged_for_review.json manually
5. Promote: python ingestion/pipeline.py --promote
6. Restart (or wait for next request) — FastAPI re-reads rules/ automatically
```

## Output Files

```
ingestion_output/
  01_retail.json              ← extracted rules, ready to review
  04_cob.json
  ...
  flagged_for_review.json     ← rules the LLM was uncertain about
  manifest.json               ← summary of this extraction run
  raw_llm_responses/          ← raw LLM outputs for debugging
    CLM_chunk0.json
    CLM_chunk1.json
    ...
```

## Cost Estimate

A typical 300-page PBM F6 guide requires approximately:
- 14 segment extraction calls (~6,000 tokens each)
- 14 deduplication calls (~4,000 tokens each)
- Total: ~140,000 input tokens + ~40,000 output tokens
- Cost at Sonnet rates: approximately $0.60–$1.20 per PDF

This runs once per PBM guide update (typically quarterly).

## Pipeline Steps

```
Step 1/6  Loading PDF ................ guide.pdf (312 pages, 2.1MB)
Step 2/6  Extracting text ............ 87,432 tokens extracted
Step 3/6  Chunking by segment ........ 14 segments identified
           HDR (8 pages), INS (12 pages), CLM (31 pages) ...
Step 4/6  Extracting rules via LLM ... 14 API call groups
           [HDR     ] ████████████████ done — 12 rules extracted
           [CLM     ] ████████████████ done — 44 rules extracted
           ...
Step 5/6  Validating ................. 147 rules — 144 valid, 3 flagged
Step 6/6  Writing output ............. ingestion_output/01_retail.json
```

## Validation

Rules are graded:

| Status  | Meaning                                        |
|---------|------------------------------------------------|
| VALID   | Ready to use                                   |
| WARN    | Minor issue (e.g. missing field_name) — usable |
| INVALID | Must be fixed — goes to flagged_for_review.json |

Common INVALID reasons: missing `field_id`, bad `NNN-XX` format, unknown
`action`, missing `params` for `ZERO_PAD_LEFT`, missing `action` in a `cases`
branch.

## Architecture Note

The LLM is used **only during this offline compilation step**.
When actual D.0 claims flow through the FastAPI engine, they hit the
deterministic Python pipeline with zero LLM involvement. This means:

- Claim processing is fast, predictable, and auditable
- LLM costs are incurred once per PDF, not per claim
- Rule files can be reviewed and approved before going live
- A bad LLM extraction is caught at compile time, not at claim time
