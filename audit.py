"""
audit.py
========
Structured audit logging for the Text-to-SQL system.

Every query execution — successful or not — is recorded with full
context: user identity, role, generated SQL, execution time, result
summary, and rows affected.

Dual-write strategy
-------------------
  1. **PostgreSQL** ``audit_log`` table  – queryable, persistent, used
     by the ``GET /v1/audit`` endpoint (Admin only).
  2. **JSON-lines file** – append-only fallback for reliability; sits
     at ``guardrails/rbac_audit.log``.

Public surface
--------------
    AuditRecord   – Pydantic model for a single audit entry
    AuditLogger   – Writes records to PostgreSQL + file
    ensure_audit_table() – Creates the audit_log table if missing
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from pydantic import BaseModel, Field


# =========================================================
# LOGGING (console + file)
# =========================================================

_logger = logging.getLogger("audit")

# JSON-lines file sits beside the guardrails logs
_AUDIT_FILE = Path(__file__).parent / "guardrails" / "rbac_audit.log"

_file_logger = logging.getLogger("audit_file")
_file_logger.setLevel(logging.INFO)
_file_logger.propagate = False

if not _file_logger.handlers:
    _fh = logging.FileHandler(_AUDIT_FILE, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(message)s"))
    _file_logger.addHandler(_fh)


# =========================================================
# AUDIT RECORD MODEL
# =========================================================

class AuditRecord(BaseModel):
    """
    A single audit log entry capturing full execution context.
    """

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique record identifier.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 UTC timestamp.",
    )
    user_id: str = Field(
        description="Identifier of the user who ran the query.",
    )
    role: str = Field(
        description="Role of the user (viewer / editor / admin).",
    )
    question: Optional[str] = Field(
        default=None,
        description="Original natural-language question.",
    )
    generated_sql: Optional[str] = Field(
        default=None,
        description="Raw SQL produced by the LLM.",
    )
    safe_sql: Optional[str] = Field(
        default=None,
        description="Guardrail-modified SQL that was actually executed.",
    )
    execution_time_ms: float = Field(
        default=0.0,
        description="Wall-clock execution time in milliseconds.",
    )
    success: bool = Field(
        default=False,
        description="Whether the query executed successfully.",
    )
    row_count: int = Field(
        default=0,
        description="Number of rows returned (SELECT) or affected (DML).",
    )
    rows_affected: int = Field(
        default=0,
        description="Rows modified by DML (INSERT/UPDATE/DELETE).",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if execution failed.",
    )
    risk_level: str = Field(
        default="safe",
        description="Risk classification: safe / moderate / risky.",
    )
    permission_granted: bool = Field(
        default=True,
        description="Whether the user's role had the required permission.",
    )
    ip_address: str = Field(
        default="",
        description="Client IP address.",
    )


# =========================================================
# SQL TO CREATE THE AUDIT TABLE
# =========================================================

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id         VARCHAR(255) NOT NULL,
    role            VARCHAR(20)  NOT NULL,
    question        TEXT,
    generated_sql   TEXT,
    safe_sql        TEXT,
    execution_time_ms DOUBLE PRECISION DEFAULT 0,
    success         BOOLEAN DEFAULT FALSE,
    row_count       INTEGER DEFAULT 0,
    rows_affected   INTEGER DEFAULT 0,
    error           TEXT,
    risk_level      VARCHAR(20) DEFAULT 'safe',
    permission_granted BOOLEAN DEFAULT TRUE,
    ip_address      VARCHAR(45) DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
    ON audit_log (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_user_id
    ON audit_log (user_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_role
    ON audit_log (role);
"""

_INSERT_SQL = """
INSERT INTO audit_log (
    id, timestamp, user_id, role, question, generated_sql, safe_sql,
    execution_time_ms, success, row_count, rows_affected,
    error, risk_level, permission_granted, ip_address
) VALUES (
    %(id)s, %(timestamp)s, %(user_id)s, %(role)s, %(question)s,
    %(generated_sql)s, %(safe_sql)s, %(execution_time_ms)s,
    %(success)s, %(row_count)s, %(rows_affected)s,
    %(error)s, %(risk_level)s, %(permission_granted)s, %(ip_address)s
);
"""


