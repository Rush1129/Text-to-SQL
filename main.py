"""
main.py
======
FastAPI REST API for the Text-to-SQL pipeline.

Endpoints
---------
  POST  /v1/query    – Run a natural-language question through the pipeline
  GET   /v1/schema   – Return the database schema
  GET   /v1/history  – Return past queries for a session
  GET   /v1/audit    – Return audit log entries (Admin only)

RBAC
----
  Roles: viewer (default), editor, admin
  Headers: X-User-Id, X-User-Role
  Permission checks enforced before query execution.

Run with:
    uvicorn main:app --reload
OpenAPI docs available at:
    http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import pipeline as pl  # shared pipeline objects + run_query()
from audit import AuditLogger, AuditRecord, ensure_audit_table
from rbac import (
    Permission,
    PermissionDeniedError,
    Role,
    UserContext,
    check_permission,
    has_permission,
    permission_for_sql,
    resolve_user_context,
)

# =========================================================
# LOGGING
# =========================================================

logger = logging.getLogger("api")

# =========================================================
# APP SETUP
# =========================================================

app = FastAPI(
    title="Text-to-SQL API",
    description=(
        "Natural-language to SQL query engine with guardrails, "
        "back-translation verification, sanity checking, "
        "composite confidence scoring, and role-based access control."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# STARTUP — ensure audit table exists
# =========================================================

@app.on_event("startup")
async def _startup():
    ensure_audit_table()
    logger.info("Audit table ensured on startup.")

# =========================================================
# AUDIT LOGGER INSTANCE
# =========================================================

audit_logger = AuditLogger()

# =========================================================
# SESSION HISTORY  (in-memory; cleared on restart)
# =========================================================

# Maps session_id -> list of result dicts (newest last)
_history: dict[str, list[dict]] = defaultdict(list)

# =========================================================
# RBAC DEPENDENCY
# =========================================================

def _get_user_context(request: Request) -> UserContext:
    """
    Extract user context from request headers.

    Headers:
        X-User-Id   – User identifier (defaults to 'anonymous')
        X-User-Role – User role: viewer, editor, admin (defaults to 'viewer')

    Returns UserContext. Raises HTTPException(400) for invalid roles.
    """
    user_id  = request.headers.get("X-User-Id", "")
    role_str = request.headers.get("X-User-Role", "")
    ip       = request.client.host if request.client else ""

    try:
        return resolve_user_context(
            user_id=user_id,
            role_str=role_str,
            ip_address=ip,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

# =========================================================
# REQUEST / RESPONSE MODELS
# =========================================================


class QueryRequest(BaseModel):
    """Request body for POST /v1/query."""

    question: str = Field(
        ...,
        min_length=3,
        description="Natural-language question to convert to SQL.",
        examples=["How many students are enrolled in each department?"],
    )
    session_id: str = Field(
        default="default",
        description=(
            "Session identifier used to group queries in GET /v1/history. "
            "Defaults to 'default'."
        ),
    )
    confirmed: bool = Field(
        default=False,
        description=(
            "Set to true when the user has acknowledged a risk warning "
            "(moderate or risky query) and explicitly wants to proceed."
        ),
    )


class QueryResponse(BaseModel):
    """Full response from POST /v1/query."""

    # mirrors QueryResult fields — validated by Pydantic for the response
    question:                 str
    sql:                      str
    safe_sql:                 str
    explanation:              str
    tables_accessed:          list[str]
    columns_accessed:         list[dict]
    execution_results:        list[dict]
    dataframe:                list[dict]        # DataFrame rows serialised from sandbox result
    row_count:                int
    execution_time_ms:        float
    execution_error:          Optional[str]
    sql_valid:                bool
    validation_message:       str
    guardrail_allowed:        bool
    guardrail_warnings:       list[str]
    guardrail_limit_applied:  bool
    risk_level:               str               # safe | moderate | risky
    risk_warning:             str               # human-readable 1-3 line warning
    back_translated_question: str
    alignment_score:          float
    alignment_label:          str
    alignment_flagged:        bool
    alignment_flag_reason:    Optional[str]
    judge_reason:             Optional[str]
    sanity_anomalies:         list[dict]
    sanity_pass_rate:         float
    sanity_summary:           str
    confidence:               dict
    needs_clarification:      bool
    clarification_request:    Optional[dict]
    error:                    Optional[str]
    rows_affected:            int               # rows modified by DML
    # API-level metadata
    session_id:               str
    timestamp:                str
    user_id:                  str
    role:                     str


class SchemaTableInfo(BaseModel):
    """Schema info for a single table (returned by GET /v1/schema)."""
    columns:      list[dict]
    primary_keys: list[str]
    foreign_keys: list[dict]


class SchemaResponse(BaseModel):
    """Full schema returned by GET /v1/schema."""
    table_count: int
    tables: dict[str, SchemaTableInfo]


class HistoryItem(BaseModel):
    """One past query entry returned by GET /v1/history."""
    timestamp:   str
    session_id:  str
    question:    str
    sql:         str
    safe_sql:    str
    row_count:   int
    confidence:  dict
    error:       Optional[str]


class HistoryResponse(BaseModel):
    """Response from GET /v1/history."""
    session_id:   str
    total_queries: int
    queries:      list[dict]   # full QueryResponse dicts, newest first


class AuditResponse(BaseModel):
    """Response from GET /v1/audit."""
    total_records: int
    records:       list[dict]


# =========================================================
# ENDPOINTS
# =========================================================

@app.post(
    "/v1/query",
    response_model=QueryResponse,
    summary="Run a natural-language query",
    tags=["Query"],
)
async def post_query(request: QueryRequest, raw_request: Request) -> QueryResponse:
    """
    Accept a natural-language question, run it through the full
    Text-to-SQL pipeline, and return:

    - The generated SQL and guardrail-safe SQL
    - Execution results as a list of row dicts
    - Composite confidence score with per-signal breakdown
    - Back-translation alignment score and label
    - Sanity check anomalies (if any)
    - Guardrail warnings (if any)
    - A `clarification_request` if the question is ambiguous
    - User identity and role (from RBAC headers)

    If the query is blocked by guardrails, `guardrail_allowed` is
    `false` and `guardrail_warnings` lists the violations.

    Permission is checked before execution based on the user's role
    and the type of SQL generated.
    """
    # ── Resolve user context ────────────────────────
    ctx = _get_user_context(raw_request)

    logger.info(
        "POST /v1/query | user=%r | role=%s | session=%r | question=%r",
        ctx.user_id, ctx.role.value, request.session_id, request.question,
    )

    # ── Run pipeline ────────────────────────────────
    try:
        result = pl.run_query(
            request.question,
            confirmed=request.confirmed,
            role=ctx.role.value,
        )
    except Exception as exc:
        logger.exception("run_query raised an unexpected exception: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # ── Permission check (post-generation) ──────────
    # We check AFTER generation so we can log the SQL even if denied.
    permission_granted = True
    if result.sql:
        required = permission_for_sql(result.sql)
        if not has_permission(ctx.role, required):
            permission_granted = False
            # Log the denied attempt
            audit_logger.log(AuditRecord(
                user_id=ctx.user_id,
                role=ctx.role.value,
                question=request.question,
                generated_sql=result.sql,
                safe_sql=result.safe_sql,
                execution_time_ms=0.0,
                success=False,
                row_count=0,
                rows_affected=0,
                error=f"Permission denied: role '{ctx.role.value}' lacks '{required.value}'",
                risk_level=result.risk_level,
                permission_granted=False,
                ip_address=ctx.ip_address,
            ))
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Permission denied: your role '{ctx.role.value}' "
                    f"does not have '{required.value}' permission. "
                    f"This query requires at least "
                    f"{'Editor' if required == Permission.QUERY_WRITE else 'Admin'} "
                    f"role."
                ),
            )

    # ── Build response ──────────────────────────────
    ts  = datetime.now(timezone.utc).isoformat()
    raw = result.to_dict()
    raw["session_id"] = request.session_id
    raw["timestamp"]  = ts
    raw["user_id"]    = ctx.user_id
    raw["role"]       = ctx.role.value

    # Append to session history
    _history[request.session_id].append(raw)
    logger.info(
        "Session %r now has %d entries.",
        request.session_id, len(_history[request.session_id]),
    )

    # ── Write audit record ──────────────────────────
    audit_logger.log(AuditRecord(
        user_id=ctx.user_id,
        role=ctx.role.value,
        question=request.question,
        generated_sql=result.sql,
        safe_sql=result.safe_sql,
        execution_time_ms=result.execution_time_ms,
        success=(result.execution_error is None and result.error is None),
        row_count=result.row_count,
        rows_affected=result.rows_affected,
        error=result.execution_error or result.error,
        risk_level=result.risk_level,
        permission_granted=True,
        ip_address=ctx.ip_address,
    ))

    return QueryResponse(**raw)


@app.get(
    "/v1/schema",
    response_model=SchemaResponse,
    summary="Get the database schema",
    tags=["Schema"],
)
async def get_schema(raw_request: Request) -> SchemaResponse:
    """
    Return the full database schema loaded from `outputs/schema.json`.

    Each table entry includes:
    - `columns`      – list of `{name, type}` dicts
    - `primary_keys` – list of column names that form the PK
    - `foreign_keys` – list of FK constraint dicts

    Requires VIEW_SCHEMA permission (all roles).
    """
    ctx = _get_user_context(raw_request)

    try:
        check_permission(ctx.role, Permission.VIEW_SCHEMA)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)

    logger.info("GET /v1/schema | user=%r | role=%s", ctx.user_id, ctx.role.value)

    tables = {}
    for table_name, info in pl.schema.items():
        tables[table_name] = SchemaTableInfo(
            columns=info.get("columns", []),
            primary_keys=info.get("primary_keys", []),
            foreign_keys=info.get("foreign_keys", []),
        )
    return SchemaResponse(table_count=len(tables), tables=tables)


@app.get(
    "/v1/history",
    response_model=HistoryResponse,
    summary="Get past queries for a session",
    tags=["History"],
)
async def get_history(
    raw_request: Request,
    session_id: str = Query(
        default="default",
        description="Session ID to retrieve history for.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of entries to return (newest first).",
    ),
) -> HistoryResponse:
    """
    Return past queries and their full results for the given session.

    Results are returned newest-first.  History is in-memory and is
    cleared when the server restarts.

    Requires VIEW_HISTORY permission (all roles).
    """
    ctx = _get_user_context(raw_request)

    try:
        check_permission(ctx.role, Permission.VIEW_HISTORY)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)

    logger.info(
        "GET /v1/history | user=%r | role=%s | session=%r | limit=%d",
        ctx.user_id, ctx.role.value, session_id, limit,
    )
    entries = _history.get(session_id, [])
    # Newest first, capped at limit
    ordered = list(reversed(entries))[:limit]
    return HistoryResponse(
        session_id=session_id,
        total_queries=len(entries),
        queries=ordered,
    )


@app.get(
    "/v1/audit",
    response_model=AuditResponse,
    summary="Get audit log entries (Admin only)",
    tags=["Audit"],
)
async def get_audit(
    raw_request: Request,
    user_id: Optional[str] = Query(
        default=None,
        description="Filter by user ID.",
    ),
    role: Optional[str] = Query(
        default=None,
        description="Filter by role (viewer / editor / admin).",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of records to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Offset for pagination.",
    ),
) -> AuditResponse:
    """
    Return audit log entries from the PostgreSQL ``audit_log`` table.

    Supports filtering by ``user_id`` and ``role``, with pagination
    via ``limit`` and ``offset``.

    **Admin only** — requires VIEW_AUDIT_LOG permission.
    """
    ctx = _get_user_context(raw_request)

    try:
        check_permission(ctx.role, Permission.VIEW_AUDIT_LOG)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)

    logger.info(
        "GET /v1/audit | admin=%r | filters=(user_id=%r, role=%r)",
        ctx.user_id, user_id, role,
    )

    records = audit_logger.query_logs(
        user_id=user_id,
        role=role,
        limit=limit,
        offset=offset,
    )
    total = audit_logger.count_logs(user_id=user_id, role=role)

    return AuditResponse(total_records=total, records=records)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health", tags=["Meta"])
async def health() -> dict:
    """Simple liveness probe."""
    return {"status": "ok", "schema_tables": len(pl.schema)}
