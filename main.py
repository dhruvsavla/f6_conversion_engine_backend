import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv()   # reads .env from the project root

from agent import orchestrator, rules_reader
from agent.batch_processor import BATCH_JOBS, process_batch_background, start_batch_job
from ingestion.ingest_api import router as ingest_router
from database import init_db, seed_from_rules_folder, migrate_db
import db_ops
from agent.reverse_orchestrator import ReverseOrchestrator
from agent.validation_orchestrator import ValidationOrchestrator

reverse_orchestrator     = ReverseOrchestrator()
validation_orchestrator  = ValidationOrchestrator()

app = FastAPI(title="NCPDP D.0 → F6 Conversion Engine", version="3.0.0")
app.include_router(ingest_router)

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


@app.on_event("startup")
def startup():
    init_db()
    migrate_db()
    seed_from_rules_folder(str(RULES_DIR))


# ── Health / Stats ────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}


@app.get("/api/stats")
def stats():
    return db_ops.get_db_stats()


# ── Single Conversion (streaming) — persists to DB ────────────────────────────

@app.post("/api/convert/stream")
async def convert_stream(req: ConvertRequest):
    """SSE endpoint: streams agent step events then a final result with conversion_id."""
    if not req.d0_text.strip():
        raise HTTPException(400, "d0_text must not be empty.")

    cid = db_ops.create_conversion(filename="manual_input", d0_input=req.d0_text)
    db_ops.mark_conversion_processing(cid)

    async def generate():
        steps: list[dict] = []
        try:
            async for event in orchestrator.convert_stream(req.d0_text):
                if event["type"] == "step":
                    steps.append(event["data"])
                elif event["type"] == "result":
                    result = event["data"]
                    active_rs = db_ops.get_active_rule_set()
                    try:
                        db_ops.complete_conversion(
                            conversion_id=cid,
                            transaction_type=result.get("transaction_type", ""),
                            f6_output=result.get("f6_output", ""),
                            summary=result.get("audit", {}).get("summary", {}),
                            rule_set_version=active_rs["name"] if active_rs else "default",
                        )
                        db_ops.insert_audit_entries(cid, result.get("audit", {}).get("entries", []))
                        db_ops.insert_audit_findings(cid, result.get("audit", {}).get("findings", []))
                        db_ops.insert_agent_steps(cid, steps)
                    except Exception:
                        pass  # persistence failure must not break the live stream
                    event["data"]["conversion_id"] = cid  # inject so frontend can link to history
                elif event["type"] == "error":
                    db_ops.fail_conversion(cid, event["data"].get("message", "Unknown error"))
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            db_ops.fail_conversion(cid, str(e))
            yield f"data: {json.dumps({'type':'error','data':{'message':str(e)}})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/convert")
async def convert(req: ConvertRequest):
    """Non-streaming: returns complete JSON including conversion_id."""
    if not req.d0_text.strip():
        raise HTTPException(400, "d0_text must not be empty.")

    cid = db_ops.create_conversion(filename="manual_input", d0_input=req.d0_text)
    db_ops.mark_conversion_processing(cid)

    steps: list[dict] = []
    result = None
    try:
        async for event in orchestrator.convert_stream(req.d0_text):
            if event["type"] == "step":
                steps.append(event["data"])
            elif event["type"] == "result":
                result = event["data"]
            elif event["type"] == "error":
                db_ops.fail_conversion(cid, event["data"]["message"])
                raise HTTPException(500, event["data"]["message"])
    except HTTPException:
        raise
    except Exception as e:
        db_ops.fail_conversion(cid, str(e))
        raise HTTPException(500, str(e))

    if result is None:
        db_ops.fail_conversion(cid, "Conversion produced no result.")
        raise HTTPException(500, "Conversion produced no result.")

    active_rs = db_ops.get_active_rule_set()
    db_ops.complete_conversion(
        conversion_id=cid,
        transaction_type=result.get("transaction_type", ""),
        f6_output=result.get("f6_output", ""),
        summary=result.get("audit", {}).get("summary", {}),
        rule_set_version=active_rs["name"] if active_rs else "default",
    )
    db_ops.insert_audit_entries(cid, result.get("audit", {}).get("entries", []))
    db_ops.insert_audit_findings(cid, result.get("audit", {}).get("findings", []))
    db_ops.insert_agent_steps(cid, steps)

    return {"conversion_id": cid, **result, "agent_steps": steps}


