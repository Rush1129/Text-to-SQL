"""
SQL Guardrail Middleware
========================

Safety layer that validates every SQL query before execution.

Rules (each independently configurable):
  1. Block DDL (CREATE, ALTER, DROP, …)
  2. Block DML writes (INSERT, UPDATE, DELETE, …)
  3. Reject subqueries nested deeper than N levels
  4. Enforce a row LIMIT if none is specified
  5. Block queries estimated to scan > N rows (EXPLAIN)
"""

import logging
import re
import sqlite3
from datetime import datetime

import sqlparse
from pydantic import BaseModel, Field

from guardrail_config import GuardrailConfig


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


# =========================================================
# SQL GUARDRAIL ENGINE
# =========================================================

class SQLGuardrail:
    """
    Validates SQL queries against a configurable set
    of safety rules.

    Usage::

        guardrail = SQLGuardrail()            # defaults
        result = guardrail.validate(sql, db_path="db.sqlite")
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

    # ── Rule 1: DDL Check ───────────────────────────

    def check_ddl(self, sql: str) -> str | None:
        """
        Return a violation string if the query
        starts with a DDL keyword, else None.
        """
        if not self.config.block_ddl:
            return None

        first_token = self._first_keyword(sql)

        if first_token in {
            kw.upper()
            for kw in self.config.ddl_keywords
        }:
            return (
                f"DDL_BLOCKED: Statement type "
                f"'{first_token}' is not allowed."
            )

        return None

    # ── Rule 2: DML Write Check ─────────────────────

    def check_dml_writes(self, sql: str) -> str | None:
        """
        Return a violation string if the query is a
        DML write operation, else None.
        """
        if not self.config.block_dml_writes:
            return None

        first_token = self._first_keyword(sql)

        if first_token in {
            kw.upper()
            for kw in self.config.dml_write_keywords
        }:
            return (
                f"DML_WRITE_BLOCKED: Statement type "
                f"'{first_token}' is not allowed."
            )

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
        db_path: str | None = None,
    ) -> str | None:
        """
        Run EXPLAIN QUERY PLAN on the SQLite database
        and inspect for full table scans. Block if
        estimated scan rows exceed the threshold.
        """
        if not self.config.block_expensive_scans:
            return None

        if db_path is None:
            return None

        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
            )
            cursor = conn.cursor()

            cursor.execute(
                f"EXPLAIN QUERY PLAN {sql}"
            )
            plan_rows = cursor.fetchall()

            # Check for SCAN (full table scan) entries
            # SQLite may output "SCAN TABLE x" or "SCAN x"
            scan_tables = []
            for row in plan_rows:
                detail = str(row[-1]) if row else ""
                detail_upper = detail.upper()
                if "SCAN" in detail_upper and "SEARCH" not in detail_upper:
                    # Match "SCAN TABLE name" or "SCAN name"
                    match = re.search(
                        r"SCAN(?:\s+TABLE)?\s+(\w+)",
                        detail,
                        re.IGNORECASE,
                    )
                    table_name = (
                        match.group(1) if match else "unknown"
                    )
                    scan_tables.append(table_name)

            if not scan_tables:
                conn.close()
                return None

            # Check row counts for scanned tables
            total_scan_rows = 0
            for table_name in scan_tables:
                try:
                    cursor.execute(
                        f"SELECT COUNT(*) FROM [{table_name}]"
                    )
                    count = cursor.fetchone()[0]
                    total_scan_rows += count
                except sqlite3.OperationalError:
                    pass  # Table might not exist

            conn.close()

            if total_scan_rows > self.config.max_scan_rows:
                return (
                    f"EXPENSIVE_SCAN_BLOCKED: Query would "
                    f"scan ~{total_scan_rows:,} rows across "
                    f"table(s) {', '.join(scan_tables)}, "
                    f"exceeding the limit of "
                    f"{self.config.max_scan_rows:,}."
                )

        except sqlite3.Error as e:
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
        db_path: str | None = None,
    ) -> GuardrailResult:
        """
        Run all guardrail checks in order.

        Args:
            sql: The SQL query to validate.
            db_path: Optional path to SQLite database
                     (needed for EXPLAIN check).

        Returns:
            GuardrailResult with allowed status,
            the (possibly modified) SQL, and any
            violation messages.
        """
        violations: list[str] = []
        current_sql = sql.strip()

        # ── 1. DDL Check ────────────────────────────
        ddl_violation = self.check_ddl(current_sql)
        if ddl_violation:
            violations.append(ddl_violation)
            self._log_blocked("DDL_DETECTED", current_sql)
            return GuardrailResult(
                allowed=False,
                sql=current_sql,
                violations=violations,
            )

        # ── 2. DML Write Check ──────────────────────
        dml_violation = self.check_dml_writes(current_sql)
        if dml_violation:
            violations.append(dml_violation)
            self._log_blocked("DML_WRITE_DETECTED", current_sql)
            return GuardrailResult(
                allowed=False,
                sql=current_sql,
                violations=violations,
            )

        # ── 3. Subquery Depth Check ─────────────────
        depth_violation = self.check_subquery_depth(
            current_sql
        )
        if depth_violation:
            violations.append(depth_violation)
            self._log_blocked(
                "DEEP_SUBQUERY", current_sql
            )
            return GuardrailResult(
                allowed=False,
                sql=current_sql,
                violations=violations,
            )

        # ── 4. Row Limit Enforcement ────────────────
        current_sql = self.enforce_row_limit(current_sql)

        # ── 5. EXPLAIN Scan Check ───────────────────
        scan_violation = self.check_explain_scan(
            current_sql, db_path
        )
        if scan_violation:
            violations.append(scan_violation)
            self._log_blocked(
                "EXPENSIVE_SCAN", current_sql
            )
            return GuardrailResult(
                allowed=False,
                sql=current_sql,
                violations=violations,
            )

        # ── All checks passed ───────────────────────
        return GuardrailResult(
            allowed=True,
            sql=current_sql,
            violations=[],
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
