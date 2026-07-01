"""
db_ops.py

All database read/write operations. Backend routes import from here.
No business logic — just SQL wrapped in typed functions.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from database import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


# ── Conversions ───────────────────────────────────────────────────────────────

def create_conversion(
    filename:   str,
    d0_input:   str,
    batch_id:   Optional[str] = None,
    direction:  str = 'D0_TO_F6',
    input_text: str = '',
) -> str:
    cid = _uid()
    with db() as conn:
        conn.execute("""
            INSERT INTO conversions
                (id, batch_id, filename, d0_input, input_text, direction, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
        """, (cid, batch_id, filename, d0_input, input_text or d0_input, direction, _now()))
    return cid


def mark_conversion_processing(conversion_id: str):
    with db() as conn:
        conn.execute(
            "UPDATE conversions SET status='processing' WHERE id=%s",
            (conversion_id,),
        )


def complete_conversion(
    conversion_id:    str,
    transaction_type: str,
    f6_output:        str  = '',
    summary:          dict = None,
    rule_set_version: str  = 'default',
    d0_output:        str  = '',
):
    summary = summary or {}
    with db() as conn:
        conn.execute("""
            UPDATE conversions SET
                status             = 'success',
                transaction_type   = ?,
                f6_output          = ?,
                d0_output          = ?,
                fields_added       = ?,
                fields_carried     = ?,
                fields_transformed = ?,
                fields_removed     = ?,
                fields_modified    = ?,
                fields_missing     = ?,
                warnings_count     = ?,
                errors_count       = ?,
                rule_set_version   = ?,
                completed_at       = ?
            WHERE id = %s
        """, (
            transaction_type, f6_output, d0_output,
            summary.get('added', 0),       summary.get('carried', 0),
            summary.get('transformed', 0), summary.get('removed', 0),
            summary.get('modified', 0),    summary.get('missing', 0),
            summary.get('warnings', 0),    summary.get('errors', 0),
            rule_set_version, _now(),
            conversion_id,
        ))


def fail_conversion(conversion_id: str, error_message: str):
    with db() as conn:
        conn.execute("""
            UPDATE conversions SET
                status        = 'failed',
                error_message = %s,
                completed_at  = %s
            WHERE id = %s
        """, (error_message, _now(), conversion_id))


def get_conversion(conversion_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM conversions WHERE id=%s", (conversion_id,)
        ).fetchone()
        return dict(row) if row else None


def list_conversions(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if batch_id:
        clauses.append("batch_id = %s")
        params.append(batch_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params += [limit, offset]
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM conversions {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def count_conversions(status: Optional[str] = None) -> int:
    with db() as conn:
        if status:
            return conn.execute(
                "SELECT COUNT(*) AS count FROM conversions WHERE status=%s", (status,)
            ).fetchone()['count']
        return conn.execute("SELECT COUNT(*) AS count FROM conversions").fetchone()['count']


# ── Audit Entries ─────────────────────────────────────────────────────────────

def insert_audit_entries(conversion_id: str, entries: list[dict]):
    if not entries:
        return
    with db() as conn:
        conn.executemany("""
            INSERT INTO audit_entries (
                conversion_id, segment, occurrence,
                from_field_id, to_field_id, field_name,
                change_type, old_value, new_value,
                rule_applied, notes,
                condition_evaluated, condition_passed, condition_expression
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [
            (
                conversion_id,
                e.get('segment', ''),          e.get('occurrence', 1),
                e.get('from_field_id', ''),    e.get('to_field_id', ''),
                e.get('field_name', ''),       e.get('change_type', ''),
                e.get('old_value', ''),        e.get('new_value', ''),
                e.get('rule_applied', ''),     e.get('notes', ''),
                int(e.get('condition_evaluated', False)),
                int(e.get('condition_passed', True)),
                e.get('condition_expression', ''),
            )
            for e in entries
        ])


def get_audit_entries(
    conversion_id: str,
    segment: Optional[str] = None,
    change_type: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    clauses = ["conversion_id = %s"]
    params: list = [conversion_id]
    if segment:
        clauses.append("segment = %s")
        params.append(segment)
    if change_type:
        clauses.append("change_type = %s")
        params.append(change_type)
    if search:
        clauses.append(
            "(field_name LIKE %s OR from_field_id LIKE %s OR old_value LIKE %s OR new_value LIKE %s)"
        )
        s = f'%{search}%'
        params += [s, s, s, s]
    where = "WHERE " + " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_entries {where} ORDER BY id ASC", params
        ).fetchall()
        return [dict(r) for r in rows]


# ── Audit Findings ────────────────────────────────────────────────────────────

def insert_audit_findings(conversion_id: str, findings: list[dict]):
    if not findings:
        return
    with db() as conn:
        conn.executemany("""
            INSERT INTO audit_findings
                (conversion_id, severity, code, message, segment, field_id, occurrence)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, [
            (
                conversion_id,
                f.get('severity', 'WARN'), f.get('code', ''),
                f.get('message', ''),      f.get('segment', ''),
                f.get('field_id', ''),     f.get('occurrence', 1),
            )
            for f in findings
        ])


def get_audit_findings(conversion_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_findings WHERE conversion_id=%s ORDER BY severity DESC, id ASC",
            (conversion_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Agent Steps ───────────────────────────────────────────────────────────────

def insert_agent_steps(conversion_id: str, steps: list[dict]):
    if not steps:
        return
    with db() as conn:
        conn.executemany("""
            INSERT INTO agent_steps
                (conversion_id, step_order, step_id, label, status, detail)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, [
            (
                conversion_id, i,
                s.get('id', ''), s.get('label', ''),
                s.get('status', 'complete'), s.get('detail', ''),
            )
            for i, s in enumerate(steps)
        ])


def upsert_agent_step(conversion_id: str, step: dict) -> None:
    """
    Insert or update a single agent step row.
    Called incrementally during _execute_forward so the DB-polling SSE
    generator can observe progress in near-real-time rather than only
    seeing the full step list once the entire conversion finishes.
    """
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM agent_steps WHERE conversion_id=%s AND step_id=%s",
            (conversion_id, step['id']),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE agent_steps SET status=%s, detail=%s WHERE id=%s",
                (step.get('status', 'complete'), step.get('detail', ''), existing['id']),
            )
        else:
            conn.execute(
                "INSERT INTO agent_steps (conversion_id, step_order, step_id, label, status, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    conversion_id,
                    step.get('step_order', 0),
                    step['id'],
                    step.get('label', step['id']),
                    step.get('status', 'complete'),
                    step.get('detail', ''),
                ),
            )


