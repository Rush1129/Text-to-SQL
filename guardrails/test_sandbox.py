"""
Sandbox Executor Tests
======================

Verifies that the three-layer protection stack works:
  1. Read-only connection blocks writes
  2. PRAGMA query_only blocks writes
  3. Auto-rollback prevents persistence

Also tests that SELECT queries execute correctly.
"""

import sqlite3
import os
import sys

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from guardrails.sandbox_executor import SandboxExecutor


DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "database", "college_2.sqlite"))


def test_select_works():
    """Test: Normal SELECT queries execute successfully."""
    print("\n[TEST 1] SELECT query executes correctly")

    sandbox = SandboxExecutor(DB_PATH)
    result = sandbox.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' LIMIT 5;"
    )

    assert result.success, f"SELECT should succeed: {result.error}"
    assert result.row_count > 0, "Should return at least one row"
    assert len(result.columns) > 0, "Should have column names"
    assert result.sandbox_info, "Should have sandbox info"

    print(f"   PASS -- {result.row_count} row(s), "
          f"columns: {result.columns}")


def test_insert_blocked():
    """Test: INSERT is blocked by the sandbox."""
    print("\n[TEST 2] INSERT is blocked")

    sandbox = SandboxExecutor(DB_PATH)
    result = sandbox.execute(
        "INSERT INTO classroom VALUES ('Test', 999, 50);"
    )

    assert not result.success, "INSERT should be blocked"
    assert result.error is not None, "Should have error message"

    print(f"   PASS -- Blocked: {result.error}")


def test_drop_blocked():
    """Test: DROP TABLE is blocked by the sandbox."""
    print("\n[TEST 3] DROP TABLE is blocked")

    sandbox = SandboxExecutor(DB_PATH)
    result = sandbox.execute("DROP TABLE classroom;")

    assert not result.success, "DROP should be blocked"
    assert result.error is not None, "Should have error message"

    print(f"   PASS -- Blocked: {result.error}")


def test_update_blocked():
    """Test: UPDATE is blocked by the sandbox."""
    print("\n[TEST 4] UPDATE is blocked")

    sandbox = SandboxExecutor(DB_PATH)
    result = sandbox.execute(
        "UPDATE classroom SET capacity = 999 "
        "WHERE building = 'Packard';"
    )

    assert not result.success, "UPDATE should be blocked"
    assert result.error is not None, "Should have error message"

    print(f"   PASS -- Blocked: {result.error}")


def test_delete_blocked():
    """Test: DELETE is blocked by the sandbox."""
    print("\n[TEST 5] DELETE is blocked")

    sandbox = SandboxExecutor(DB_PATH)
    result = sandbox.execute(
        "DELETE FROM classroom WHERE building = 'Packard';"
    )

    assert not result.success, "DELETE should be blocked"
    assert result.error is not None, "Should have error message"

    print(f"   PASS -- Blocked: {result.error}")


def test_create_table_blocked():
    """Test: CREATE TABLE is blocked by the sandbox."""
    print("\n[TEST 6] CREATE TABLE is blocked")

    sandbox = SandboxExecutor(DB_PATH)
    result = sandbox.execute(
        "CREATE TABLE evil_table (id INTEGER);"
    )

    assert not result.success, "CREATE should be blocked"
    assert result.error is not None, "Should have error message"

    print(f"   PASS -- Blocked: {result.error}")


def test_database_unchanged():
    """Test: Database content unchanged after all tests."""
    print("\n[TEST 7] Database remains unchanged")

    # Get table count directly (bypassing sandbox)
    conn = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True
    )
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table';"
    )
    table_count = cursor.fetchone()[0]
    conn.close()

    # Verify no evil_table was created
    conn = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True
    )
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='evil_table';"
    )
    evil = cursor.fetchall()
    conn.close()

    assert len(evil) == 0, "evil_table should NOT exist"
    print(f"   PASS -- {table_count} tables, "
          f"no evil_table found")


def test_verify_sandbox():
    """Test: Sandbox self-verification works."""
    print("\n[TEST 8] Sandbox self-verification")

    sandbox = SandboxExecutor(DB_PATH)
    results = sandbox.verify_sandbox()

    assert results["readonly_connection"], (
        "Read-only connection should block writes"
    )
    assert results["pragma_query_only"], (
        "PRAGMA query_only should be active"
    )
    assert results["rollback_works"], (
        "Rollback should work"
    )

    print(f"   PASS -- All layers verified: {results}")


# =========================================================
# RUN ALL TESTS
# =========================================================

if __name__ == "__main__":
    print("=" * 55)
    print("  SANDBOX EXECUTOR TESTS")
    print("=" * 55)

    tests = [
        test_select_works,
        test_insert_blocked,
        test_drop_blocked,
        test_update_blocked,
        test_delete_blocked,
        test_create_table_blocked,
        test_database_unchanged,
        test_verify_sandbox,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"   FAIL -- {e}")
            failed += 1
        except Exception as e:
            print(f"   ERROR -- {type(e).__name__}: {e}")
            failed += 1

    print("\n" + "=" * 55)
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print("=" * 55)

    if failed > 0:
        sys.exit(1)
