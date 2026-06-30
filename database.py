"""
database.py

SQLite database layer using Python's built-in sqlite3.
No ORM — direct SQL for transparency and control.

Call init_db() once on startup to create all tables.
Call seed_from_rules_folder() after init_db() to bootstrap from rules/ JSON files.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get('NCPDP_DB_PATH', './ncpdp_converter.db')


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row           # rows behave like dicts
    conn.execute('PRAGMA journal_mode=WAL')   # concurrent reads while writing
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


@contextmanager
def db():
    """Context manager: auto-commits on success, rolls back on exception."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist. Safe to call on every startup."""
    with db() as conn:
        conn.executescript("""

        -- ── Batches ──────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS batches (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            total_files     INTEGER NOT NULL DEFAULT 0,
            completed_files INTEGER NOT NULL DEFAULT 0,
            failed_files    INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            completed_at    TEXT
        );

        -- ── Conversions ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS conversions (
            id                  TEXT PRIMARY KEY,
            batch_id            TEXT REFERENCES batches(id),
            filename            TEXT NOT NULL,
            transaction_type    TEXT,
            status              TEXT NOT NULL DEFAULT 'pending',
            d0_input            TEXT NOT NULL,
            f6_output           TEXT,
            error_message       TEXT,
            total_fields        INTEGER DEFAULT 0,
            fields_added        INTEGER DEFAULT 0,
            fields_carried      INTEGER DEFAULT 0,
            fields_transformed  INTEGER DEFAULT 0,
            fields_removed      INTEGER DEFAULT 0,
            fields_modified     INTEGER DEFAULT 0,
            fields_missing      INTEGER DEFAULT 0,
            warnings_count      INTEGER DEFAULT 0,
            errors_count        INTEGER DEFAULT 0,
            created_at          TEXT NOT NULL,
            completed_at        TEXT,
            rule_set_version    TEXT
        );

        -- ── Audit Entries ─────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS audit_entries (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            conversion_id        TEXT NOT NULL REFERENCES conversions(id) ON DELETE CASCADE,
            segment              TEXT NOT NULL,
            occurrence           INTEGER NOT NULL DEFAULT 1,
            from_field_id        TEXT NOT NULL DEFAULT '',
            to_field_id          TEXT NOT NULL DEFAULT '',
            field_name           TEXT NOT NULL DEFAULT '',
            change_type          TEXT NOT NULL DEFAULT '',
            old_value            TEXT NOT NULL DEFAULT '',
            new_value            TEXT NOT NULL DEFAULT '',
            rule_applied         TEXT NOT NULL DEFAULT '',
            notes                TEXT NOT NULL DEFAULT '',
            condition_evaluated  INTEGER NOT NULL DEFAULT 0,
            condition_passed     INTEGER NOT NULL DEFAULT 1,
            condition_expression TEXT NOT NULL DEFAULT ''
        );

        -- ── Audit Findings ────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS audit_findings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            conversion_id TEXT NOT NULL REFERENCES conversions(id) ON DELETE CASCADE,
            severity      TEXT NOT NULL,
            code          TEXT NOT NULL DEFAULT '',
            message       TEXT NOT NULL DEFAULT '',
            segment       TEXT NOT NULL DEFAULT '',
            field_id      TEXT NOT NULL DEFAULT '',
            occurrence    INTEGER NOT NULL DEFAULT 1
        );

        -- ── Agent Steps ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS agent_steps (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            conversion_id TEXT NOT NULL REFERENCES conversions(id) ON DELETE CASCADE,
            step_order    INTEGER NOT NULL,
            step_id       TEXT NOT NULL,
            label         TEXT NOT NULL,
            status        TEXT NOT NULL,
            detail        TEXT NOT NULL DEFAULT ''
        );

        -- ── Rule Sets ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS rule_sets (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            description   TEXT NOT NULL DEFAULT '',
            is_active     INTEGER NOT NULL DEFAULT 0,
            version       TEXT NOT NULL DEFAULT '1.0',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            source_pdf    TEXT NOT NULL DEFAULT '',
            total_rules   INTEGER NOT NULL DEFAULT 0
        );

        -- ── Rules ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS rules (
            id                TEXT PRIMARY KEY,
            rule_set_id       TEXT NOT NULL REFERENCES rule_sets(id) ON DELETE CASCADE,
            transaction_type  TEXT NOT NULL,
            segment_id        TEXT NOT NULL,
            field_id          TEXT NOT NULL,
            field_name        TEXT NOT NULL DEFAULT '',
            action            TEXT NOT NULL,
            rule_json         TEXT NOT NULL,
            mandatory_f6      INTEGER NOT NULL DEFAULT 0,
            warn_if_empty     INTEGER NOT NULL DEFAULT 0,
            warn_code         TEXT NOT NULL DEFAULT '',
            warn_severity     TEXT NOT NULL DEFAULT '',
            notes             TEXT NOT NULL DEFAULT '',
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );

        -- ── Validations ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS validations (
            id               TEXT PRIMARY KEY,
            transaction_type TEXT NOT NULL,
            overall_status   TEXT NOT NULL,
            score            INTEGER NOT NULL DEFAULT 0,
            total_checks     INTEGER NOT NULL DEFAULT 0,
            passed           INTEGER NOT NULL DEFAULT 0,
            warnings         INTEGER NOT NULL DEFAULT 0,
            errors           INTEGER NOT NULL DEFAULT 0,
            rule_set_id      TEXT,
            categories_json  TEXT NOT NULL DEFAULT '{}',
            checks_json      TEXT NOT NULL DEFAULT '[]',
            parse_errors_json TEXT NOT NULL DEFAULT '[]',
            created_at       TEXT NOT NULL
        );

        -- ── Rule Resolutions ──────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS rule_resolutions (
            id                TEXT PRIMARY KEY,
            resolution        TEXT NOT NULL,
            entry_id          TEXT NOT NULL,
            field_id          TEXT NOT NULL DEFAULT '',
            segment_id        TEXT NOT NULL DEFAULT '',
            transaction_type  TEXT NOT NULL DEFAULT '',
            rejection_reason  TEXT NOT NULL DEFAULT '',
            validator_status  TEXT NOT NULL DEFAULT '',
            issues_json       TEXT NOT NULL DEFAULT '[]',
            resolved_at       TEXT NOT NULL,
            resolved_by       TEXT NOT NULL DEFAULT 'user'
        );
        CREATE INDEX IF NOT EXISTS idx_resolutions_resolved
            ON rule_resolutions(resolved_at DESC);

        -- ── Indexes ───────────────────────────────────────────────────────
        CREATE INDEX IF NOT EXISTS idx_conversions_batch   ON conversions(batch_id);
        CREATE INDEX IF NOT EXISTS idx_conversions_status  ON conversions(status);
        CREATE INDEX IF NOT EXISTS idx_conversions_created ON conversions(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_entries_conv  ON audit_entries(conversion_id);
        CREATE INDEX IF NOT EXISTS idx_audit_findings_conv ON audit_findings(conversion_id);
        CREATE INDEX IF NOT EXISTS idx_agent_steps_conv    ON agent_steps(conversion_id);
        CREATE INDEX IF NOT EXISTS idx_rules_set           ON rules(rule_set_id, transaction_type, segment_id);
        CREATE INDEX IF NOT EXISTS idx_validations_created ON validations(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_validations_status  ON validations(overall_status);

        """)
    print(f'[DB] Initialized: {DB_PATH}')


