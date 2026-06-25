# F6 Conversion Engine — Backend

FastAPI backend for the NCPDP Telecommunications Standard **D.0 → F6** agentic conversion engine. Built for the Hexaware Life Sciences SCION onboarding accelerator.

## What it does

Accepts a pipe-delimited NCPDP D.0 transaction, runs it through an 8-step agentic pipeline, and returns an F6-formatted output with a full field-level audit trail. All conversion logic lives in JSON rule files — the engine is a generic interpreter with no hard-coded field logic.

## Architecture

```
main.py                    FastAPI app + API endpoints
agent/
  orchestrator.py          8-step async pipeline (SSE streaming)
  rules_reader.py          Hot-loads all rules/*.json at request time
  segment_parser.py        Parses SEGMENT|field_id=value|... format
  transaction_detector.py  Rules-driven transaction type detection
  field_mapper.py          Applies per-field actions (carry/transform/modify/add/remove)
  transformer.py           Named transforms (ZERO_PAD_LEFT, SET_VALUE, …)
  f6_assembler.py          Assembles the final pipe-delimited F6 string
  audit_builder.py         Builds audit trail and validation findings
rules/
  00_global.json           Detection priority order, global config
  01_retail.json           Retail pharmacy claim (B1)
  02_specialty.json        Specialty / high cost therapy (B1)
  03_controlled.json       Controlled substance — DUR segment (B1)
  04_cob.json              Coordination of benefits (B1 + COB segment)
  05_reversal.json         Claim reversal (B2) — key fields only
  06_compound.json         Compound prescription (B1 + CMP segment)
  07_ltc.json              Long-term care (B1)
  08_medicare_part_d.json  Medicare Part D (B1)
  09_eligibility.json      Eligibility verification (E1)
  10_prior_auth.json       Prior authorization (PA)
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app --reload --port 8000
```

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| POST | `/api/convert/stream` | SSE stream — 8 agent steps then result |
| POST | `/api/convert` | Non-streaming JSON conversion |
| GET | `/api/rules-summary` | Live rules library metadata |
| GET | `/api/sample?type=RETAIL` | Load a sample D.0 transaction |

### Sample types
`RETAIL` · `SPECIALTY` · `CONTROLLED` · `COB` · `REVERSAL` · `COMPOUND` · `LTC` · `MEDICARE_PART_D` · `ELIGIBILITY` · `PRIOR_AUTH`

## Input format

```
HDR|101-A1=610279|102-A2=D0|103-A3=B1|104-A4=PCN4501|109-A9=1|201-B1=1457823690|202-B2=01|401-D1=20260115
INS|302-C2=ZH48291045|301-C1=RXGRP88|306-C6=01|990-MG=017394
PAT|304-C4=19580712|305-C5=2|310-CA=MARGARET|311-CB=ELLIS
CLM|455-EM=1|402-D2=104872|436-E1=03|407-D7=00071015523|442-E7=30000|403-D3=00|405-D5=30|406-D6=0|408-D8=0|414-DE=20260114|419-DJ=1
PRE|466-EZ=01|411-DB=1538192047|427-DR=NGUYEN
PRI|409-D9=8420|412-DC=125|426-DQ=9500|430-DU=8545|423-DN=01
```

## Output format

```
HDR|101-A1=00610279|102-A2=F6|103-A3=B1|104-A4=PCN4501|...
INS|302-C2=ZH48291045|301-C1=RXGRP88|306-C6=01|367-2N= |~~990-MG=017394~~
...
CLM|...|419-DJ=1|995-E2= |996-E3= 
PRE|466-EZ=01|411-DB=1538192047|427-DR=NGUYEN|364-2J= 
PRI|409-D9=8420|412-DC=125|426-DQ=9500|430-DU=8545|423-DN=01|478-H7= 
```

- Added F6 fields appended as `field_id= ` (empty, awaiting supplier data)
- Removed D.0 fields appear at end of segment as `~~field_id=value~~`
- Carried and transformed fields stay at their original position

## Rules file schema

Each file in `rules/` must have a `transaction_type` key:

```json
{
  "transaction_type": "RETAIL",
  "description": "...",
  "detection": {
    "transaction_code": ["B1"],
    "compound_code_not": ["2"]
  },
  "segments": {
    "HDR": [
      { "field_id": "101-A1", "field_name": "BIN / IIN Number", "action": "transform",
        "transform": "ZERO_PAD_LEFT", "params": { "length": 8 } },
      { "field_id": "102-A2", "field_name": "Version / Release Number", "action": "transform",
        "transform": "SET_VALUE", "value": "F6" },
      { "field_id": "103-A3", "field_name": "Transaction Code", "action": "carry" }
    ],
    "INS": [
      { "field_id": "990-MG", "field_name": "Other Payer BIN", "action": "remove" },
      { "field_id": "367-2N", "field_name": "Medicaid Agency Number", "action": "add",
        "default_value": "", "warn_if_empty": true, "warn_code": "BNI",
        "warn_message": "367-2N is new in F6. Confirm value with payer." }
    ]
  }
}
```

### Actions
| Action | Effect |
|--------|--------|
| `carry` | Field carried unchanged |
| `transform` | Value transformed (see transforms below) |
| `modify` | Value carried, flagged as modified (expanded code set in F6) |
| `remove` | Field dropped; appears as `~~field_id=value~~` in output |
| `add` | New F6 field appended to segment; not present in D.0 |

### Transforms
`ZERO_PAD_LEFT` · `SET_VALUE` · `REMOVE_HYPHENS` · `UPPERCASE` · `LOWERCASE` · `MAP_CODE` · `DATE_REFORMAT`

### Detection criteria
`transaction_code` · `compound_code` · `compound_code_not` · `patient_residence` · `patient_residence_not` · `group_id_prefix` · `segment_present` · `segment_not_present` · `field_present` · `submission_clarification_code`

Drop a new `.json` file in `rules/` — the engine picks it up automatically on the next request, no restart needed.

## CORS

Configured for `localhost:3000` and `localhost:5173` (React dev servers). Update `main.py` for other origins.
