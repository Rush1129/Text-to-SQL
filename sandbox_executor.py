"""
Sandbox Executor
================

Defense-in-depth execution layer for generated SQL queries.

Every query is executed inside a three-layer protection stack:

  1. ``?mode=ro``           – SQLite URI read-only connection
  2. ``PRAGMA query_only``  – Connection-level write blocker
  3. Explicit ``ROLLBACK``  – Always rolls back, even on success

If the guardrail middleware misses something, this layer ensures
the database remains untouched.
"""

import logging
import sqlite3

from pydantic import BaseModel, Field


# =========================================================
# LOGGING
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
    """Outcome of a sandboxed query execution."""

    success: bool = Field(
        description="Whether execution completed without errors."
    )
    columns: list[str] = Field(
        default_factory=list,
        description="Column names from the result set.",
    )
    rows: list[tuple] = Field(
        default_factory=list,
        description="Data rows returned by the query.",
    )
    row_count: int = Field(
        default=0,
        description="Number of rows returned.",
    )
    error: str | None = Field(
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

    Usage::

        sandbox = SandboxExecutor("database/college_2.sqlite")
        result = sandbox.execute("SELECT * FROM student LIMIT 5")
        if result.success:
            for row in result.rows:
                print(row)
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
    ):
        """
        Args:
            db_path: Path to the SQLite database file.
            readonly: Whether to enforce read-only mode.
                      Defaults to True (always recommended).
        """
        self.db_path = db_path
        self.readonly = readonly

    # ── Main Entry Point ────────────────────────────

    def execute(self, sql: str) -> SandboxResult:
        """
        Execute *sql* inside the read-only sandbox.

        Steps:
          1. Open connection in read-only mode
          2. Enable PRAGMA query_only
          3. Begin explicit transaction
          4. Execute the query
          5. Fetch results
          6. ROLLBACK (always, even on success)

        Returns:
            SandboxResult with columns, rows, and
            protection metadata.
        """
        conn = None

        try:
            # ── Layer 1: Read-only connection ───────
            conn = self._open_connection()

            cursor = conn.cursor()

            # ── Layer 2: PRAGMA query_only ──────────
            if self.readonly:
                cursor.execute("PRAGMA query_only = ON;")

            # ── Layer 3: Explicit transaction ───────
            # SQLite auto-begins, but we explicitly
            # control the boundary for clarity.
            cursor.execute("BEGIN;")

            _logger.info(
                "Executing query in sandbox: %s",
                sql.replace("\n", " ").strip()[:120],
            )

            # ── Execute the query ───────────────────
            cursor.execute(sql)

            # ── Fetch results ───────────────────────
            columns = (
                [desc[0] for desc in cursor.description]
                if cursor.description
                else []
            )

            rows = cursor.fetchall()

            _logger.info(
                "Query returned %d row(s).", len(rows)
            )

            return SandboxResult(
                success=True,
                columns=columns,
                rows=[tuple(row) for row in rows],
                row_count=len(rows),
                error=None,
                sandbox_info=self._PROTECTION_SUMMARY,
            )

        except sqlite3.Error as e:
            error_msg = str(e)

            _logger.warning(
                "Sandbox rejected query: %s | Error: %s",
                sql.replace("\n", " ").strip()[:80],
                error_msg,
            )

            return SandboxResult(
                success=False,
                columns=[],
                rows=[],
                row_count=0,
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
            "pragma_query_only": False,
            "rollback_works": False,
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