# ── Hex binary (streaming) — persists to DB ───────────────────────────────────

@app.post("/api/convert/hex/stream")
async def convert_hex_stream(request: Request):
    """SSE endpoint for binary NCPDP hex files (application/octet-stream)."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Request body must not be empty.")
    text = body.decode("latin-1")

    cid = db_ops.create_conversion(filename="binary_upload.dat", d0_input=text)
    db_ops.mark_conversion_processing(cid)

    async def generate():
        steps: list[dict] = []
        try:
            async for event in orchestrator.convert_stream(text):
                if event["type"] == "step":
                    steps.append(event["data"])
                elif event["type"] == "result":
                    result = event["data"]
                    active_rs = db_ops.get_active_rule_set()
                    try:
                        db_ops.complete_conversion(
                            conversion_id=cid,
                            transaction_type=result.get("transaction_type", ""),
                            f6_output=result.get("f6_output", ""),
                            summary=result.get("audit", {}).get("summary", {}),
                            rule_set_version=active_rs["name"] if active_rs else "default",
                        )
                        db_ops.insert_audit_entries(cid, result.get("audit", {}).get("entries", []))
                        db_ops.insert_audit_findings(cid, result.get("audit", {}).get("findings", []))
                        db_ops.insert_agent_steps(cid, steps)
                    except Exception:
                        pass
                    event["data"]["conversion_id"] = cid
                elif event["type"] == "error":
                    db_ops.fail_conversion(cid, event["data"].get("message", ""))
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            db_ops.fail_conversion(cid, str(e))
            yield f"data: {json.dumps({'type':'error','data':{'message':str(e)}})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/convert/hex")
async def convert_hex(request: Request):
    """Non-streaming hex binary conversion."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Request body must not be empty.")
    text = body.decode("latin-1")

    steps: list[dict] = []
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


# ── In-memory batch (existing — unchanged) ────────────────────────────────────

@app.post("/api/convert/batch")
async def convert_batch(req: ConvertRequest, background_tasks: BackgroundTasks):
    if not req.d0_text.strip():
        raise HTTPException(400, "d0_text must not be empty.")
    job_id = start_batch_job(req.d0_text)
    background_tasks.add_task(process_batch_background, job_id, req.d0_text)
    return {"message": "Batch accepted", "job_id": job_id}


@app.get("/api/convert/batch/{job_id}")
async def get_batch_status(job_id: str):
    job = BATCH_JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"Batch job '{job_id}' not found.")
    return job


# ── File-upload batch (new — DB-backed) ───────────────────────────────────────

