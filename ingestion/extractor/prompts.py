"""
ingestion/extractor/prompts.py

All LLM prompts for the rule extraction pipeline.
Centralized here so they can be reviewed, versioned, and improved independently.
Never embed prompt strings in other modules.
"""

from __future__ import annotations

SYSTEM_PROMPT = """
You are a specialist in NCPDP (National Council for Prescription Drug Programs)
Telecommunication Standard Version F6 pharmacy claims processing.

Your task is to read excerpts from a PBM (Pharmacy Benefit Manager) F6 implementation
guide and convert the human-language rules into a precise, machine-readable JSON format.

## Target JSON Schema

Each rule you extract must conform to this exact schema:

```json
{
  "field_id": "NNN-XX",
  "field_name": "Human readable name",
  "action": "carry | transform | add | remove | modify | cases",
  "mandatory_f6": true | false,
  "notes": "One sentence explaining the rule source",

  // For action=transform:
  "transform": "ZERO_PAD_LEFT | SET_VALUE | REMOVE_HYPHENS | UPPERCASE | MAP_CODE",
  "value": "fixed value (for SET_VALUE)",
  "params": { "length": 8 },
  "map": { "old_code": "new_code" },

  // For action=add (new F6 field not in D.0):
  "default_value": "",
  "d0_present": false,

  // For action=cases (if-then logic):
  "cases": [
    {
      "when": {
        "field": "SEGMENT.field_id",
        "operator": "eq | neq | in | not_in | empty | not_empty | gt | lt | starts_with",
        "value": "value_or_list"
      },
      "then": { "action": "add", "default_value": "08" }
    },
    { "when": "default", "then": { "action": "add", "default_value": "01" } }
  ],

  // For a condition guard (run this rule only IF condition is true):
  "condition": {
    "if": {
      "field": "SEGMENT.field_id",
      "operator": "eq",
      "value": "2"
    }
  },

  // Compound AND/OR conditions:
  "condition": {
    "if": {
      "logic": "AND",
      "conditions": [
        { "field": "CLM.406-D6", "operator": "eq", "value": "2" },
        { "field": "CLM.420-DK", "operator": "in", "value": ["42","43"] }
      ]
    }
  },

  // Conditional warnings:
  "warn_if_empty": true,
  "warn_condition": {
    "if": { "field": "INS.694-ZJ", "operator": "eq", "value": "Y" }
  },
  "warn_code": "BNS",
  "warn_severity": "WARN | ERROR",
  "warn_message": "Human-readable warning message shown in the audit trail."
}
```

## Field Reference Format

When a condition references another field, use: "SEGMENT_ID.field_id"
Examples:
  "CLM.406-D6"    — Compound Code in the Claim segment
  "INS.694-ZJ"    — Medicare Part D indicator in the Insurance segment
  "PAT.384-4X"    — Patient Residence in the Patient segment
  "INS[2].302-C2" — Cardholder ID in the SECOND Insurance segment (for repeating)

## Segment IDs and Their Owned Fields

Each field belongs to exactly one segment. Use this ownership map:

  HDR  — 101-A1, 102-A2, 103-A3, 104-A4, 109-A9, 201-B1, 202-B2, 401-D1
  INS  — 302-C2, 301-C1, 306-C6, 308-C8, 309-C9, 367-2N, 694-ZJ, 695-ZK, 990-MG
  PAT  — 304-C4, 305-C5, 310-CA, 311-CB, 384-4X, 357-NV
  CLM  — 455-EM, 402-D2, 403-D3, 407-D7, 405-D5, 406-D6, 408-D8, 414-DE,
          415-DF, 418-DI, 419-DJ, 420-DK, 461-EU, 409-D9, 412-DC, 430-DU,
          423-DN, 600-28, 995-E2, 996-E3, 426-DQ, 436-E1, 442-E7, 454-EK
  PRE  — 411-DB, 466-EZ, 427-DR, 364-2J, 498-PY, 464-EX, 835-5C, 875-5S, 876-5T
  PRI  — 409-D9, 412-DC, 426-DQ, 430-DU, 423-DN, 433-DX, 478-H7, 481-HA, 562-J1,
          506-F6, 521-FL, 565-J4, 566-J5
  DUR  — 473-7E, 439-E4, 528-FS, 529-FT, 530-FU, 531-FV, 532-FW, 533-FX, 544-FY
  COB  — 337-4C, 338-5C, 339-6C, 443-E8, 431-DV, 342-HC, 471-5E, 472-6E,
          392-MU, 393-MV, 685-ZE
  CMP  — 450-EF, 451-EG, 452-EH, 488-RE, 489-TE, 448-ED, 449-EE, 490-UE, 891-MX
  PA   — 461-EU, 462-EV, 463-EW, 738-PD, 880-K5, 881-K6, 841-5H
  CLN  — 424-DO, 491-VE
  WRK  — (workers compensation fields)
  DOC  — (additional documentation fields)

## The Cross-Reference Rule (CRITICAL)

When you are extracting rules for segment X, you will sometimes read text that
MENTIONS a field from a different segment Y. This happens when the document says
things like:

  "If Patient Residence (384-4X) is 03, set Pharmacy Service Type (600-28) to 05"
  "See also HDR field 101-A1 for routing"
  "When Compound Code (406-D6) = 2, refer to the PA segment"

In ALL of these cases:
  - 384-4X is a PAT field — if you are extracting PAT rules, you MAY include it
  - 600-28 is a CLM field — even if mentioned in the PAT section, DO NOT include it
    in your PAT output. It will be extracted when you process the CLM section.
  - Cross-references to other segments are CONTEXT, not field definitions.
  - A field belongs to exactly one segment (the ownership map above).
    If the field_id is NOT in the ownership map for the current segment,
    DO NOT include it in your output — no exceptions.

## Action Semantics

  carry    — Field exists in D.0 and is copied unchanged to F6
  transform — Field exists in D.0 but value must be modified
  add      — Field is NEW in F6, did not exist in D.0
  remove   — Field existed in D.0 but is DEPRECATED in F6
  modify   — Value must be remapped using a lookup table
  cases    — Action depends on value of another field (if-then)

## Critical Rules

1.  ONLY extract rules for fields listed in the ownership map for the target segment.
2.  If you are uncertain about a rule, set "extraction_confidence": "LOW" and add a note.
3.  If the text says "required", set "mandatory_f6": true.
4.  If the text says "optional" or "situational", set "mandatory_f6": false.
5.  If the text describes a condition, ALWAYS use the condition schema — never describe it in "notes".
6.  Preserve exact NCPDP field codes (e.g. "406-D6") as written in the document.
7.  If a field code appears in a table with a format like "406" or "D6" separately, combine as "406-D6".
8.  Output ONLY a JSON array. No explanation, no markdown, no preamble, no postamble.
    The first character of your response must be "[" and the last must be "]".
9.  Do NOT extract fields whose field_id appears in another segment's ownership list.
    If you are processing the CMP segment, do NOT output rules for 101-A1, 102-A2,
    364-2J, 392-MU, 411-DB, 418-DI, 600-28, 841-5H, 990-MG — those belong elsewhere.
10. Cross-references and examples do NOT define fields. A statement like "see CLM.406-D6"
    does not mean you should add 406-D6 to the current segment's rules.
11. Before adding any rule to your output array, verify the field_id is in the
    ownership map for the segment you are currently processing. If it is not,
    discard it silently — do not add it, do not flag it.
12. The ownership map is authoritative. If a field appears in the document text under
    the wrong section heading, trust the ownership map over the document structure.
""".strip()


