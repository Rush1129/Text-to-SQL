"""
SQL Guardrail Middleware
========================

Safety layer that validates every SQL query before execution.

Rules (each independently configurable):
  1. Classify DDL as RISKY — warn user, require confirmation
  2. Classify DML writes as MODERATE (or SAFE if ≤100 rows)
  3. Reject subqueries nested deeper than N levels (hard block)
  4. Enforce a row LIMIT if none is specified
  5. Block queries estimated to scan > N rows (EXPLAIN, hard block)

Risk Levels
-----------
  SAFE     – Read-only SELECT; or DML estimated to touch ≤100 rows
  MODERATE – DML writes (INSERT/UPDATE/DELETE/MERGE) with >100 row estimate
  RISKY    – DDL (CREATE/ALTER/DROP/TRUNCATE), or hard-blocked violations
"""

import logging
import os
import re
from enum import Enum

import psycopg2
import sqlparse
from datetime import datetime
from pydantic import BaseModel, Field

from .guardrail_config import GuardrailConfig


# =========================================================
# RISK LEVEL
# =========================================================

class RiskLevel(str, Enum):
    """Risk classification for a SQL query."""
    SAFE     = "safe"
    MODERATE = "moderate"
    RISKY    = "risky"


# =========================================================
# GUARDRAIL RESULT MODEL
# =========================================================

class GuardrailResult(BaseModel):
    """Outcome of the guardrail validation pipeline."""

    allowed: bool = Field(
        description="Whether the query is allowed to execute."
    )
    sql: str = Field(
        description=(
            "The (possibly modified) SQL query. "
            "May have a LIMIT clause appended."
        )
    )
    violations: list[str] = Field(
        default_factory=list,
        description="List of human-readable violation reasons.",
    )
    risk_level: RiskLevel = Field(
        default=RiskLevel.SAFE,
        description="Risk classification: safe, moderate, or risky.",
    )
    risk_warning: str = Field(
        default="",
        description="1–3 line human-readable warning about what this query may do.",
    )


# =========================================================
# SQL GUARDRAIL ENGINE
# =========================================================

