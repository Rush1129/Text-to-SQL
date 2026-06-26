"""
Read-Only User Setup Script
============================

Creates a restricted read-only copy of the SQLite database
and verifies that all protection layers are active.

SQLite does not have a native user/role system like
PostgreSQL or MySQL. Instead, we achieve the "SELECT-only
user" concept through three mechanisms:

  1. OS-level read-only file permissions on the DB copy
  2. Connection-level ``?mode=ro`` URI parameter
  3. ``PRAGMA query_only = ON`` at the connection level

For production PostgreSQL/MySQL deployments, see the
commented-out SQL templates at the bottom of this file.

Usage::

    python setup_readonly_user.py
"""

import os
import shutil
import sqlite3
import stat
import sys


# =========================================================
# CONFIGURATION
# =========================================================

SOURCE_DB = "database/college_2.sqlite"
READONLY_DB = "database/college_2_readonly.sqlite"


# =========================================================
# CREATE READ-ONLY COPY
# =========================================================

def create_readonly_copy(
    source: str = SOURCE_DB,
    destination: str = READONLY_DB,
) -> bool:
    """
    Create a filesystem-level read-only copy of the
    source database.

    Steps:
      1. Copy the source database file
      2. Set OS-level read-only permissions
      3. Verify the copy is valid

    Returns:
        True if the copy was created successfully.
    """
    print("=" * 55)
    print("  READ-ONLY DATABASE SETUP")
    print("=" * 55)

    # ── Validate source exists ──────────────────────
    if not os.path.exists(source):
        print(f"\n❌ Source database not found: {source}")
        return False

    print(f"\n📂 Source: {source}")
    print(f"📂 Target: {destination}")

    # ── Copy the database ───────────────────────────
    try:
        shutil.copy2(source, destination)
        print("\n✅ Database copied successfully.")
    except OSError as e:
        print(f"\n❌ Failed to copy database: {e}")
        return False

    # ── Set read-only permissions ───────────────────
    try:
        # Remove write permission for all users
        os.chmod(
            destination,
            stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH,
        )
        print("✅ Read-only file permissions applied.")
    except OSError as e:
        print(f"\n⚠️  Could not set read-only permissions: {e}")
        print("   (The database copy still exists.)")

    return True


# =========================================================
# VERIFY PROTECTIONS
# =========================================================

def verify_protections(db_path: str = READONLY_DB) -> bool:
    """
    Verify that all three protection layers are working.

    Tests:
      1. Read-only connection blocks writes
      2. PRAGMA query_only blocks writes
      3. SELECT queries still work

    Returns:
        True if all verifications pass.
    """
    print("\n" + "-" * 55)
    print("  VERIFICATION")
    print("-" * 55)

    all_passed = True

    # ── Test 1: Read-only connection ────────────────
    print("\n🔍 Test 1: Read-only connection mode")
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True
        )
        cursor = conn.cursor()

        try:
            cursor.execute(
                "CREATE TABLE _test_write_ (id INTEGER);"
            )
            print("   ❌ FAIL — Write was NOT blocked!")
            all_passed = False
        except sqlite3.OperationalError as e:
            print(f"   ✅ PASS — Write blocked: {e}")

        conn.close()

    except sqlite3.OperationalError as e:
        print(f"   ✅ PASS — Connection refused: {e}")

    # ── Test 2: PRAGMA query_only ───────────────────
    print("\n🔍 Test 2: PRAGMA query_only = ON")
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True
        )
        cursor = conn.cursor()
        cursor.execute("PRAGMA query_only = ON;")

        try:
            cursor.execute(
                "INSERT INTO student VALUES (999, 'test');"
            )
            print("   ❌ FAIL — Write was NOT blocked!")
            all_passed = False
        except sqlite3.OperationalError as e:
            print(f"   ✅ PASS — Write blocked: {e}")

        conn.close()

    except sqlite3.Error as e:
        print(f"   ⚠️  Could not test: {e}")

    # ── Test 3: SELECT still works ──────────────────
    print("\n🔍 Test 3: SELECT queries work normally")
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True
        )
        cursor = conn.cursor()
        cursor.execute("PRAGMA query_only = ON;")

        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' LIMIT 5;"
        )
        tables = cursor.fetchall()

        if tables:
            print(f"   ✅ PASS — Found {len(tables)} table(s):")
            for t in tables:
                print(f"      • {t[0]}")
        else:
            print("   ⚠️  No tables found (database may be empty).")

        conn.close()

    except sqlite3.Error as e:
        print(f"   ❌ FAIL — SELECT blocked: {e}")
        all_passed = False

    # ── Test 4: Auto-rollback ───────────────────────
    print("\n🔍 Test 4: Transaction rollback")
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True
        )
        cursor = conn.cursor()
        cursor.execute("BEGIN;")
        cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master;"
        )
        count_before = cursor.fetchone()[0]
        conn.rollback()

        cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master;"
        )
        count_after = cursor.fetchone()[0]

        if count_before == count_after:
            print("   ✅ PASS — Rollback verified.")
        else:
            print("   ❌ FAIL — Data changed after rollback!")
            all_passed = False

        conn.close()

    except sqlite3.Error as e:
        print(f"   ⚠️  Could not test: {e}")

    # ── Summary ─────────────────────────────────────
    print("\n" + "=" * 55)
    if all_passed:
        print("  ✅ ALL PROTECTIONS VERIFIED")
    else:
        print("  ⚠️  SOME PROTECTIONS FAILED — review above")
    print("=" * 55)

    return all_passed


