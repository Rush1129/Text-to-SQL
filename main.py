"""
app.py
======
FastAPI REST API for the Text-to-SQL pipeline.

Endpoints
---------
  POST  /v1/query    – Run a natural-language question through the pipeline
  GET   /v1/schema   – Return the database schema
  GET   /v1/history  – Return past queries for a session

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

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import pipeline as pl  # shared pipeline objects + run_query()

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
        "back-translation verification, sanity checking, and "
        "composite confidence scoring."
    ),
    version="1.0.0",
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
# SESSION HISTORY  (in-memory; cleared on restart)
# =========================================================

# Maps session_id -> list of result dicts (newest last)
_history: dict[str, list[dict]] = defaultdict(list)

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
    row_count:                int
    execution_time_ms:        float
    execution_error:          Optional[str]
    sql_valid:                bool
    validation_message:       str
    guardrail_allowed:        bool
    guardrail_warnings:       list[str]
    guardrail_limit_applied:  bool
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
    # API-level metadata
    session_id:               str
    timestamp:                str


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


# =========================================================
# ENDPOINTS
# =========================================================

@app.post(
    "/v1/query",
    response_model=QueryResponse,
    summary="Run a natural-language query",
    tags=["Query"],
)
async def post_query(request: QueryRequest) -> QueryResponse:
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

    If the query is blocked by guardrails, `guardrail_allowed` is
    `false` and `guardrail_warnings` lists the violations.
    """
    logger.info(
        "POST /v1/query | session=%r | question=%r",
        request.session_id, request.question,
    )

    try:
        result = pl.run_query(request.question)
    except Exception as exc:
        logger.exception("run_query raised an unexpected exception: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    ts  = datetime.now(timezone.utc).isoformat()
    raw = result.to_dict()
    raw["session_id"] = request.session_id
    raw["timestamp"]  = ts

    # Append to session history
    _history[request.session_id].append(raw)
    logger.info(
        "Session %r now has %d entries.",
        request.session_id, len(_history[request.session_id]),
    )

    return QueryResponse(**raw)


@app.get(
    "/v1/schema",
    response_model=SchemaResponse,
    summary="Get the database schema",
    tags=["Schema"],
)
async def get_schema() -> SchemaResponse:
    """
    Return the full database schema loaded from `outputs/schema.json`.

    Each table entry includes:
    - `columns`      – list of `{name, type}` dicts
    - `primary_keys` – list of column names that form the PK
    - `foreign_keys` – list of FK constraint dicts
    """
    logger.info("GET /v1/schema")
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
    """
    logger.info("GET /v1/history | session=%r | limit=%d", session_id, limit)
    entries = _history.get(session_id, [])
    # Newest first, capped at limit
    ordered = list(reversed(entries))[:limit]
    return HistoryResponse(
        session_id=session_id,
        total_queries=len(entries),
        queries=ordered,
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health", tags=["Meta"])
async def health() -> dict:
    """Simple liveness probe."""
    return {"status": "ok", "schema_tables": len(pl.schema)}