@app.post("/api/batch/upload")
async def batch_upload(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """Upload multiple D.0 text/binary files. Returns batch_id immediately."""
    if not files:
        raise HTTPException(400, "No files uploaded.")
    if len(files) > 100:
        raise HTTPException(400, "Maximum 100 files per batch.")

    batch_name = f'batch_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}'
    batch_id   = db_ops.create_batch(name=batch_name, total_files=len(files))

    file_data: list[tuple[str, str, str]] = []
    for f in files:
        content = await f.read()
        text    = content.decode("latin-1", errors="replace")
        cid     = db_ops.create_conversion(
            filename=f.filename or "unknown.txt",
            d0_input=text,
            batch_id=batch_id,
        )
        file_data.append((cid, text, f.filename or "unknown.txt"))

    background_tasks.add_task(_process_file_batch, batch_id, file_data)

    return {"batch_id": batch_id, "total_files": len(files), "status": "processing"}


async def _process_file_batch(batch_id: str, file_data: list[tuple]):
    """Background task: convert each uploaded file and persist results."""
    active_rs = db_ops.get_active_rule_set()

    for cid, text, filename in file_data:
        db_ops.mark_conversion_processing(cid)
        try:
            steps: list[dict] = []
            result = None
            async for event in orchestrator.convert_stream(text):
                if event["type"] == "step":
                    steps.append(event["data"])
                elif event["type"] == "result":
                    result = event["data"]
                elif event["type"] == "error":
                    raise RuntimeError(event["data"].get("message", "Conversion error"))

            if result is None:
                raise RuntimeError("Conversion produced no result.")

            db_ops.complete_conversion(
                conversion_id=cid,
                transaction_type=result.get("transaction_type", ""),
                f6_output=result.get("f6_output", ""),
                summary=result.get("audit", {}).get("summary", {}),
                rule_set_version=active_rs["name"] if active_rs else "default",
            )
            db_ops.insert_audit_entries(cid, result.get("audit", {}).get("entries", []))
            db_ops.insert_audit_findings(cid, result.get("audit", {}).get("findings", []))
            db_ops.insert_agent_steps(cid, steps)
        except Exception as e:
            db_ops.fail_conversion(cid, str(e))
        finally:
            db_ops.update_batch_progress(batch_id)


# ── History (DB-backed) ───────────────────────────────────────────────────────

@app.get("/api/conversions")
def list_conversions(
    limit:    int          = Query(default=20, le=100),
    offset:   int          = Query(default=0),
    status:   Optional[str] = None,
    batch_id: Optional[str] = None,
):
    rows  = db_ops.list_conversions(limit=limit, offset=offset,
                                    status=status, batch_id=batch_id)
    total = db_ops.count_conversions(status=status)
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/conversions/{conversion_id}")
def get_conversion_detail(
    conversion_id: str,
    segment:     Optional[str] = None,
    change_type: Optional[str] = None,
    search:      Optional[str] = None,
):
    conv = db_ops.get_conversion(conversion_id)
    if not conv:
        raise HTTPException(404, "Conversion not found.")

    entries  = db_ops.get_audit_entries(conversion_id, segment, change_type, search)
    findings = db_ops.get_audit_findings(conversion_id)
    steps    = db_ops.get_agent_steps(conversion_id)

    return {
        **conv,
        "direction":  conv.get("direction", "D0_TO_F6"),
        "d0_output":  conv.get("d0_output") or "",
        "input_text": conv.get("input_text") or conv.get("d0_input") or "",
        "audit": {
            "summary": {
                "added":       conv["fields_added"],
                "carried":     conv["fields_carried"],
                "transformed": conv["fields_transformed"],
                "removed":     conv["fields_removed"],
                "modified":    conv["fields_modified"],
                "missing":     conv["fields_missing"],
                "warnings":    conv["warnings_count"],
                "errors":      conv["errors_count"],
            },
            "findings": findings,
            "entries":  entries,
        },
        "agent_steps": steps,
    }


# ── Batch history ─────────────────────────────────────────────────────────────

@app.get("/api/batches")
def list_batches(
    limit:  int = Query(default=20, le=100),
    offset: int = Query(default=0),
):
    rows = db_ops.list_batches(limit=limit, offset=offset)
    return {"items": rows}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    batch = db_ops.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found.")
    conversions = db_ops.list_conversions(batch_id=batch_id, limit=200)
    return {**batch, "conversions": conversions}


# ── Downloads ─────────────────────────────────────────────────────────────────

@app.get("/api/conversions/{conversion_id}/download/json")
def download_audit_json(conversion_id: str):
    conv = db_ops.get_conversion(conversion_id)
    if not conv:
        raise HTTPException(404, "Not found.")

    report = {
        "conversion_id":    conversion_id,
        "filename":         conv["filename"],
        "transaction_type": conv["transaction_type"],
        "status":           conv["status"],
        "created_at":       conv["created_at"],
        "completed_at":     conv["completed_at"],
        "f6_output":        conv["f6_output"],
        "audit_summary": {
            "added":       conv["fields_added"],
            "carried":     conv["fields_carried"],
            "transformed": conv["fields_transformed"],
            "removed":     conv["fields_removed"],
            "modified":    conv["fields_modified"],
            "missing":     conv["fields_missing"],
            "warnings":    conv["warnings_count"],
            "errors":      conv["errors_count"],
        },
        "findings":    db_ops.get_audit_findings(conversion_id),
        "entries":     db_ops.get_audit_entries(conversion_id),
        "agent_steps": db_ops.get_agent_steps(conversion_id),
    }

    content  = json.dumps(report, indent=2, ensure_ascii=False)
    safe_name = conv["filename"].replace(" ", "_")
    filename  = f'audit_{conversion_id[:8]}_{safe_name}.json'

    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/conversions/{conversion_id}/download/f6")
def download_f6_output(conversion_id: str):
    conv = db_ops.get_conversion(conversion_id)
    if not conv or not conv.get("f6_output"):
        raise HTTPException(404, "Not found or conversion not complete.")

    filename = f'f6_{conv["filename"]}'
    return StreamingResponse(
        io.BytesIO(conv["f6_output"].encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Rules (DB-backed) ─────────────────────────────────────────────────────────

@app.get("/api/rules/sets")
def list_rule_sets():
    return {"items": db_ops.list_rule_sets()}


@app.post("/api/rules/sets/{rule_set_id}/activate")
def activate_rule_set(rule_set_id: str):
    db_ops.activate_rule_set(rule_set_id)
    return {"status": "activated", "rule_set_id": rule_set_id}


@app.get("/api/rules")
def list_rules(
    rule_set_id:      Optional[str] = None,
    transaction_type: Optional[str] = None,
    segment_id:       Optional[str] = None,
    search:           Optional[str] = None,
):
    active = db_ops.get_active_rule_set()
    rs_id  = rule_set_id or (active["id"] if active else None)
    if not rs_id:
        return {"items": []}
    items = db_ops.list_rules(rs_id, transaction_type, segment_id, search)
    return {"items": items}


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: str, body: dict):
    db_ops.update_rule(rule_id, body)
    return {"status": "updated"}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str):
    db_ops.delete_rule(rule_id)
    return {"status": "deleted"}


# ── Existing rules-summary (file-based, unchanged) ───────────────────────────

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


# ── F6 Samples ────────────────────────────────────────────────────────────────

F6_SAMPLES: dict[str, str] = {
    "RETAIL": (
        "HDR|101-A1=00610279|102-A2=F6|103-A3=B1|104-A4=PCN4501|109-A9=1"
        "|201-B1=1457823690|202-B2=01|401-D1=20260115\n"
        "INS|302-C2=ZH48291045|301-C1=RXGRP88|306-C6=01|367-2N=01|~~990-MG=017394~~\n"
        "PAT|304-C4=19580712|305-C5=2|310-CA=MARGARET|311-CB=ELLIS|357-NV=01\n"
        "CLM|455-EM=1|402-D2=104872|436-E1=03|407-D7=00071015523|442-E7=30000"
        "|403-D3=00|405-D5=30|406-D6=0|408-D8=0|414-DE=20260114|419-DJ=1"
        "|995-E2=01|996-E3=N\n"
        "PRE|466-EZ=01|411-DB=1538192047|427-DR=NGUYEN|364-2J=\n"
        "PRI|409-D9=8420|412-DC=125|426-DQ=9500|430-DU=8545|423-DN=01|478-H7="
    ),
    "COB": (
        "HDR|101-A1=00600428|102-A2=F6|103-A3=B1|104-A4=COB7700|109-A9=1"
        "|201-B1=1457823690|202-B2=01|401-D1=20260308\n"
        "INS|302-C2=MD55012789|301-C1=MCRGRP|306-C6=01|367-2N=01|~~990-MG=610502~~\n"
        "PAT|304-C4=19490311|305-C5=1|310-CA=ROBERT|311-CB=HAYES|357-NV=01\n"
        "CLM|455-EM=1|402-D2=903117|436-E1=03|407-D7=00185003101|442-E7=9000"
        "|403-D3=01|405-D5=90|406-D6=0|408-D8=0|414-DE=20260301|419-DJ=1"
        "|995-E2=02|996-E3=N\n"
        "COB|337-4C=1|338-5C=01|339-6C=03|340-7C=610502|443-E8=20260305"
        "|341-HB=1|342-HC=08|431-DV=6200\n"
        "PRI|409-D9=11200|412-DC=150|426-DQ=12000|430-DU=5150|423-DN=01|478-H7="
    ),
    "CONTROLLED": (
        "HDR|101-A1=00003858|102-A2=F6|103-A3=B1|104-A4=CTRL019|109-A9=1"
        "|201-B1=1366472890|202-B2=01|401-D1=20260220\n"
        "INS|302-C2=CS1184320|301-C1=CSGRP02|306-C6=01|367-2N=01\n"
        "PAT|304-C4=19840405|305-C5=2|310-CA=LINDA|311-CB=FERRARO|357-NV=01\n"
        "CLM|455-EM=1|402-D2=778120|436-E1=03|407-D7=00406012305|442-E7=6000"
        "|403-D3=00|405-D5=20|406-D6=0|408-D8=0|414-DE=20260219|419-DJ=5"
        "|995-E2=01|996-E3=N\n"
        "PRE|466-EZ=01|411-DB=1285730492|427-DR=COLLINS|364-2J=\n"
        "DUR|473-7E=1|439-E4=TD|440-E5=00|441-E6=1B\n"
        "PRI|409-D9=3650|412-DC=125|426-DQ=4200|430-DU=3775|423-DN=01|478-H7="
    ),
}


@app.get("/api/sample-f6")
async def sample_f6(type: str = "RETAIL"):
    tx_type = type.upper()
    if tx_type not in F6_SAMPLES:
        available = sorted(F6_SAMPLES.keys())
        raise HTTPException(400, f"Unknown type '{type}'. Available F6 samples: {available}")
    return {"f6_text": F6_SAMPLES[tx_type]}


# ── F6 → D.0 Reverse Conversion ──────────────────────────────────────────────

@app.post("/api/reverse-convert")
async def reverse_convert(body: dict):
    """Convert F6 → D.0. Body: {f6_text, filename?}. Persists to DB."""
    f6_text  = (body.get("f6_text") or "").strip()
    filename = body.get("filename") or "manual_f6_input"

    if not f6_text:
        raise HTTPException(400, "f6_text is required")

    active_rs = db_ops.get_active_rule_set()
    cid = db_ops.create_conversion(
        filename=filename,
        d0_input='',
        direction='F6_TO_D0',
        input_text=f6_text,
    )
    db_ops.mark_conversion_processing(cid)

    try:
        result = await reverse_orchestrator.convert(f6_text)

        db_ops.complete_conversion(
            conversion_id=cid,
            transaction_type=result.transaction_type,
            f6_output='',
            d0_output=result.d0_output,
            summary=result.audit['summary'],
            rule_set_version=active_rs['name'] if active_rs else 'default',
        )
        db_ops.insert_audit_entries(cid, result.audit['entries'])
        db_ops.insert_audit_findings(cid, result.audit['findings'])
        db_ops.insert_agent_steps(cid, result.agent_steps)

        return {
            'conversion_id':    cid,
            'transaction_type': result.transaction_type,
            'd0_output':        result.d0_output,
            'f6_input':         f6_text,
            'agent_steps':      result.agent_steps,
            'audit':            result.audit,
        }

    except Exception as e:
        db_ops.fail_conversion(cid, str(e))
        raise HTTPException(500, f"Reverse conversion failed: {e}")


# ── F6 → D.0 Batch Upload ─────────────────────────────────────────────────────

@app.post("/api/reverse-batch")
async def reverse_batch_upload(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """Upload multiple F6 files. Returns batch_id immediately."""
    if not files:
        raise HTTPException(400, "No files uploaded.")
    if len(files) > 100:
        raise HTTPException(400, "Maximum 100 files per batch.")

    batch_name = f'f6_to_d0_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}'
    batch_id   = db_ops.create_batch(name=batch_name, total_files=len(files))

    file_data: list[tuple[str, str]] = []
    for f in files:
        content = await f.read()
        text    = content.decode("latin-1", errors="replace")
        cid     = db_ops.create_conversion(
            filename=f.filename or "unknown_f6.txt",
            d0_input='',
            batch_id=batch_id,
            direction='F6_TO_D0',
            input_text=text,
        )
        file_data.append((cid, text))

    background_tasks.add_task(_process_reverse_batch, batch_id, file_data)
    return {"batch_id": batch_id, "total_files": len(files), "status": "processing"}


async def _process_reverse_batch(batch_id: str, file_data: list[tuple]):
    active_rs = db_ops.get_active_rule_set()
    for cid, text in file_data:
        db_ops.mark_conversion_processing(cid)
        try:
            result = await reverse_orchestrator.convert(text)
            db_ops.complete_conversion(
                conversion_id=cid,
                transaction_type=result.transaction_type,
                d0_output=result.d0_output,
                summary=result.audit['summary'],
                rule_set_version=active_rs['name'] if active_rs else 'default',
            )
            db_ops.insert_audit_entries(cid, result.audit['entries'])
            db_ops.insert_audit_findings(cid, result.audit['findings'])
            db_ops.insert_agent_steps(cid, result.agent_steps)
        except Exception as e:
            db_ops.fail_conversion(cid, str(e))
        finally:
            db_ops.update_batch_progress(batch_id)


# ── Download D.0 Output ───────────────────────────────────────────────────────

# ── F6 Validator ─────────────────────────────────────────────────────────────

@app.post("/api/validate")
async def validate_f6(body: dict):
    """Validate an F6 transaction. Body: {f6_text, rule_set_id?}."""
    f6_text     = (body.get("f6_text") or "").strip()
    rule_set_id = body.get("rule_set_id")

    if not f6_text:
        raise HTTPException(400, "f6_text is required")

    try:
        result = await validation_orchestrator.validate(f6_text, rule_set_id=rule_set_id)
        summary = result.report.summary
        return {
            'validation_id':    result.validation_id,
            'transaction_type': result.transaction_type,
            'overall_status':   result.overall_status,
            'rule_set_id':      result.rule_set_id,
            'rule_set_name':    result.rule_set_name,
            'agent_steps':      result.agent_steps,
            'summary':          summary,
            'categories':       result.report.categories,
            'checks': [
                {
                    'check_id':    c.check_id,
                    'category':    c.category,
                    'segment':     c.segment,
                    'field_id':    c.field_id,
                    'field_name':  c.field_name,
                    'status':      c.status,
                    'expected':    c.expected,
                    'actual':      c.actual,
                    'message':     c.message,
                    'occurrence':  c.occurrence,
                    'rule_source': c.rule_source,
                }
                for c in result.report.checks
            ],
            'parse_errors': result.report.parse_errors,
        }
    except Exception as e:
        raise HTTPException(500, f"Validation failed: {e}")


@app.get("/api/validations")
def list_validations(
    limit:  int          = Query(default=20, le=100),
    offset: int          = Query(default=0),
    status: Optional[str] = None,
):
    rows  = db_ops.list_validations(limit=limit, offset=offset, status=status)
    total = db_ops.count_validations(status=status)
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/validations/{validation_id}")
def get_validation(validation_id: str):
    val = db_ops.get_validation(validation_id)
    if not val:
        raise HTTPException(404, "Validation not found.")
    return val


@app.get("/api/conversions/{conversion_id}/download/d0")
def download_d0_output(conversion_id: str):
    """Download the D.0 output for an F6→D0 conversion."""
    conv = db_ops.get_conversion(conversion_id)
    if not conv or not conv.get("d0_output"):
        raise HTTPException(404, "Not found or D.0 output not available.")
    filename = f'd0_{conv["filename"]}'
    return StreamingResponse(
        io.BytesIO(conv["d0_output"].encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