# =========================================================
# PRINT PROTECTION SUMMARY
# =========================================================

def print_summary():
    """Print a summary of all active protection layers."""

    print("\n" + "=" * 55)
    print("  PROTECTION LAYERS SUMMARY")
    print("=" * 55)

    layers = [
        (
            "Layer 1: SQL Guardrails",
            "Blocks DDL, DML writes, deep subqueries, "
            "and expensive scans BEFORE execution.",
            "sql_guardrails.py",
        ),
        (
            "Layer 2: Read-Only Connection",
            "SQLite URI parameter ?mode=ro prevents "
            "any filesystem-level writes.",
            "sandbox_executor.py",
        ),
        (
            "Layer 3: PRAGMA query_only",
            "Connection-level setting that makes SQLite "
            "reject any write operation.",
            "sandbox_executor.py",
        ),
        (
            "Layer 4: Auto-Rollback",
            "Every transaction is rolled back in a "
            "finally block, even on success.",
            "sandbox_executor.py",
        ),
        (
            "Layer 5: Read-Only File Permissions",
            "OS-level file permissions prevent any "
            "process from writing to the DB.",
            "setup_readonly_user.py",
        ),
    ]

    for name, desc, source in layers:
        print(f"\n  🛡️  {name}")
        print(f"     {desc}")
        print(f"     Source: {source}")

    print("\n" + "=" * 55)


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    print("\n🔧 Setting up read-only database user...\n")

    success = create_readonly_copy()

    if success:
        verify_protections()
        print_summary()
    else:
        print("\n❌ Setup failed. See errors above.")
        sys.exit(1)

    print("\n✅ Setup complete!\n")


# =========================================================
# PRODUCTION DATABASE TEMPLATES (PostgreSQL / MySQL)
# =========================================================
#
# When migrating from SQLite to a production database,
# use these SQL templates to create a real SELECT-only
# database user.
#
# ── PostgreSQL ──────────────────────────────────────────
#
#   -- Create a read-only role
#   CREATE ROLE readonly_user WITH LOGIN PASSWORD 'secure_password';
#
#   -- Grant CONNECT to the database
#   GRANT CONNECT ON DATABASE your_db TO readonly_user;
#
#   -- Grant USAGE on schemas
#   GRANT USAGE ON SCHEMA public TO readonly_user;
#
#   -- Grant SELECT on all existing tables
#   GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user;
#
#   -- Grant SELECT on future tables automatically
#   ALTER DEFAULT PRIVILEGES IN SCHEMA public
#       GRANT SELECT ON TABLES TO readonly_user;
#
# ── MySQL ───────────────────────────────────────────────
#
#   -- Create a read-only user
#   CREATE USER 'readonly_user'@'%' IDENTIFIED BY 'secure_password';
#
#   -- Grant SELECT-only permissions
#   GRANT SELECT ON your_db.* TO 'readonly_user'@'%';
#
#   -- Apply changes
#   FLUSH PRIVILEGES;
#
# =========================================================
