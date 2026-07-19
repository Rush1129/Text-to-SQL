"""
main.py
======
FastAPI REST API for the Text-to-SQL pipeline.

Endpoints
---------
  Auth (public):
    POST  /v1/auth/signup     – Create a new user account
    POST  /v1/auth/login      – Log in with email + password → JWT

  Database Connections (JWT required):
    POST   /v1/connections       – Connect a PostgreSQL database
    GET    /v1/connections       – List user's saved connections
    DELETE /v1/connections/{id}  – Remove a connection

  Pipeline (JWT required):
    POST  /v1/query    – Run a natural-language question through the pipeline
    GET   /v1/schema   – Return the database schema
    GET   /v1/history  – Return past queries for a session
    GET   /v1/audit    – Return audit log entries (Admin only)

Authentication
--------------
  JWT-based. Send ``Authorization: Bearer <token>`` on all
  protected endpoints.

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
from auth import (
    create_access_token,
    decrypt_value,
    get_current_user,
    hash_password,
    verify_password,
)
from db_models import (
    create_db_connection,
    create_user,
    delete_connection,
    ensure_tables,
    get_connection,
    get_connection_by_db,
    get_user_by_email,
    list_user_connections,
    update_connection_password,
    update_connection_schema,
)
from rbac import (
    Permission,
    PermissionDeniedError,
    Role,
    UserContext,
    check_permission,
    has_permission,
    permission_for_sql,
)
from schema.extractor import (
    build_embeddings,
    extract_and_embed,
    get_collection_name,
    verify_pg_connection,
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
        "Natural-language to SQL query engine with JWT authentication, "
        "multi-database support, guardrails, back-translation verification, "
        "sanity checking, composite confidence scoring, and RBAC."
    ),
    version="3.0.0",
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
# STARTUP
# =========================================================

@app.on_event("startup")
async def _startup():
    ensure_tables()          # users + db_connections
    ensure_audit_table()     # audit_log
    logger.info("All tables ensured on startup.")

# =========================================================
# AUDIT LOGGER INSTANCE
# =========================================================

audit_logger = AuditLogger()

# =========================================================
# SESSION HISTORY  (in-memory; cleared on restart)
# =========================================================

_history: dict[str, list[dict]] = defaultdict(list)


# =========================================================
# REQUEST / RESPONSE MODELS
# =========================================================

# ── Auth ────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: str = Field(..., min_length=3, description="Email address.")
    password: str = Field(..., min_length=6, description="Password (min 6 chars).")

class LoginRequest(BaseModel):
    email: str = Field(..., description="Email address.")
    password: str = Field(..., description="Password.")

class AuthResponse(BaseModel):
    token: str
    user_id: str
    email: str
    role: str
    message: str = ""


# ── Connections ─────────────────────────────────────────

class ConnectRequest(BaseModel):
    connection_name: str = Field(
        ..., min_length=1,
        description="A friendly name for this connection (e.g. 'My College DB').",
    )
    host: str = Field(..., description="PostgreSQL host.")
    port: int = Field(default=5432, description="PostgreSQL port.")
    database_name: str = Field(..., description="Database name.")
    username: str = Field(..., description="Database username.")
    password: str = Field(..., description="Database password.")

class ConnectionResponse(BaseModel):
    id: str
    connection_name: str
    host: str
    port: int
    database_name: str
    username: str
    is_verified: bool
    table_count: int = 0
    created_at: str
    message: str = ""

class ConnectionListResponse(BaseModel):
    connections: list[dict]


# ── Query ───────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str = Field(
        ..., min_length=3,
        description="Natural-language question to convert to SQL.",
        examples=["How many students are enrolled in each department?"],
    )
    session_id: str = Field(
        default="default",
        description="Session identifier for GET /v1/history.",
    )
    confirmed: bool = Field(
        default=False,
        description="Set to true to acknowledge risk warnings and proceed.",
    )
    connection_id: Optional[str] = Field(
        default=None,
        description="UUID of the database connection to query. Defaults to the built-in database.",
    )


class QueryResponse(BaseModel):
    question:                 str
    sql:                      str
    safe_sql:                 str
    explanation:              str
    tables_accessed:          list[str]
    columns_accessed:         list[dict]
    execution_results:        list[dict]
    dataframe:                list[dict]
    row_count:                int
    execution_time_ms:        float
    execution_error:          Optional[str]
    sql_valid:                bool
    validation_message:       str
    guardrail_allowed:        bool
    guardrail_warnings:       list[str]
    guardrail_limit_applied:  bool
    risk_level:               str
    risk_warning:             str
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
    rows_affected:            int
    session_id:               str
    timestamp:                str
    user_id:                  str
    role:                     str


# ── Schema / History / Audit ────────────────────────────

class SchemaTableInfo(BaseModel):
    columns:      list[dict]
    primary_keys: list[str]
    foreign_keys: list[dict]

class SchemaResponse(BaseModel):
    table_count: int
    tables: dict[str, SchemaTableInfo]

class HistoryResponse(BaseModel):
    session_id:   str
    total_queries: int
    queries:      list[dict]

class AuditResponse(BaseModel):
    total_records: int
    records:       list[dict]


# =========================================================
# AUTH ENDPOINTS
# =========================================================

@app.post(
    "/v1/auth/signup",
    response_model=AuthResponse,
    summary="Create a new user account",
    tags=["Auth"],
)
async def signup(request: SignupRequest):
    """
    Create a new user with email + password.

    - Password is hashed with bcrypt before storage.
    - New users default to ``viewer`` role.
    - Returns a JWT token for immediate use.
    """
    try:
        user = create_user(
            email=request.email,
            password=request.password,
            role="viewer",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
    )

    logger.info("New user signed up: %s (role=%s)", user["email"], user["role"])

    return AuthResponse(
        token=token,
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
        message="Account created successfully.",
    )


@app.post(
    "/v1/auth/login",
    response_model=AuthResponse,
    summary="Log in with email and password",
    tags=["Auth"],
)
async def login(request: LoginRequest):
    """
    Authenticate with email + password.

    Returns a JWT token valid for 24 hours.
    """
    user = get_user_by_email(request.email)

    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
    )

    logger.info("User logged in: %s (role=%s)", user["email"], user["role"])

    return AuthResponse(
        token=token,
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
        message="Login successful.",
    )


# =========================================================
# DATABASE CONNECTION ENDPOINTS
# =========================================================

@app.post(
    "/v1/connections",
    response_model=ConnectionResponse,
    summary="Connect a PostgreSQL database",
    tags=["Connections"],
)
async def connect_database(request: ConnectRequest, raw_request: Request):
    """
    Connect a PostgreSQL database for querying.

    Steps:
    1. Verify the connection is reachable
    2. Check if this exact DB was connected before (reuse cached schema)
    3. If new: extract schema, generate LLM descriptions, build embeddings
    4. Encrypt and store credentials

    Schema extraction is **cached** — reconnecting to the same database
    skips re-extraction and uses the stored schema.
    """
    ctx = get_current_user(raw_request)

    # Build SQLAlchemy URL for verification
    pg_url = (
        f"postgresql+psycopg2://{request.username}:{request.password}"
        f"@{request.host}:{request.port}/{request.database_name}"
    )

    # 1. Verify connection
    success, msg = verify_pg_connection(pg_url)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Could not connect to database: {msg}",
        )

    # 2. Check if this exact DB connection exists (reuse schema)
    existing = get_connection_by_db(
        user_id=ctx.user_id,
        host=request.host,
        port=request.port,
        database_name=request.database_name,
        username=request.username,
    )

    if existing and existing.get("schema_json"):
        # Re-use existing connection — just update the password
        logger.info(
            "Reusing existing connection for %s@%s:%d/%s (schema cached, %d tables).",
            request.username, request.host, request.port,
            request.database_name, existing.get("table_count", 0),
        )
        update_connection_password(existing["id"], ctx.user_id, request.password)

        # Ensure embeddings exist
        coll_name = get_collection_name(existing["id"])
        try:
            pl.chroma_client.get_collection(name=coll_name)
        except Exception:
            # Rebuild embeddings from cached schema
            build_embeddings(existing["schema_json"], coll_name)

        return ConnectionResponse(
            id=existing["id"],
            connection_name=existing["connection_name"],
            host=existing["host"],
            port=existing["port"],
            database_name=existing["database_name"],
            username=existing["username"],
            is_verified=True,
            table_count=existing.get("table_count", 0),
            created_at=existing["created_at"],
            message=f"Reconnected (schema cached — {existing.get('table_count', 0)} tables).",
        )

    # 3. New connection — extract schema + build embeddings
    try:
        # Create the connection record first to get an ID
        conn_record = create_db_connection(
            user_id=ctx.user_id,
            connection_name=request.connection_name,
            host=request.host,
            port=request.port,
            database_name=request.database_name,
            username=request.username,
            password=request.password,
        )
        conn_id = conn_record["id"]

        # Extract schema and build embeddings
        coll_name = get_collection_name(conn_id)
        schema_data = extract_and_embed(pg_url, coll_name)

        # Cache the schema
        update_connection_schema(conn_id, schema_data)

        logger.info(
            "New connection created: %s (%d tables extracted).",
            request.connection_name, len(schema_data),
        )

        return ConnectionResponse(
            id=conn_id,
            connection_name=request.connection_name,
            host=request.host,
            port=request.port,
            database_name=request.database_name,
            username=request.username,
            is_verified=True,
            table_count=len(schema_data),
            created_at=conn_record["created_at"],
            message=f"Connected! {len(schema_data)} tables extracted and embedded.",
        )

    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Connection setup failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Schema extraction failed: {exc}",
        )


@app.get(
    "/v1/connections",
    response_model=ConnectionListResponse,
    summary="List saved database connections",
    tags=["Connections"],
)
async def list_connections(raw_request: Request):
    """Return all database connections for the authenticated user."""
    ctx = get_current_user(raw_request)
    connections = list_user_connections(ctx.user_id)

    # Remove schema_json from response (too large)
    for conn in connections:
        conn.pop("schema_json", None)

    return ConnectionListResponse(connections=connections)


@app.delete(
    "/v1/connections/{conn_id}",
    summary="Delete a database connection",
    tags=["Connections"],
)
async def remove_connection(conn_id: str, raw_request: Request):
    """Delete a saved database connection."""
    ctx = get_current_user(raw_request)
    deleted = delete_connection(conn_id, ctx.user_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found.")

    logger.info("Connection deleted: %s by user %s", conn_id, ctx.user_id)
    return {"message": "Connection deleted.", "id": conn_id}


# =========================================================
# QUERY ENDPOINT
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
    Text-to-SQL pipeline.

    Optionally specify ``connection_id`` to query a user-connected database
    instead of the default built-in database.
    """
    ctx = get_current_user(raw_request)

    logger.info(
        "POST /v1/query | user=%r | role=%s | conn=%s | question=%r",
        ctx.user_id, ctx.role.value,
        request.connection_id or "default", request.question,
    )

    # Run pipeline with optional connection
    try:
        result = pl.run_query(
            request.question,
            confirmed=request.confirmed,
            role=ctx.role.value,
            connection_id=request.connection_id,
            user_id=ctx.user_id,
        )
    except Exception as exc:
        logger.exception("run_query raised an unexpected exception: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # Permission check (post-generation)
    if result.sql:
        required = permission_for_sql(result.sql)
        if not has_permission(ctx.role, required):
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
                    f"does not have '{required.value}' permission."
                ),
            )

    # Build response
    ts  = datetime.now(timezone.utc).isoformat()
    raw = result.to_dict()
    raw["session_id"] = request.session_id
    raw["timestamp"]  = ts
    raw["user_id"]    = ctx.user_id
    raw["role"]       = ctx.role.value

    _history[request.session_id].append(raw)

    # Write audit record
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


