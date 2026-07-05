"""
PostgreSQL Read-Only User Setup Script
=======================================

Creates and verifies a restricted read-only role in PostgreSQL
for safe query execution by the Text-to-SQL system.

Unlike SQLite, PostgreSQL has a native user/role system.
We achieve SELECT-only access through:

  1. A dedicated ``readonly_user`` role with only CONNECT + SELECT
  2. The application connects exclusively as this role
  3. SET TRANSACTION READ ONLY at the session level (defense-in-depth)

Usage::

    python guardrails/setup_readonly_user.py

Requires the following environment variables (or .env file):
    PG_HOST, PG_PORT, PG_DB,
    PG_ADMIN_USER, PG_ADMIN_PASSWORD,
    PG_READONLY_USER, PG_READONLY_PASSWORD
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()


# =========================================================
# CONFIGURATION (from environment)
# =========================================================

PG_HOST             = os.environ.get("PG_HOST",             "localhost")
PG_PORT             = os.environ.get("PG_PORT",             "5432")
PG_DB               = os.environ.get("PG_DB",               "college_2")
PG_ADMIN_USER       = os.environ.get("PG_ADMIN_USER",       "postgres")
PG_ADMIN_PASSWORD   = os.environ.get("PG_ADMIN_PASSWORD",   "")
PG_READONLY_USER    = os.environ.get("PG_READONLY_USER",    "readonly_user")
PG_READONLY_PASSWORD = os.environ.get("PG_READONLY_PASSWORD", "")

ADMIN_DSN = (
    f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} "
    f"user={PG_ADMIN_USER} password={PG_ADMIN_PASSWORD}"
)

READONLY_DSN = (
    f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} "
    f"user={PG_READONLY_USER} password={PG_READONLY_PASSWORD}"
)


# =========================================================
# CREATE READ-ONLY ROLE
# =========================================================

def create_readonly_role() -> bool:
    """
    Connect as admin and create a read-only role with:
      - LOGIN privilege
      - CONNECT on the target database
      - USAGE on the public schema
      - SELECT on all existing and future tables

    Returns:
        True if setup completed successfully.
    """
    print("=" * 55)
    print("  POSTGRESQL READ-ONLY ROLE SETUP")
    print("=" * 55)

    print(f"\n📡 Connecting as admin ({PG_ADMIN_USER}) to '{PG_DB}'...")

    try:
        conn = psycopg2.connect(ADMIN_DSN)
        conn.autocommit = True
        cursor = conn.cursor()

        # ── 1. Create role if not exists ────────────
        print(f"\n🔧 Creating role '{PG_READONLY_USER}'...")
        cursor.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT FROM pg_catalog.pg_roles
                    WHERE rolname = '{PG_READONLY_USER}'
                ) THEN
                    CREATE ROLE {PG_READONLY_USER}
                        WITH LOGIN
                        PASSWORD '{PG_READONLY_PASSWORD}';
                    RAISE NOTICE 'Role created.';
                ELSE
                    ALTER ROLE {PG_READONLY_USER}
                        WITH LOGIN
                        PASSWORD '{PG_READONLY_PASSWORD}';
                    RAISE NOTICE 'Role already exists — password updated.';
                END IF;
            END
            $$;
        """)
        print(f"   ✅ Role '{PG_READONLY_USER}' ready.")

        # ── 2. Grant CONNECT on database ────────────
        print(f"\n🔧 Granting CONNECT on database '{PG_DB}'...")
        cursor.execute(
            f"GRANT CONNECT ON DATABASE {PG_DB} "
            f"TO {PG_READONLY_USER};"
        )
        print("   ✅ CONNECT granted.")

        # ── 3. Grant USAGE on public schema ─────────
        print("\n🔧 Granting USAGE on schema public...")
        cursor.execute(
            f"GRANT USAGE ON SCHEMA public TO {PG_READONLY_USER};"
        )
        print("   ✅ USAGE granted.")

        # ── 4. Grant SELECT on all existing tables ──
        print("\n🔧 Granting SELECT on all existing tables...")
        cursor.execute(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA public "
            f"TO {PG_READONLY_USER};"
        )
        print("   ✅ SELECT on existing tables granted.")

        # ── 5. Grant SELECT on future tables ────────
        print("\n🔧 Setting default privileges for future tables...")
        cursor.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT ON TABLES TO {PG_READONLY_USER};"
        )
        print("   ✅ Default privileges set.")

        conn.close()
        print("\n✅ Read-only role setup complete.")
        return True

    except psycopg2.Error as e:
        print(f"\n❌ Setup failed: {e}")
        return False


