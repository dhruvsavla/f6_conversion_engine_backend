import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import orchestrator, rules_reader

app = FastAPI(title="NCPDP D.0 → F6 Conversion Engine", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RULES_DIR = Path(__file__).parent / "rules"

SAMPLES: dict[str, str] = {
    "RETAIL": (
        "HDR|101-A1=610279|102-A2=D0|103-A3=B1|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823690|202-B2=01|401-D1=20260115\n"
        "INS|302-C2=ZH48291045|301-C1=RXGRP88|306-C6=01|990-MG=017394\n"
        "PAT|304-C4=19580712|305-C5=2|310-CA=MARGARET|311-CB=ELLIS\n"
        "CLM|455-EM=1|402-D2=104872|436-E1=03|407-D7=00071015523|442-E7=30000"
        "|403-D3=00|405-D5=30|406-D6=0|408-D8=0|414-DE=20260114|419-DJ=1\n"
        "PRE|466-EZ=01|411-DB=1538192047|427-DR=NGUYEN\n"
        "PRI|409-D9=8420|412-DC=125|426-DQ=9500|430-DU=8545|423-DN=01"
    ),
    "SPECIALTY": (
        "HDR|101-A1=014658|102-A2=D0|103-A3=B1|104-A4=SPEC220|109-A9=1"
        "|201-B1=1902845510|202-B2=01|401-D1=20260203\n"
        "INS|302-C2=SP9920475|301-C1=SPNET01|306-C6=01\n"
        "PAT|304-C4=19710920|305-C5=1|310-CA=DAVID|311-CB=OKORO\n"
        "CLM|455-EM=1|402-D2=550241|436-E1=03|407-D7=00078069115|442-E7=2800"
        "|403-D3=00|405-D5=28|406-D6=0|408-D8=0|414-DE=20260201|419-DJ=3\n"
        "PRE|466-EZ=01|411-DB=1740388291|427-DR=PATEL\n"
        "PRI|409-D9=1284500|412-DC=200|426-DQ=1300000|430-DU=1284700|423-DN=01"
    ),
    "CONTROLLED": (
        "HDR|101-A1=003858|102-A2=D0|103-A3=B1|104-A4=CTRL019|109-A9=1"
        "|201-B1=1366472890|202-B2=01|401-D1=20260220\n"
        "INS|302-C2=CS1184320|301-C1=CSGRP02|306-C6=01\n"
        "PAT|304-C4=19840405|305-C5=2|310-CA=LINDA|311-CB=FERRARO\n"
        "CLM|455-EM=1|402-D2=778120|436-E1=03|407-D7=00406012305|442-E7=6000"
        "|403-D3=00|405-D5=20|406-D6=0|408-D8=0|414-DE=20260219|419-DJ=5\n"
        "PRE|466-EZ=01|411-DB=1285730492|427-DR=COLLINS\n"
        "DUR|473-7E=1|439-E4=TD|440-E5=00|441-E6=1B\n"
        "PRI|409-D9=3650|412-DC=125|426-DQ=4200|430-DU=3775|423-DN=01"
    ),
    "COB": (
        "HDR|101-A1=600428|102-A2=D0|103-A3=B1|104-A4=COB7700|109-A9=1"
        "|201-B1=1457823690|202-B2=01|401-D1=20260308\n"
        "INS|302-C2=MD55012789|301-C1=MCRGRP|306-C6=01|990-MG=610502\n"
        "PAT|304-C4=19490311|305-C5=1|310-CA=ROBERT|311-CB=HAYES\n"
        "CLM|455-EM=1|402-D2=903117|436-E1=03|407-D7=00185003101|442-E7=9000"
        "|403-D3=01|405-D5=90|406-D6=0|408-D8=0|414-DE=20260301|419-DJ=1\n"
        "COB|337-4C=1|338-5C=01|339-6C=03|340-7C=610502|443-E8=20260305"
        "|341-HB=1|342-HC=08|431-DV=6200\n"
        "PRI|409-D9=11200|412-DC=150|426-DQ=12000|430-DU=5150|423-DN=01"
    ),
    "REVERSAL": (
        "HDR|101-A1=610279|102-A2=D0|103-A3=B2|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823690|202-B2=01|401-D1=20260116\n"
        "INS|302-C2=ZH48291045|301-C1=RXGRP88\n"
        "CLM|455-EM=1|402-D2=104872"
    ),
    "COMPOUND": (
        "HDR|101-A1=017912|102-A2=D0|103-A3=B1|104-A4=CMP3300|109-A9=1"
        "|201-B1=1902845510|202-B2=01|401-D1=20260325\n"
        "INS|302-C2=CP7741290|301-C1=CMPGRP|306-C6=01\n"
        "PAT|304-C4=19951102|305-C5=2|310-CA=AISHA|311-CB=RAHMAN\n"
        "CLM|455-EM=1|402-D2=661450|436-E1=00|407-D7=0|442-E7=12000"
        "|403-D3=00|405-D5=30|406-D6=2|408-D8=0|414-DE=20260324|419-DJ=1\n"
        "CMP|450-EF=7|451-EG=02|447-EC=3\n"
        "PRI|409-D9=15600|412-DC=175|426-DQ=18000|430-DU=15775|423-DN=01"
    ),
    "LTC": (
        "HDR|101-A1=610279|102-A2=D0|103-A3=B1|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823696|202-B2=01|401-D1=20260115\n"
        "INS|302-C2=ZH48291051|301-C1=RXGRP88|306-C6=01\n"
        "PAT|304-C4=19380610|305-C5=1|310-CA=EDWARD|311-CB=FOSTER|384-7E=03\n"
        "CLM|455-EM=1|402-D2=00378007301|436-E1=03|407-D7=00071015528|442-E7=25000"
        "|403-D3=00|405-D5=30|406-D6=0|408-D8=0|414-DE=20260114|419-DJ=1\n"
        "PRE|466-EZ=01|411-DB=1234567898|427-DR=MILLER\n"
        "PRI|409-D9=2500|412-DC=250|426-DQ=2750|430-DU=2500|423-DN=01"
    ),
    "MEDICARE_PART_D": (
        "HDR|101-A1=610279|102-A2=D0|103-A3=B1|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823697|202-B2=01|401-D1=20260115\n"
        "INS|302-C2=ZH48291052|301-C1=PDM001|306-C6=01\n"
        "PAT|304-C4=19380815|305-C5=1|310-CA=FRANK|311-CB=GARCIA\n"
        "CLM|455-EM=1|402-D2=00006017154|436-E1=03|407-D7=00071015529|442-E7=40000"
        "|403-D3=00|405-D5=30|406-D6=0|408-D8=0|414-DE=20260114|419-DJ=1\n"
        "PRE|466-EZ=01|411-DB=1234567900|427-DR=ANDERSON\n"
        "PRI|409-D9=4000|412-DC=400|426-DQ=4400|430-DU=4000|423-DN=01"
    ),
    "ELIGIBILITY": (
        "HDR|101-A1=610279|102-A2=D0|103-A3=E1|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823698|202-B2=01|401-D1=20260115\n"
        "INS|302-C2=ZH48291053|301-C1=RXGRP88|306-C6=01\n"
        "PAT|304-C4=19800430|305-C5=2|310-CA=GRACE|311-CB=HARRIS"
    ),
    "PRIOR_AUTH": (
        "HDR|101-A1=610279|102-A2=D0|103-A3=PA|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823699|202-B2=01|401-D1=20260115\n"
        "INS|302-C2=ZH48291054|301-C1=RXGRP88|306-C6=01\n"
        "PAT|304-C4=19750917|305-C5=1|310-CA=HENRY|311-CB=IRWIN\n"
        "CLM|455-EM=1|402-D2=55513008530|436-E1=03|407-D7=00071015530|442-E7=60000"
        "|403-D3=00|405-D5=30|406-D6=0|408-D8=0|414-DE=20260114|419-DJ=1\n"
        "PRE|466-EZ=01|411-DB=1234567902|427-DR=THOMPSON\n"
        "PA|498-GN=PRIOR AUTH 12345|496-GL=20260115|497-GM=20261231|499-GO=01\n"
        "PRI|409-D9=6000|412-DC=600|426-DQ=6600|430-DU=6000|423-DN=01"
    ),
}


class ConvertRequest(BaseModel):
    d0_text: str


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.post("/api/convert/stream")
async def convert_stream(req: ConvertRequest):
    """SSE endpoint: streams agent step events then the final result."""
    if not req.d0_text.strip():
        raise HTTPException(400, "d0_text must not be empty.")

    async def generate():
        async for event in orchestrator.convert_stream(req.d0_text):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/convert")
async def convert(req: ConvertRequest):
    """Non-streaming: waits for full conversion and returns complete JSON."""
    if not req.d0_text.strip():
        raise HTTPException(400, "d0_text must not be empty.")

    steps = []
    result = None
    async for event in orchestrator.convert_stream(req.d0_text):
        if event["type"] == "step":
            steps.append(event["data"])
        elif event["type"] == "result":
            result = event["data"]
        elif event["type"] == "error":
            raise HTTPException(500, event["data"]["message"])

    if result is None:
        raise HTTPException(500, "Conversion produced no result.")

    return {**result, "agent_steps": steps}


@app.post("/api/convert/hex/stream")
async def convert_hex_stream(request: Request):
    """SSE endpoint for binary NCPDP hex files (application/octet-stream)."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Request body must not be empty.")
    text = body.decode("latin-1")  # NCPDP is 8-bit; latin-1 preserves all byte values

    async def generate():
        async for event in orchestrator.convert_stream(text):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/convert/hex")
async def convert_hex(request: Request):
    """Non-streaming: binary NCPDP hex file → full JSON result."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Request body must not be empty.")
    text = body.decode("latin-1")

    steps = []
    result = None
    async for event in orchestrator.convert_stream(text):
        if event["type"] == "step":
            steps.append(event["data"])
        elif event["type"] == "result":
            result = event["data"]
        elif event["type"] == "error":
            raise HTTPException(500, event["data"]["message"])

    if result is None:
        raise HTTPException(500, "Conversion produced no result.")

    return {**result, "agent_steps": steps}


@app.get("/api/rules-summary")
async def rules_summary():
    try:
        ruleset = rules_reader.load_all(str(RULES_DIR))
    except Exception as e:
        raise HTTPException(500, f"Failed to load rules: {e}")

    details = []
    for tx_type, rules in ruleset.rules_by_tx.items():
        segs = rules.get("segments", {})
        details.append({
            "transaction_type": tx_type,
            "description": rules.get("description", ""),
            "file": next((f for f in ruleset.files if tx_type.lower() in f.lower()), ""),
            "segments": [
                {"segment": seg, "rule_count": len(field_rules)}
                for seg, field_rules in segs.items()
            ],
            "total_field_rules": sum(len(v) for v in segs.values()),
        })

    return {
        "files": ruleset.files,
        "total_rules": ruleset.total_field_rules,
        "transaction_types": ruleset.transaction_types,
        "details": details,
    }


@app.get("/api/sample")
async def sample(type: str = "RETAIL"):
    tx_type = type.upper()
    if tx_type not in SAMPLES:
        raise HTTPException(400, f"Unknown type '{type}'. Valid: {sorted(SAMPLES.keys())}")
    return {"d0_text": SAMPLES[tx_type]}