class SQLGuardrail:
    """
    Validates SQL queries against a configurable set
    of safety rules.

    Usage::

        guardrail = SQLGuardrail()            # defaults
        result = guardrail.validate(sql, dsn="host=localhost ...")
        if not result.allowed:
            print(result.violations)
    """

    def __init__(self, config: GuardrailConfig | None = None):
        self.config = config or GuardrailConfig()
        self._setup_logger()

    # ── Logging Setup ───────────────────────────────

    def _setup_logger(self) -> None:
        """Configure a dedicated file logger for blocked queries."""

        self._logger = logging.getLogger("sql_guardrail")
        self._logger.setLevel(logging.WARNING)

        # Avoid duplicate handlers on re-instantiation
        if not self._logger.handlers:
            handler = logging.FileHandler(
                self.config.log_file,
                encoding="utf-8",
            )
            formatter = logging.Formatter(
                "[%(asctime)s] BLOCKED | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    def _log_blocked(self, rule: str, sql: str) -> None:
        """Log a blocked query with the rule that triggered it."""
        self._logger.warning(
            "Rule: %s | Query: %s",
            rule,
            sql.replace("\n", " ").strip(),
        )

    # ── Risk Classification ─────────────────────────

    # Warning templates
    _WARNINGS = {
        "ddl": (
            "⚠️ This query will permanently alter your database structure — "
            "it may drop tables, delete columns, or rename objects. "
            "This action is difficult or impossible to undo."
        ),
        "dml_moderate": (
            "⚠️ This query will modify data in your database — rows may be "
            "inserted, updated, or deleted permanently. "
            "Make sure you intend to change this data before proceeding."
        ),
        "dml_safe": (
            "ℹ️ This write query is estimated to affect at most 100 rows — "
            "it has been classified as low-risk. "
            "Review the SQL carefully before executing."
        ),
    }

    def _estimate_affected_rows(self, sql: str, dsn: str | None) -> int | None:
        """
        Use EXPLAIN to estimate how many rows a DML statement affects.
        Returns the row estimate, or None if unavailable.
        """
        if dsn is None:
            return None
        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(f"EXPLAIN {sql}")
            plan_rows = cursor.fetchall()
            conn.close()
            for (line,) in plan_rows:
                # First line of EXPLAIN output has the top-level row estimate
                rows_match = re.search(r"rows=(\d+)", line)
                if rows_match:
                    return int(rows_match.group(1))
        except Exception:
            pass
        return None

    def classify_risk(
        self,
        sql: str,
        dsn: str | None = None,
    ) -> tuple[RiskLevel, str]:
        """
        Classify a SQL query into SAFE / MODERATE / RISKY and return
        a (risk_level, risk_warning) tuple.

        Logic:
          - DDL keywords → RISKY
          - DML write keywords:
              • estimated rows ≤ 100 → SAFE (with informational note)
              • otherwise            → MODERATE
          - Everything else (SELECT, WITH, EXPLAIN) → SAFE
        """
        first_token = self._first_keyword(sql)

        # DDL is always RISKY
        if first_token in {kw.upper() for kw in self.config.ddl_keywords}:
            return RiskLevel.RISKY, self._WARNINGS["ddl"]

        # DML writes — check estimated row count
        if first_token in {kw.upper() for kw in self.config.dml_write_keywords}:
            estimated = self._estimate_affected_rows(sql, dsn)
            if estimated is not None and estimated <= 100:
                return RiskLevel.SAFE, self._WARNINGS["dml_safe"]
            return RiskLevel.MODERATE, self._WARNINGS["dml_moderate"]

        # Everything else (SELECT, WITH, EXPLAIN …)
        return RiskLevel.SAFE, ""

    # ── Rule 1: DDL Check (kept for hard-block cases) ──

    def check_ddl(self, sql: str) -> str | None:
        """
        No longer returns a hard block — DDL is now classified as RISKY
        and surfaced to the user for confirmation. Returns None always.
        Preserved for any callers that still reference this method.
        """
        return None

    # ── Rule 2: DML Write Check (kept for compatibility) ─

    def check_dml_writes(self, sql: str) -> str | None:
        """
        No longer returns a hard block — DML is classified as MODERATE
        or SAFE via classify_risk(). Returns None always.
        Preserved for any callers that still reference this method.
        """
        return None

    # ── Rule 3: Subquery Depth Check ────────────────

    def check_subquery_depth(self, sql: str) -> str | None:
        """
        Walk the sqlparse token tree and count nested
        SELECT statements inside parentheses.
        Reject if depth exceeds the configured limit.
        """
        if not self.config.block_deep_subqueries:
            return None

        parsed = sqlparse.parse(sql)
        if not parsed:
            return None

        max_depth = self._measure_subquery_depth(
            parsed[0].tokens, current_depth=0
        )

        if max_depth > self.config.max_subquery_depth:
            return (
                f"DEEP_SUBQUERY_BLOCKED: Subquery nesting "
                f"depth is {max_depth}, which exceeds the "
                f"maximum allowed depth of "
                f"{self.config.max_subquery_depth}."
            )

        return None

    def _measure_subquery_depth(
        self, tokens, current_depth: int
    ) -> int:
        """Recursively measure subquery nesting depth."""

        max_depth = current_depth

        for token in tokens:
            # Parenthesised group containing a SELECT
            if isinstance(
                token, sqlparse.sql.Parenthesis
            ):
                inner_sql = token.value.strip("()")

                if self._contains_select(inner_sql):
                    depth = self._measure_subquery_depth(
                        token.tokens,
                        current_depth + 1,
                    )
                    max_depth = max(max_depth, depth)
                else:
                    depth = self._measure_subquery_depth(
                        token.tokens,
                        current_depth,
                    )
                    max_depth = max(max_depth, depth)

            # Recurse into other grouping tokens
            elif hasattr(token, "tokens"):
                depth = self._measure_subquery_depth(
                    token.tokens,
                    current_depth,
                )
                max_depth = max(max_depth, depth)

        return max_depth

    @staticmethod
    def _contains_select(sql_fragment: str) -> bool:
        """Check whether a SQL fragment contains a SELECT."""
        parsed = sqlparse.parse(sql_fragment.strip())
        if parsed:
            stmt_type = parsed[0].get_type()
            if stmt_type and stmt_type.upper() == "SELECT":
                return True
        return False

    # ── Rule 4: Row Limit Enforcement ───────────────

    def enforce_row_limit(self, sql: str) -> str:
        """
        If the query is a SELECT without a LIMIT clause,
        append LIMIT {max_row_limit}.

        Returns the (possibly modified) SQL string.
        """
        if not self.config.enforce_row_limit:
            return sql

        first_token = self._first_keyword(sql)
        if first_token != "SELECT":
            return sql

        # Check if a LIMIT clause already exists
        if self._has_limit_clause(sql):
            return sql

        # Append LIMIT
        cleaned = sql.rstrip().rstrip(";")
        modified = (
            f"{cleaned}\nLIMIT {self.config.max_row_limit}"
        )

        return modified

    @staticmethod
    def _has_limit_clause(sql: str) -> bool:
        """
        Check whether the outermost query has a LIMIT
        clause, using sqlparse token inspection.
        """
        parsed = sqlparse.parse(sql)
        if not parsed:
            return False

        # Walk top-level tokens looking for LIMIT keyword
        for token in parsed[0].tokens:
            if (
                token.ttype is sqlparse.tokens.Keyword
                and token.normalized == "LIMIT"
            ):
                return True

        return False

    # ── Rule 5: EXPLAIN Scan Check ──────────────────

    def check_explain_scan(
        self,
        sql: str,
        dsn: str | None = None,
    ) -> str | None:
        """
        Run EXPLAIN on the PostgreSQL database and inspect
        the plan for sequential scans (Seq Scan).
        Block if the estimated scan rows exceed the threshold.

        PostgreSQL EXPLAIN returns a single text column per row
        (the query plan as formatted text).
        """
        if not self.config.block_expensive_scans:
            return None

        if dsn is None:
            return None

        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            cursor = conn.cursor()

            cursor.execute(f"EXPLAIN {sql}")
            plan_rows = cursor.fetchall()

            # Each row is (plan_line_text,)
            # Look for "Seq Scan on <table>" lines and extract
            # the estimated row count from "rows=N"
            seq_scan_tables: list[str] = []
            total_estimated_rows = 0

            for (line,) in plan_rows:
                line_upper = line.upper()
                if "SEQ SCAN" in line_upper:
                    # Extract table name: "Seq Scan on tablename"
                    table_match = re.search(
                        r"Seq Scan on (\w+)",
                        line,
                        re.IGNORECASE,
                    )
                    table_name = (
                        table_match.group(1)
                        if table_match
                        else "unknown"
                    )
                    seq_scan_tables.append(table_name)

                    # Extract estimated rows: "rows=N"
                    rows_match = re.search(
                        r"rows=(\d+)", line
                    )
                    if rows_match:
                        total_estimated_rows += int(
                            rows_match.group(1)
                        )

            conn.close()

            if not seq_scan_tables:
                return None

            if total_estimated_rows > self.config.max_scan_rows:
                return (
                    f"EXPENSIVE_SCAN_BLOCKED: Query would "
                    f"seq-scan ~{total_estimated_rows:,} rows "
                    f"across table(s) "
                    f"{', '.join(seq_scan_tables)}, "
                    f"exceeding the limit of "
                    f"{self.config.max_scan_rows:,}."
                )

        except psycopg2.Error as e:
            # If EXPLAIN itself fails, log but allow
            self._logger.warning(
                "Rule: EXPLAIN_ERROR | Error: %s | Query: %s",
                str(e),
                sql.replace("\n", " ").strip(),
            )
            return None

        return None

    # ── Main Validation Pipeline ────────────────────

    def validate(
        self,
        sql: str,
        dsn: str | None = None,
    ) -> GuardrailResult:
        """
        Run all guardrail checks in order.

        DDL and DML are no longer hard-blocked — they are classified into
        risk levels (SAFE / MODERATE / RISKY) and surfaced to the caller
        so the user can confirm before execution.

        Hard blocks remain for:
          - Subqueries nested deeper than max_subquery_depth
          - Queries estimated to seq-scan > max_scan_rows rows

        Args:
            sql: The SQL query to validate.
            dsn: Optional psycopg2 DSN connection string
                 (needed for EXPLAIN-based checks).

        Returns:
            GuardrailResult with allowed status, the (possibly modified)
            SQL, violation messages, risk_level, and risk_warning.
        """
        violations: list[str] = []
        current_sql = sql.strip()

        # ── 1. Risk Classification ───────────────────
        # Classify DDL/DML — does NOT block; caller decides what to do.
        risk_level, risk_warning = self.classify_risk(current_sql, dsn)

        # ── 2. Subquery Depth Check (hard block) ─────
        depth_violation = self.check_subquery_depth(current_sql)
        if depth_violation:
            violations.append(depth_violation)
            self._log_blocked("DEEP_SUBQUERY", current_sql)
            return GuardrailResult(
                allowed=False,
                sql=current_sql,
                violations=violations,
                risk_level=RiskLevel.RISKY,
                risk_warning=(
                    "🚫 This query contains deeply nested subqueries that "
                    "exceed the allowed depth limit and has been blocked "
                    "for safety."
                ),
            )

        # ── 3. Row Limit Enforcement ─────────────────
        # Only apply LIMIT to SELECT queries (not DML/DDL)
        first_token = self._first_keyword(current_sql)
        if first_token == "SELECT":
            current_sql = self.enforce_row_limit(current_sql)

        # ── 4. EXPLAIN Scan Check (hard block) ───────
        scan_violation = self.check_explain_scan(current_sql, dsn)
        if scan_violation:
            violations.append(scan_violation)
            self._log_blocked("EXPENSIVE_SCAN", current_sql)
            return GuardrailResult(
                allowed=False,
                sql=current_sql,
                violations=violations,
                risk_level=RiskLevel.RISKY,
                risk_warning=(
                    "🚫 This query would perform a massive sequential scan "
                    "across hundreds of thousands of rows and has been blocked "
                    "to protect database performance."
                ),
            )

        # ── All structural checks passed ─────────────
        return GuardrailResult(
            allowed=True,
            sql=current_sql,
            violations=violations,
            risk_level=risk_level,
            risk_warning=risk_warning,
        )

    # ── Helpers ─────────────────────────────────────

    @staticmethod
    def _first_keyword(sql: str) -> str:
        """
        Extract the first meaningful keyword from SQL
        using sqlparse tokenization.
        """
        parsed = sqlparse.parse(sql.strip())
        if not parsed:
            return ""

        for token in parsed[0].tokens:
            if token.ttype in (
                sqlparse.tokens.Keyword.DDL,
                sqlparse.tokens.Keyword.DML,
                sqlparse.tokens.Keyword,
            ):
                return token.normalized.upper()

            # Skip whitespace and comments
            if (
                token.ttype in (
                    sqlparse.tokens.Whitespace,
                    sqlparse.tokens.Newline,
                    sqlparse.tokens.Comment.Single,
                    sqlparse.tokens.Comment.Multiline,
                )
            ):
                continue

            # If we hit a non-keyword token first, stop
            break

        return ""