# =========================================================
# SCHEMA ENDPOINT
# =========================================================

@app.get(
    "/v1/schema",
    response_model=SchemaResponse,
    summary="Get the database schema",
    tags=["Schema"],
)
async def get_schema(
    raw_request: Request,
    connection_id: Optional[str] = Query(
        default=None,
        description="Connection UUID. Omit for default database.",
    ),
) -> SchemaResponse:
    """Return the schema for the default or a user-connected database."""
    ctx = get_current_user(raw_request)

    try:
        check_permission(ctx.role, Permission.VIEW_SCHEMA)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)

    # Load schema from connection or default
    if connection_id:
        conn = get_connection(connection_id, ctx.user_id)
        if not conn or not conn.get("schema_json"):
            raise HTTPException(status_code=404, detail="Connection or schema not found.")
        target_schema = conn["schema_json"]
    else:
        target_schema = pl.schema

    tables = {}
    for table_name, info in target_schema.items():
        tables[table_name] = SchemaTableInfo(
            columns=info.get("columns", []),
            primary_keys=info.get("primary_keys", []),
            foreign_keys=info.get("foreign_keys", []),
        )
    return SchemaResponse(table_count=len(tables), tables=tables)


# =========================================================
# HISTORY ENDPOINT
# =========================================================

@app.get(
    "/v1/history",
    response_model=HistoryResponse,
    summary="Get past queries for a session",
    tags=["History"],
)
async def get_history(
    raw_request: Request,
    session_id: str = Query(default="default"),
    limit: int = Query(default=50, ge=1, le=500),
) -> HistoryResponse:
    """Return past queries for the given session (newest first)."""
    ctx = get_current_user(raw_request)

    try:
        check_permission(ctx.role, Permission.VIEW_HISTORY)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)

    entries = _history.get(session_id, [])
    ordered = list(reversed(entries))[:limit]
    return HistoryResponse(
        session_id=session_id,
        total_queries=len(entries),
        queries=ordered,
    )


# =========================================================
# AUDIT ENDPOINT
# =========================================================

@app.get(
    "/v1/audit",
    response_model=AuditResponse,
    summary="Get audit log entries (Admin only)",
    tags=["Audit"],
)
async def get_audit(
    raw_request: Request,
    user_id: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AuditResponse:
    """Return audit log entries (Admin only)."""
    ctx = get_current_user(raw_request)

    try:
        check_permission(ctx.role, Permission.VIEW_AUDIT_LOG)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)

    records = audit_logger.query_logs(
        user_id=user_id, role=role, limit=limit, offset=offset,
    )
    total = audit_logger.count_logs(user_id=user_id, role=role)
    return AuditResponse(total_records=total, records=records)


# =========================================================
# USER PROFILE ENDPOINT
# =========================================================

@app.get("/v1/auth/me", tags=["Auth"])
async def get_me(raw_request: Request):
    """Return the currently authenticated user's profile."""
    ctx = get_current_user(raw_request)
    from db_models import get_user_by_id
    user = get_user_by_id(ctx.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health", tags=["Meta"])
async def health() -> dict:
    return {"status": "ok", "schema_tables": len(pl.schema)}