# Maps each segment to its owned field IDs.
# Used to inject a concrete allowed-list into every extraction prompt
# and as the authoritative filter in _parse_llm_response.
SEGMENT_OWNED_FIELDS: dict[str, list[str]] = {
    'HDR': ['101-A1', '102-A2', '103-A3', '104-A4', '109-A9', '201-B1', '202-B2', '401-D1'],
    'INS': ['302-C2', '301-C1', '306-C6', '308-C8', '309-C9', '367-2N', '694-ZJ', '695-ZK', '990-MG'],
    'PAT': ['304-C4', '305-C5', '310-CA', '311-CB', '384-4X', '357-NV'],
    'CLM': ['455-EM', '402-D2', '403-D3', '407-D7', '405-D5', '406-D6', '408-D8', '414-DE',
            '415-DF', '418-DI', '419-DJ', '420-DK', '461-EU', '409-D9', '412-DC', '430-DU',
            '423-DN', '600-28', '995-E2', '996-E3', '426-DQ', '436-E1', '442-E7', '454-EK'],
    'PRE': ['411-DB', '466-EZ', '427-DR', '364-2J', '498-PY', '464-EX', '835-5C', '875-5S', '876-5T'],
    'PRI': ['409-D9', '412-DC', '426-DQ', '430-DU', '423-DN', '433-DX', '478-H7', '481-HA',
            '562-J1', '506-F6', '521-FL', '565-J4', '566-J5'],
    'DUR': ['473-7E', '439-E4', '528-FS', '529-FT', '530-FU', '531-FV', '532-FW', '533-FX', '544-FY'],
    'COB': ['337-4C', '338-5C', '339-6C', '443-E8', '431-DV', '342-HC', '471-5E', '472-6E',
            '392-MU', '393-MV', '685-ZE'],
    'CMP': ['450-EF', '451-EG', '452-EH', '488-RE', '489-TE', '448-ED', '449-EE', '490-UE', '891-MX'],
    'PA':  ['461-EU', '462-EV', '463-EW', '738-PD', '880-K5', '881-K6', '841-5H'],
    'CLN': ['424-DO', '491-VE'],
}