def migrate_db():
    """
    Safe incremental migrations. Each ALTER TABLE is wrapped in try/except
    because sqlite3 raises OperationalError if the column already exists.
    Call after init_db() on every startup.
    """
    column_migrations = [
        "ALTER TABLE conversions ADD COLUMN direction TEXT NOT NULL DEFAULT 'D0_TO_F6'",
        "ALTER TABLE conversions ADD COLUMN input_text TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE conversions ADD COLUMN d0_output TEXT",
    ]
    structural_migrations = [
        # LLM hybrid audit trail — created here (not init_db) so existing DBs get it safely
        """CREATE TABLE IF NOT EXISTS llm_decisions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            conversion_id    TEXT NOT NULL REFERENCES conversions(id) ON DELETE CASCADE,
            field_id         TEXT NOT NULL DEFAULT '',
            field_name       TEXT NOT NULL DEFAULT '',
            segment_id       TEXT NOT NULL DEFAULT '',
            resolved_value   TEXT NOT NULL DEFAULT '',
            original_value   TEXT NOT NULL DEFAULT '',
            reasoning        TEXT NOT NULL DEFAULT '',
            confidence       TEXT NOT NULL DEFAULT '',
            finding_code     TEXT NOT NULL DEFAULT '',
            action           TEXT NOT NULL DEFAULT '',
            llm_model        TEXT NOT NULL DEFAULT '',
            phi_was_masked   INTEGER NOT NULL DEFAULT 1,
            was_overridden   INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_llm_conv ON llm_decisions(conversion_id)",
    ]
    with db() as conn:
        for ddl in column_migrations:
            try:
                conn.execute(ddl)
            except Exception:
                pass  # column already exists
        for ddl in structural_migrations:
            try:
                conn.execute(ddl)
            except Exception:
                pass  # table/index already exists


def seed_from_rules_folder(rules_dir: str) -> None:
    """
    If no rule sets exist and rules/ folder has JSON files, auto-import them.
    This bootstraps the DB from the existing rules/ folder on first startup.
    """
    import db_ops  # local import avoids circular dependency at module level

    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM rule_sets").fetchone()[0]
        if count > 0:
            return  # already seeded — don't overwrite

    rules_path = Path(rules_dir)
    if not rules_path.exists():
        return

    rule_files = sorted(rules_path.glob("*.json"))
    if not rule_files:
        return

    rsid = db_ops.create_rule_set(
        name="Default (from rules/ folder)",
        description="Auto-imported from rules/ JSON files on first startup",
        version="1.0",
    )

    all_rules: list[dict] = []
    for fpath in rule_files:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            tx_type = data.get("transaction_type", "RETAIL")
            for seg_id, seg_rules in data.get("segments", {}).items():
                for rule in (seg_rules or []):
                    all_rules.append({
                        **rule,
                        "transaction_type": tx_type,
                        "segment_id": seg_id,
                    })
        except Exception as exc:
            print(f'[DB] Warning: could not parse {fpath.name}: {exc}')

    if all_rules:
        db_ops.insert_rules_bulk(rsid, all_rules)
        db_ops.activate_rule_set(rsid)
        print(f'[DB] Seeded {len(all_rules)} rules from {len(rule_files)} file(s) → rule_set {rsid}')