# =========================================================
# HELPER: build admin DSN for audit writes
# =========================================================

def _admin_dsn() -> str:
    """Build a DSN using the admin DB user (needs INSERT on audit_log)."""
    host   = os.environ.get("PG_HOST",  "localhost")
    port   = os.environ.get("PG_PORT",  "5432")
    dbname = os.environ.get("PG_DB",    "college_2")
    user   = os.environ.get("PG_ADMIN_DB_USER", os.environ.get("PG_ADMIN_USER", "postgres"))
    pwd    = os.environ.get("PG_ADMIN_DB_PASSWORD", os.environ.get("PG_ADMIN_PASSWORD", ""))
    return f"host={host} port={port} dbname={dbname} user={user} password={pwd}"


# =========================================================
# ENSURE AUDIT TABLE EXISTS
# =========================================================

def ensure_audit_table(dsn: str | None = None) -> None:
    """
    Create the ``audit_log`` table and its indexes if they do not exist.

    Uses the admin DSN by default.
    """
    dsn = dsn or _admin_dsn()
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
        conn.close()
        _logger.info("audit_log table ensured.")
    except psycopg2.Error as exc:
        _logger.warning("Could not create audit_log table: %s", exc)


# =========================================================
# AUDIT LOGGER
# =========================================================

class AuditLogger:
    """
    Dual-write audit logger: PostgreSQL table + JSON-lines file.

    Usage::

        logger = AuditLogger()
        logger.log(AuditRecord(user_id="alice", role="viewer", ...))
        records = logger.query_logs(limit=50)
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or _admin_dsn()

    # ── Write ───────────────────────────────────────

    def log(self, record: AuditRecord) -> None:
        """
        Write *record* to PostgreSQL and to the JSON-lines file.

        PostgreSQL failures are logged but do not raise — the file
        write acts as a reliable fallback.
        """
        record_dict = record.model_dump()

        # 1. JSON-lines file (always succeeds unless disk is full)
        try:
            _file_logger.info(json.dumps(record_dict, default=str))
        except Exception as exc:
            _logger.warning("Audit file write failed: %s", exc)

        # 2. PostgreSQL
        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, record_dict)
            conn.close()
        except psycopg2.Error as exc:
            _logger.warning("Audit DB write failed: %s", exc)

    # ── Query ──────────────────────────────────────

    def query_logs(
        self,
        user_id: str | None = None,
        role: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query audit records from PostgreSQL with optional filters.

        Returns a list of dicts, newest first.
        """
        conditions = []
        params: dict[str, Any] = {}

        if user_id:
            conditions.append("user_id = %(user_id)s")
            params["user_id"] = user_id
        if role:
            conditions.append("role = %(role)s")
            params["role"] = role

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        sql = f"""
            SELECT id, timestamp, user_id, role, question, generated_sql,
                   safe_sql, execution_time_ms, success, row_count,
                   rows_affected, error, risk_level, permission_granted,
                   ip_address
            FROM audit_log
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT %(limit)s OFFSET %(offset)s;
        """
        params["limit"] = limit
        params["offset"] = offset

        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            conn.close()
            # Convert to plain dicts with serialisable values
            return [
                {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, uuid.UUID) else v)
                 for k, v in dict(row).items()}
                for row in rows
            ]
        except psycopg2.Error as exc:
            _logger.warning("Audit query failed: %s", exc)
            return []

    def count_logs(
        self,
        user_id: str | None = None,
        role: str | None = None,
    ) -> int:
        """Return total count of audit records matching the filters."""
        conditions = []
        params: dict[str, Any] = {}

        if user_id:
            conditions.append("user_id = %(user_id)s")
            params["user_id"] = user_id
        if role:
            conditions.append("role = %(role)s")
            params["role"] = role

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        sql = f"SELECT COUNT(*) FROM audit_log {where_clause};"

        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql, params)
                result = cur.fetchone()
            conn.close()
            return result[0] if result else 0
        except psycopg2.Error as exc:
            _logger.warning("Audit count failed: %s", exc)
            return 0