# =========================================================
# VERIFY PROTECTIONS
# =========================================================

def verify_protections() -> bool:
    """
    Verify that all protection layers are working correctly.

    Tests:
      1. Read-only role cannot CREATE tables
      2. Read-only role cannot INSERT rows
      3. Read-only role CAN run SELECT queries
      4. SET TRANSACTION READ ONLY is enforceable

    Returns:
        True if all verifications pass.
    """
    print("\n" + "-" * 55)
    print("  VERIFICATION")
    print("-" * 55)

    all_passed = True

    # ── Test 1: CREATE is blocked ───────────────────
    print(f"\n🔍 Test 1: CREATE TABLE blocked for '{PG_READONLY_USER}'")
    try:
        conn = psycopg2.connect(READONLY_DSN)
        conn.autocommit = False
        cursor = conn.cursor()

        try:
            cursor.execute(
                "CREATE TABLE _sandbox_test_ (id INTEGER);"
            )
            print("   ❌ FAIL — CREATE was NOT blocked!")
            all_passed = False
        except psycopg2.Error as e:
            print(f"   ✅ PASS — CREATE blocked: {e!s:.80}")
        finally:
            conn.rollback()
            conn.close()

    except psycopg2.Error as e:
        print(f"   ⚠️  Could not connect: {e}")
        all_passed = False

    # ── Test 2: INSERT is blocked ───────────────────
    print(f"\n🔍 Test 2: INSERT blocked for '{PG_READONLY_USER}'")
    try:
        conn = psycopg2.connect(READONLY_DSN)
        conn.autocommit = False
        cursor = conn.cursor()

        # Get first table name to attempt insert
        cursor.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname='public' LIMIT 1;"
        )
        row = cursor.fetchone()
        test_table = row[0] if row else "student"

        try:
            cursor.execute(
                f"INSERT INTO {test_table} VALUES (999);"
            )
            print("   ❌ FAIL — INSERT was NOT blocked!")
            all_passed = False
        except psycopg2.Error as e:
            print(f"   ✅ PASS — INSERT blocked: {e!s:.80}")
        finally:
            conn.rollback()
            conn.close()

    except psycopg2.Error as e:
        print(f"   ⚠️  Could not connect: {e}")

    # ── Test 3: SELECT still works ──────────────────
    print(f"\n🔍 Test 3: SELECT works for '{PG_READONLY_USER}'")
    try:
        conn = psycopg2.connect(READONLY_DSN)
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION READ ONLY;")

        cursor.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' LIMIT 5;"
        )
        tables = cursor.fetchall()

        if tables:
            print(f"   ✅ PASS — Found {len(tables)} table(s):")
            for t in tables:
                print(f"      • {t[0]}")
        else:
            print("   ⚠️  No tables found (schema may be empty).")

        conn.rollback()
        conn.close()

    except psycopg2.Error as e:
        print(f"   ❌ FAIL — SELECT blocked: {e}")
        all_passed = False

    # ── Test 4: SET TRANSACTION READ ONLY ──────────
    print("\n🔍 Test 4: SET TRANSACTION READ ONLY enforceable")
    try:
        conn = psycopg2.connect(READONLY_DSN)
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION READ ONLY;")

        try:
            cursor.execute(
                "CREATE TABLE _ro_test_ (id INTEGER);"
            )
            print("   ❌ FAIL — Write was NOT blocked!")
            all_passed = False
        except psycopg2.Error:
            print("   ✅ PASS — Write blocked by READ ONLY transaction.")
        finally:
            conn.rollback()
            conn.close()

    except psycopg2.Error as e:
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
            "guardrails/sql_guardrails.py",
        ),
        (
            "Layer 2: PostgreSQL Read-Only Role",
            f"'{PG_READONLY_USER}' role has only SELECT "
            "privilege — writes fail at the DB level.",
            "guardrails/setup_readonly_user.py",
        ),
        (
            "Layer 3: SET TRANSACTION READ ONLY",
            "Session-level setting that rejects any "
            "write within the transaction.",
            "guardrails/sandbox_executor.py",
        ),
        (
            "Layer 4: Auto-Rollback",
            "Every transaction is rolled back in a "
            "finally block, even on success.",
            "guardrails/sandbox_executor.py",
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
    print("\n🔧 Setting up PostgreSQL read-only role...\n")

    success = create_readonly_role()

    if success:
        verify_protections()
        print_summary()
    else:
        print("\n❌ Setup failed. See errors above.")
        sys.exit(1)

    print("\n✅ Setup complete!\n")