def get_agent_steps(conversion_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_steps WHERE conversion_id=%s ORDER BY step_order ASC",
            (conversion_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Batches ───────────────────────────────────────────────────────────────────

def create_batch(name: str, total_files: int) -> str:
    bid = _uid()
    with db() as conn:
        conn.execute("""
            INSERT INTO batches (id, name, total_files, status, created_at)
            VALUES (%s, %s, %s, 'pending', %s)
        """, (bid, name, total_files, _now()))
    return bid


def update_batch_progress(batch_id: str):
    """Recalculate batch status and counters from its child conversions."""
    with db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) as failed
            FROM conversions WHERE batch_id=%s
        """, (batch_id,)).fetchone()

        total     = row['total']     or 0
        completed = row['completed'] or 0
        failed    = row['failed']    or 0
        done      = completed + failed

        if done == 0:
            status = 'processing'
        elif done < total:
            status = 'processing'
        elif failed == total:
            status = 'failed'
        elif failed > 0:
            status = 'partial'
        else:
            status = 'complete'

        completed_at = _now() if done == total else None
        conn.execute("""
            UPDATE batches SET
                status          = %s,
                completed_files = %s,
                failed_files    = %s,
                completed_at    = COALESCE(completed_at, %s)
            WHERE id = %s
        """, (status, completed, failed, completed_at, batch_id))


def get_batch(batch_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=%s", (batch_id,)).fetchone()
        return dict(row) if row else None


def list_batches(limit: int = 20, offset: int = 0) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Rule Sets ─────────────────────────────────────────────────────────────────

def get_active_rule_set() -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM rule_sets WHERE is_active=1 LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_rule_sets() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM rule_sets ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def create_rule_set(
    name: str,
    description: str = '',
    version: str = '1.0',
    source_pdf: str = '',
) -> str:
    rsid = _uid()
    t = _now()
    with db() as conn:
        conn.execute("""
            INSERT INTO rule_sets
                (id, name, description, version, source_pdf, is_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
        """, (rsid, name, description, version, source_pdf, t, t))
    return rsid


def activate_rule_set(rule_set_id: str):
    with db() as conn:
        conn.execute("UPDATE rule_sets SET is_active=0")
        conn.execute("UPDATE rule_sets SET is_active=1 WHERE id=%s", (rule_set_id,))


# ── Rules ─────────────────────────────────────────────────────────────────────

def insert_rules_bulk(rule_set_id: str, rules: list[dict]):
    t = _now()
    rows = [
        (
            _uid(), rule_set_id,
            r.get('transaction_type', 'RETAIL'),
            r.get('segment_id', ''),
            r.get('field_id', ''),
            r.get('field_name', r.get('field_id', '')),
            r.get('action', 'carry'),
            json.dumps(r),
            int(r.get('mandatory_f6', False)),
            int(r.get('warn_if_empty', False)),
            r.get('warn_code', ''),
            r.get('warn_severity', ''),
            r.get('notes', ''),
            t, t,
        )
        for r in rules
    ]
    with db() as conn:
        conn.executemany("""
            INSERT INTO rules (
                id, rule_set_id, transaction_type, segment_id,
                field_id, field_name, action, rule_json,
                mandatory_f6, warn_if_empty, warn_code, warn_severity,
                notes, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, rows)
        conn.execute(
            "UPDATE rule_sets SET total_rules=%s, updated_at=%s WHERE id=%s",
            (len(rows), t, rule_set_id),
        )


def list_rules(
    rule_set_id: str,
    transaction_type: Optional[str] = None,
    segment_id: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    clauses = ["rule_set_id = %s"]
    params: list = [rule_set_id]
    if transaction_type:
        clauses.append("transaction_type = %s")
        params.append(transaction_type)
    if segment_id:
        clauses.append("segment_id = %s")
        params.append(segment_id)
    if search:
        clauses.append("(field_id LIKE %s OR field_name LIKE %s)")
        s = f'%{search}%'
        params += [s, s]
    where = "WHERE " + " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM rules {where} ORDER BY segment_id, field_id",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def update_rule(rule_id: str, rule_data: dict):
    with db() as conn:
        conn.execute("""
            UPDATE rules SET
                field_name    = %s,
                action        = %s,
                rule_json     = %s,
                mandatory_f6  = %s,
                warn_if_empty = %s,
                warn_code     = %s,
                warn_severity = %s,
                notes         = %s,
                updated_at    = %s
            WHERE id = %s
        """, (
            rule_data.get('field_name', ''),
            rule_data.get('action', 'carry'),
            json.dumps(rule_data),
            int(rule_data.get('mandatory_f6', False)),
            int(rule_data.get('warn_if_empty', False)),
            rule_data.get('warn_code', ''),
            rule_data.get('warn_severity', ''),
            rule_data.get('notes', ''),
            _now(),
            rule_id,
        ))


def delete_rule(rule_id: str):
    with db() as conn:
        conn.execute("DELETE FROM rules WHERE id=%s", (rule_id,))


# ── Validations ──────────────────────────────────────────────────────────────

def save_validation(
    transaction_type: str,
    overall_status:   str,
    summary:          dict,
    categories:       dict,
    checks:           list[dict],
    rule_set_id:      Optional[str] = None,
    parse_errors:     list          = None,
) -> str:
    vid = _uid()
    with db() as conn:
        conn.execute("""
            INSERT INTO validations (
                id, transaction_type, overall_status,
                score, total_checks, passed, warnings, errors,
                rule_set_id, categories_json, checks_json, parse_errors_json,
                created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            vid, transaction_type, overall_status,
            summary.get('score', 0),
            summary.get('total_checks', 0),
            summary.get('passed', 0),
            summary.get('warnings', 0),
            summary.get('errors', 0),
            rule_set_id,
            json.dumps(categories or {}),
            json.dumps(checks or []),
            json.dumps(parse_errors or []),
            _now(),
        ))
    return vid


def get_validation(validation_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM validations WHERE id=%s", (validation_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d['categories']   = json.loads(d.pop('categories_json', '{}'))
        d['checks']       = json.loads(d.pop('checks_json', '[]'))
        d['parse_errors'] = json.loads(d.pop('parse_errors_json', '[]'))
        return d


def list_validations(
    limit:  int = 20,
    offset: int = 0,
    status: Optional[str] = None,
) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("overall_status = %s")
        params.append(status)
    where   = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params += [limit, offset]
    with db() as conn:
        rows = conn.execute(
            f"SELECT id, transaction_type, overall_status, score, total_checks, "
            f"passed, warnings, errors, rule_set_id, created_at "
            f"FROM validations {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def count_validations(status: Optional[str] = None) -> int:
    with db() as conn:
        if status:
            return conn.execute(
                "SELECT COUNT(*) AS count FROM validations WHERE overall_status=%s", (status,)
            ).fetchone()['count']
        return conn.execute("SELECT COUNT(*) AS count FROM validations").fetchone()['count']


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_db_stats() -> dict:
    with db() as conn:
        rs_row = conn.execute(
            "SELECT name FROM rule_sets WHERE is_active=1"
        ).fetchone()
        total_rules = conn.execute("""
            SELECT COUNT(*) AS count FROM rules WHERE rule_set_id=(
                SELECT id FROM rule_sets WHERE is_active=1
            )
        """).fetchone()['count']
        return {
            'total_conversions': conn.execute("SELECT COUNT(*) AS count FROM conversions").fetchone()['count'],
            'successful':        conn.execute("SELECT COUNT(*) AS count FROM conversions WHERE status='success'").fetchone()['count'],
            'failed':            conn.execute("SELECT COUNT(*) AS count FROM conversions WHERE status='failed'").fetchone()['count'],
            'processing':        conn.execute("SELECT COUNT(*) AS count FROM conversions WHERE status='processing'").fetchone()['count'],
            'total_batches':     conn.execute("SELECT COUNT(*) AS count FROM batches").fetchone()['count'],
            'total_rules':       total_rules,
            'active_rule_set':   rs_row['name'] if rs_row else None,
        }


def get_flagged_count() -> int:
    """Quick count for sidebar badge — reads file directly, no DB query."""
    from pathlib import Path
    import json as _json
    p = Path('ingestion_output/flagged_for_review.json')
    if not p.exists():
        return 0
    try:
        return len(_json.loads(p.read_text()))
    except Exception:
        return 0


# ── LLM Decisions ─────────────────────────────────────────────────────────────

def insert_llm_decisions(
    conversion_id: str,
    decisions:     list,   # list[LLMDecision] — avoid circular import with engine
    model:         str = "claude-sonnet-4-6",
) -> None:
    """Persist LLM decisions for compliance audit trail."""
    if not decisions:
        return
    with db() as conn:
        conn.executemany("""
            INSERT INTO llm_decisions (
                conversion_id, field_id, field_name, segment_id,
                resolved_value, original_value, reasoning,
                confidence, finding_code, action,
                llm_model, phi_was_masked, was_overridden
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [
            (
                conversion_id,
                d.field_id, d.field_name, d.segment_id,
                d.resolved_value, d.original_value, d.reasoning,
                d.confidence, d.finding_code, d.action,
                model, int(d.phi_was_masked), 0,
            )
            for d in decisions
        ])


def get_llm_decisions(conversion_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM llm_decisions WHERE conversion_id=%s ORDER BY id ASC",
            (conversion_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_llm_stats() -> dict:
    with db() as conn:
        total    = conn.execute("SELECT COUNT(*) AS count FROM llm_decisions").fetchone()['count']
        resolved = conn.execute(
            "SELECT COUNT(*) AS count FROM llm_decisions WHERE action='RESOLVED'"
        ).fetchone()['count']
        unres    = conn.execute(
            "SELECT COUNT(*) AS count FROM llm_decisions WHERE action='UNRESOLVABLE'"
        ).fetchone()['count']
        high     = conn.execute(
            "SELECT COUNT(*) AS count FROM llm_decisions WHERE confidence='HIGH'"
        ).fetchone()['count']
        medium   = conn.execute(
            "SELECT COUNT(*) AS count FROM llm_decisions WHERE confidence='MEDIUM'"
        ).fetchone()['count']
        low      = conn.execute(
            "SELECT COUNT(*) AS count FROM llm_decisions WHERE confidence='LOW'"
        ).fetchone()['count']
        convs    = conn.execute(
            "SELECT COUNT(DISTINCT conversion_id) AS count FROM llm_decisions"
        ).fetchone()['count']
    return {
        "total_decisions":          total,
        "resolved":                 resolved,
        "unresolvable":             unres,
        "conversions_with_llm":     convs,
        "confidence_high":          high,
        "confidence_medium":        medium,
        "confidence_low":           low,
    }
