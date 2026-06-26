"""
Guardrail Configuration
=======================

Pydantic model holding all SQL guardrail settings.
Each rule can be independently enabled/disabled, and
thresholds are fully configurable.
"""

import os
from pydantic import BaseModel, Field


class GuardrailConfig(BaseModel):
    """Configuration for the SQL guardrail middleware."""

    # ── DDL Blocking ────────────────────────────────
    block_ddl: bool = Field(
        default=True,
        description=(
            "Block all DDL statements "
            "(CREATE, ALTER, DROP, etc.)."
        ),
    )
    ddl_keywords: list[str] = Field(
        default=[
            "CREATE", "ALTER", "DROP",
            "TRUNCATE", "RENAME",
        ],
        description="DDL keywords to block.",
    )

    # ── DML Write Blocking ──────────────────────────
    block_dml_writes: bool = Field(
        default=True,
        description=(
            "Block all DML write statements "
            "(INSERT, UPDATE, DELETE, etc.)."
        ),
    )
    dml_write_keywords: list[str] = Field(
        default=[
            "INSERT", "UPDATE", "DELETE",
            "REPLACE", "MERGE",
        ],
        description="DML write keywords to block.",
    )

    # ── Row Limit Enforcement ───────────────────────
    enforce_row_limit: bool = Field(
        default=True,
        description=(
            "Append a LIMIT clause if the query "
            "does not already have one."
        ),
    )
    max_row_limit: int = Field(
        default=1000,
        description=(
            "Maximum number of rows to return. "
            "Applied only when no LIMIT exists."
        ),
        gt=0,
    )

    # ── Subquery Depth Limit ────────────────────────
    block_deep_subqueries: bool = Field(
        default=True,
        description=(
            "Reject queries with subqueries "
            "nested deeper than max_subquery_depth."
        ),
    )
    max_subquery_depth: int = Field(
        default=3,
        description="Maximum allowed subquery nesting depth.",
        gt=0,
    )

    # ── EXPLAIN-Based Scan Check ────────────────────
    block_expensive_scans: bool = Field(
        default=True,
        description=(
            "Block queries estimated to scan "
            "more than max_scan_rows rows."
        ),
    )
    max_scan_rows: int = Field(
        default=100_000,
        description=(
            "Maximum estimated rows a query may "
            "scan before being blocked."
        ),
        gt=0,
    )

    # ── Logging ─────────────────────────────────────
    log_file: str = Field(
        default=os.path.abspath(
            os.path.join(os.path.dirname(__file__), "guardrail_blocked.log")
        ),
        description="Path to the guardrail log file.",
    )