def build_extraction_prompt(chunk: 'TextChunk', transaction_type: str) -> str:
    """Build the user message for a single chunk extraction call."""

    # Build the allowed-field list for this segment.
    # Injecting this concretely into every prompt is more reliable than
    # a general instruction in the system prompt alone.
    owned = SEGMENT_OWNED_FIELDS.get(chunk.segment_id, [])
    if owned:
        allowed_block = (
            f'\nSTRICT BOUNDARY — {chunk.segment_id} Segment Only:\n'
            f'You may ONLY output rules for these field IDs: {", ".join(owned)}\n'
            f'Any field_id not in this list MUST be silently discarded.\n'
            f'Do not include fields from other segments even if they are mentioned '
            f'in the text as context or cross-references.\n'
        )
    else:
        allowed_block = ''

    context = ''
    if chunk.chunk_index > 0:
        context = (
            f'NOTE: This is chunk {chunk.chunk_index + 1} of {chunk.total_chunks} '
            f'for the {chunk.segment_id} segment. '
            f'Earlier chunks have already been processed. '
            f'Extract ONLY rules that appear in THIS chunk. '
            f'Do not repeat rules from earlier chunks.\n\n'
        )

    return (
        f'{context}'
        f'Extract ALL field-level rules for the {chunk.segment_id} segment\n'
        f'from the following text. This is for transaction type: {transaction_type}.\n'
        f'Source: {chunk.source_pdf}, pages {chunk.page_start}-{chunk.page_end}.\n'
        f'{allowed_block}\n'
        f'--- BEGIN DOCUMENT EXCERPT ---\n'
        f'{chunk.text}\n'
        f'--- END DOCUMENT EXCERPT ---\n\n'
        f'Return a JSON array of rule objects. Every field mentioned in the text should\n'
        f'produce at least one rule object IF AND ONLY IF the field_id is in the allowed\n'
        f'list above. If you see a field described with conditions, use the "cases" or\n'
        f'"condition" schema — do not simplify conditional logic to unconditional rules.\n'
        f'Remember: output ONLY field_ids from the allowed list. Discard everything else.'
    ).strip()


def build_dedup_prompt(all_rules: list[dict], segment_id: str) -> str:
    """
    After processing all chunks for a segment, deduplicate rules.
    The same field may appear in multiple chunks with slightly different text.
    This prompt merges them into the single most accurate rule.
    """
    import json

    owned = SEGMENT_OWNED_FIELDS.get(segment_id, [])
    boundary_block = ''
    if owned:
        boundary_block = (
            f'\nSTRICT BOUNDARY — {segment_id} Segment Only:\n'
            f'The output array must contain ONLY rules for these field IDs: {", ".join(owned)}\n'
            f'Remove any rule whose field_id is not in this list before returning.\n'
        )

    return (
        f'You have extracted {len(all_rules)} rules for the {segment_id} segment\n'
        f'from multiple document chunks. Some rules may be duplicates (same field_id\n'
        f'appearing in more than one chunk). Your task:\n\n'
        f'1. Merge duplicate rules for the same field_id into one definitive rule.\n'
        f'2. When merging, prefer the more specific rule (with conditions) over the simpler one.\n'
        f'3. If two versions contradict each other, keep BOTH and set "extraction_confidence": "REVIEW".\n'
        f'4. Preserve all unique rules (different field_ids).\n'
        f'5. REMOVE any rule whose field_id does not belong to the {segment_id} segment.\n'
        f'{boundary_block}\n'
        f'Input rules:\n'
        f'{json.dumps(all_rules, indent=2)}\n\n'
        f'Return a JSON array of the deduplicated, filtered rules.\n'
        f'Output ONLY the JSON array. First character "[", last character "]".'
    ).strip()
