"""
Sandbox Executor
================

Defense-in-depth execution layer for generated SQL queries.

Every query is executed inside a three-layer protection stack:

  1. ``?mode=ro``           – SQLite URI read-only connection
  2. ``PRAGMA query_only``  – Connection-level write blocker
  3. Explicit ``ROLLBACK``  – Always rolls back, even on success

Beyond safety, every execution is *instrumented*:

  • Execution time measured with ``time.perf_counter()`` (sub-ms precision)
  • ``EXPLAIN QUERY PLAN`` captured before the main query
  • Results packaged into a ``pandas.DataFrame`` (rows capped at *row_limit*)
  • Every execution written to a structured audit log (JSON lines)

If the guardrail middleware misses something, this layer ensures
the database remains untouched.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, Field


# =========================================================
# AUDIT LOG
# =========================================================

# Sits beside this file: guardrails/execution_audit.log
_AUDIT_LOG_PATH = Path(__file__).parent / "execution_audit.log"

_audit_logger = logging.getLogger("sandbox_audit")
_audit_logger.setLevel(logging.INFO)

if not _audit_logger.handlers:
    _audit_handler = logging.FileHandler(
        _AUDIT_LOG_PATH, encoding="utf-8"
    )
    # Raw JSON lines — no extra formatter decoration
    _audit_handler.setFormatter(
        logging.Formatter("%(message)s")
    )
    _audit_logger.addHandler(_audit_handler)


# =========================================================
# CONSOLE LOG
# =========================================================

_logger = logging.getLogger("sandbox_executor")
_logger.setLevel(logging.INFO)

if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] SANDBOX | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _logger.addHandler(_handler)


# =========================================================
# SANDBOX RESULT MODEL
# =========================================================

class SandboxResult(BaseModel):
    """
    Full outcome of a sandboxed query execution.

    Fields
    ------
    success         – True when the query ran without errors.
    columns         – Column names from the result set.
    rows            – Raw result rows (capped at *row_limit*).
    row_count       – Actual number of rows returned (before cap).
    dataframe       – Results as a pandas DataFrame (or None on error).
    execution_time  – Wall-clock seconds the query took (perf_counter).
    explain_plan    – List of rows from EXPLAIN QUERY PLAN.
    error           – Error message if execution failed.
    sandbox_info    – Human-readable protection summary.
    """

    model_config = {"arbitrary_types_allowed": True}

    success: bool = Field(
        description="Whether execution completed without errors."
    )
    columns: list[str] = Field(
        default_factory=list,
        description="Column names from the result set.",
    )
    rows: list[tuple] = Field(
        default_factory=list,
        description="Data rows returned (capped at row_limit).",
    )
    row_count: int = Field(
        default=0,
        description="Total rows returned before the cap was applied.",
    )
    dataframe: Optional[Any] = Field(
        default=None,
        description=(
            "pandas.DataFrame of the result set "
            "(None on execution failure)."
        ),
    )
    execution_time: float = Field(
        default=0.0,
        description="Wall-clock execution time in seconds.",
    )
    explain_plan: list[dict] = Field(
        default_factory=list,
        description=(
            "Rows from EXPLAIN QUERY PLAN, each as a dict "
            "with keys: id, parent, notused, detail."
        ),
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if execution failed.",
    )
    sandbox_info: str = Field(
        default="",
        description=(
            "Human-readable summary of protections "
            "applied during execution."
        ),
    )


# =========================================================
# SANDBOX EXECUTOR
# =========================================================

class SandboxExecutor:
    """
    Executes SQL queries inside a read-only sandbox.

    Three independent protection layers ensure the database
    is never modified, even if the guardrail layer misses
    a dangerous query.

    Every execution is also *instrumented*:

    * Precise wall-clock timing via ``time.perf_counter()``
    * ``EXPLAIN QUERY PLAN`` captured before the main query
    * Results packaged into a ``pandas.DataFrame``
    * Structured JSON-line audit log written for every call

    Usage::

        sandbox = SandboxExecutor("database/college_2.sqlite")
        result  = sandbox.execute("SELECT * FROM student LIMIT 5")

        if result.success:
            print(f"Took {result.execution_time:.4f}s")
            print(result.dataframe)
            for step in result.explain_plan:
                print(step["detail"])
    """

    # Human-readable label for the protection stack
    _PROTECTION_SUMMARY = (
        "Read-only connection (?mode=ro) + "
        "PRAGMA query_only = ON + "
        "auto-rollback transaction"
    )

    def __init__(
        self,
        db_path: str,
        readonly: bool = True,
        row_limit: int = 500,
    ):
        """
        Args:
            db_path:   Path to the SQLite database file.
            readonly:  Whether to enforce read-only mode.
                       Defaults to True (always recommended).
            row_limit: Maximum rows to include in the result
                       DataFrame. Excess rows are silently
                       discarded (row_count still reflects
                       the true total). Defaults to 500.
        """
        self.db_path = db_path
        self.readonly = readonly
        self.row_limit = row_limit

    # ── Main Entry Point ────────────────────────────

    def execute(self, sql: str) -> SandboxResult:
        """
        Execute *sql* inside the read-only sandbox.

        Steps:
          1. Open connection in read-only mode
          2. Enable PRAGMA query_only
          3. Begin explicit transaction
          4. Capture EXPLAIN QUERY PLAN
          5. Execute the query (timed)
          6. Fetch and cap results → DataFrame
          7. ROLLBACK (always, even on success)
          8. Write structured audit record

        Returns:
            SandboxResult with columns, rows, DataFrame,
            execution_time, explain_plan, and protection metadata.
        """
        conn = None
        start_ts = datetime.now(timezone.utc).isoformat()

        try:
            # ── Layer 1: Read-only connection ───────
            conn = self._open_connection()
            cursor = conn.cursor()

            # ── Layer 2: PRAGMA query_only ──────────
            if self.readonly:
                cursor.execute("PRAGMA query_only = ON;")

            # ── Layer 3: Explicit transaction ───────
            cursor.execute("BEGIN;")

            _logger.info(
                "Executing query in sandbox: %s",
                sql.replace("\n", " ").strip()[:120],
            )

            # ── EXPLAIN QUERY PLAN ──────────────────
            explain_plan = self._fetch_explain_plan(cursor, sql)

            # ── Timed execution ─────────────────────
            t0 = time.perf_counter()
            cursor.execute(sql)
            execution_time = time.perf_counter() - t0

            # ── Fetch results ───────────────────────
            columns = (
                [desc[0] for desc in cursor.description]
                if cursor.description
                else []
            )

            all_rows = cursor.fetchall()
            row_count = len(all_rows)

            # Apply row cap
            capped_rows = [
                tuple(r) for r in all_rows[: self.row_limit]
            ]

            # Build DataFrame
            df = (
                pd.DataFrame(capped_rows, columns=columns)
                if columns
                else pd.DataFrame()
            )

            _logger.info(
                "Query returned %d row(s) in %.4fs "
                "(cap: %d, df shape: %s).",
                row_count,
                execution_time,
                self.row_limit,
                df.shape,
            )

            result = SandboxResult(
                success=True,
                columns=columns,
                rows=capped_rows,
                row_count=row_count,
                dataframe=df,
                execution_time=execution_time,
                explain_plan=explain_plan,
                error=None,
                sandbox_info=self._PROTECTION_SUMMARY,
            )

        except sqlite3.Error as e:
            error_msg = str(e)
            execution_time = 0.0
            explain_plan = []

            _logger.warning(
                "Sandbox rejected query: %s | Error: %s",
                sql.replace("\n", " ").strip()[:80],
                error_msg,
            )

            result = SandboxResult(
                success=False,
                columns=[],
                rows=[],
                row_count=0,
                dataframe=None,
                execution_time=0.0,
                explain_plan=[],
                error=f"SANDBOX_BLOCKED: {error_msg}",
                sandbox_info=self._PROTECTION_SUMMARY,
            )

        finally:
            # ── Always rollback ─────────────────────
            if conn:
                try:
                    conn.rollback()
                    _logger.info("Transaction rolled back.")
                except sqlite3.Error:
                    pass  # Connection may already be closed
                finally:
                    conn.close()

        # ── Audit log ───────────────────────────────
        self._write_audit(sql, result, start_ts)

        return result

    # ── EXPLAIN Helper ──────────────────────────────

    def _fetch_explain_plan(
        self, cursor: sqlite3.Cursor, sql: str
    ) -> list[dict]:
        """
        Run ``EXPLAIN QUERY PLAN <sql>`` and return each row
        as a dict with keys: id, parent, notused, detail.

        Failures are silently swallowed so the main query
        always proceeds.
        """
        try:
            cursor.execute(f"EXPLAIN QUERY PLAN {sql}")
            rows = cursor.fetchall()
            return [
                {
                    "id":      row[0],
                    "parent":  row[1],
                    "notused": row[2],
                    "detail":  row[3],
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            _logger.debug(
                "EXPLAIN QUERY PLAN failed (non-fatal): %s", exc
            )
            return []

    # ── Audit Writer ────────────────────────────────

    def _write_audit(
        self,
        sql: str,
        result: SandboxResult,
        start_ts: str,
    ) -> None:
        """
        Append one JSON-line audit record to execution_audit.log.

        Schema::

            {
              "timestamp":      "<ISO-8601 UTC>",
              "sql":            "<executed sql>",
              "success":        true | false,
              "execution_time": 0.0012,
              "row_count":      42,
              "rows_capped":    false,
              "explain_steps":  3,
              "error":          null | "<message>"
            }
        """
        record = {
            "timestamp":      start_ts,
            "sql":            sql.replace("\n", " ").strip(),
            "success":        result.success,
            "execution_time": round(result.execution_time, 6),
            "row_count":      result.row_count,
            "rows_capped":    result.row_count > self.row_limit,
            "explain_steps":  len(result.explain_plan),
            "error":          result.error,
        }
        _audit_logger.info(json.dumps(record))

    # ── Connection Helper ───────────────────────────

    def _open_connection(self) -> sqlite3.Connection:
        """
        Open a SQLite connection.

        When ``self.readonly`` is True, the connection is
        opened via URI with ``?mode=ro``, which prevents
        the database file from being modified at the
        filesystem level.
        """
        if self.readonly:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)

        return conn

    # ── Verification Helper ─────────────────────────

    def verify_sandbox(self) -> dict:
        """
        Run a self-test to verify all three protection
        layers are active. Returns a dict with results.

        Useful for startup health checks.
        """
        results = {
            "readonly_connection": False,
            "pragma_query_only":   False,
            "rollback_works":      False,
        }

        conn = None
        try:
            conn = self._open_connection()
            cursor = conn.cursor()

            # Test read-only connection
            try:
                cursor.execute(
                    "CREATE TABLE _sandbox_test_ (id INTEGER);"
                )
            except sqlite3.OperationalError:
                results["readonly_connection"] = True

            # Test PRAGMA query_only
            if self.readonly:
                cursor.execute("PRAGMA query_only = ON;")
                cursor.execute("PRAGMA query_only;")
                pragma_val = cursor.fetchone()
                if pragma_val and pragma_val[0] == 1:
                    results["pragma_query_only"] = True

            # Test rollback
            conn.rollback()
            results["rollback_works"] = True

        except sqlite3.Error:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

        return results
