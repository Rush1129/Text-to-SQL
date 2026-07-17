"""
PostgreSQL RBAC User Setup Script
==================================

Creates and verifies three role-based PostgreSQL users:

  • ``viewer_user``  – SELECT only (read-only)
  • ``editor_user``  – SELECT + INSERT + UPDATE + DELETE (DML)
  • ``admin_user``   – Full privileges on all tables + schema (DDL + DML)

Also creates the ``audit_log`` table and grants INSERT to all three
roles so every execution is auditable.

Usage::

    python guardrails/setup_rbac_users.py

Requires environment variables (or .env file):
    PG_HOST, PG_PORT, PG_DB,
    PG_ADMIN_USER, PG_ADMIN_PASSWORD,
    PG_VIEWER_USER, PG_VIEWER_PASSWORD,
    PG_EDITOR_USER, PG_EDITOR_PASSWORD,
    PG_ADMIN_DB_USER, PG_ADMIN_DB_PASSWORD
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
# CONFIGURATION
# =========================================================

PG_HOST          = os.environ.get("PG_HOST",          "localhost")
PG_PORT          = os.environ.get("PG_PORT",          "5432")
PG_DB            = os.environ.get("PG_DB",            "college_2")
PG_ADMIN_USER    = os.environ.get("PG_ADMIN_USER",    "postgres")
PG_ADMIN_PASSWORD = os.environ.get("PG_ADMIN_PASSWORD", "")

# RBAC users
PG_VIEWER_USER     = os.environ.get("PG_VIEWER_USER",     "viewer_user")
PG_VIEWER_PASSWORD = os.environ.get("PG_VIEWER_PASSWORD",  "viewer_pass")
PG_EDITOR_USER     = os.environ.get("PG_EDITOR_USER",     "editor_user")
PG_EDITOR_PASSWORD = os.environ.get("PG_EDITOR_PASSWORD",  "editor_pass")
PG_ADMIN_DB_USER     = os.environ.get("PG_ADMIN_DB_USER",     "admin_user")
PG_ADMIN_DB_PASSWORD = os.environ.get("PG_ADMIN_DB_PASSWORD",  "admin_pass")

ADMIN_DSN = (
    f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} "
    f"user={PG_ADMIN_USER} password={PG_ADMIN_PASSWORD}"
)

_W = 60  # Console width


# =========================================================
# ROLE DEFINITIONS
# =========================================================

ROLES = [
    {
        "name": PG_VIEWER_USER,
        "password": PG_VIEWER_PASSWORD,
        "label": "Viewer",
        "privileges": "SELECT only",
    },
    {
        "name": PG_EDITOR_USER,
        "password": PG_EDITOR_PASSWORD,
        "label": "Editor",
        "privileges": "SELECT + INSERT + UPDATE + DELETE",
    },
    {
        "name": PG_ADMIN_DB_USER,
        "password": PG_ADMIN_DB_PASSWORD,
        "label": "Admin",
        "privileges": "ALL PRIVILEGES",
    },
]


# =========================================================
# CREATE ROLES
# =========================================================

def create_roles() -> bool:
    """
    Create all three RBAC roles with appropriate privileges.
    Returns True on success.
    """
    print("=" * _W)
    print("  POSTGRESQL RBAC ROLE SETUP")
    print("=" * _W)
    print(f"\n📡 Connecting as superuser ({PG_ADMIN_USER}) to '{PG_DB}'...")

    try:
        conn = psycopg2.connect(ADMIN_DSN)
        conn.autocommit = True
        cursor = conn.cursor()

        for role_def in ROLES:
            name = role_def["name"]
            password = role_def["password"]
            label = role_def["label"]
            privs = role_def["privileges"]

            print(f"\n{'─' * _W}")
            print(f"  🔧 Setting up {label} role: '{name}'")
            print(f"     Privileges: {privs}")
            print(f"{'─' * _W}")

            # 1. Create role if not exists
            cursor.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_catalog.pg_roles
                        WHERE rolname = '{name}'
                    ) THEN
                        CREATE ROLE {name}
                            WITH LOGIN
                            PASSWORD '{password}';
                        RAISE NOTICE 'Role created.';
                    ELSE
                        ALTER ROLE {name}
                            WITH LOGIN
                            PASSWORD '{password}';
                        RAISE NOTICE 'Role already exists — password updated.';
                    END IF;
                END
                $$;
            """)
            print(f"   ✅ Role '{name}' ready.")

            # 2. Grant CONNECT
            cursor.execute(f"GRANT CONNECT ON DATABASE {PG_DB} TO {name};")
            print(f"   ✅ CONNECT granted.")

            # 3. Grant USAGE on public schema
            cursor.execute(f"GRANT USAGE ON SCHEMA public TO {name};")
            print(f"   ✅ USAGE on schema 'public' granted.")

            # 4. Grant role-specific privileges
            if label == "Viewer":
                # SELECT only
                cursor.execute(
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {name};"
                )
                cursor.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT SELECT ON TABLES TO {name};"
                )
                print(f"   ✅ SELECT on all tables granted.")

            elif label == "Editor":
                # SELECT + DML
                cursor.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {name};"
                )
                cursor.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON TABLES TO {name};"
                )
                # Grant USAGE on all sequences (needed for INSERT with SERIAL)
                cursor.execute(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES "
                    f"IN SCHEMA public TO {name};"
                )
                cursor.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {name};"
                )
                print(f"   ✅ SELECT + INSERT + UPDATE + DELETE granted.")

            elif label == "Admin":
                # Full privileges
                cursor.execute(
                    f"GRANT ALL PRIVILEGES ON ALL TABLES "
                    f"IN SCHEMA public TO {name};"
                )
                cursor.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT ALL PRIVILEGES ON TABLES TO {name};"
                )
                cursor.execute(
                    f"GRANT ALL PRIVILEGES ON ALL SEQUENCES "
                    f"IN SCHEMA public TO {name};"
                )
                cursor.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT ALL PRIVILEGES ON SEQUENCES TO {name};"
                )
                cursor.execute(
                    f"GRANT CREATE ON SCHEMA public TO {name};"
                )
                print(f"   ✅ ALL PRIVILEGES granted (including DDL).")

        # ── Create audit_log table ────────────────────
        print(f"\n{'─' * _W}")
        print("  📝 Creating audit_log table...")
        print(f"{'─' * _W}")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id              UUID PRIMARY KEY,
                timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id         VARCHAR(255) NOT NULL,
                role            VARCHAR(20)  NOT NULL,
                question        TEXT,
                generated_sql   TEXT,
                safe_sql        TEXT,
                execution_time_ms DOUBLE PRECISION DEFAULT 0,
                success         BOOLEAN DEFAULT FALSE,
                row_count       INTEGER DEFAULT 0,
                rows_affected   INTEGER DEFAULT 0,
                error           TEXT,
                risk_level      VARCHAR(20) DEFAULT 'safe',
                permission_granted BOOLEAN DEFAULT TRUE,
                ip_address      VARCHAR(45) DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
                ON audit_log (timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_log_user_id
                ON audit_log (user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_log_role
                ON audit_log (role);
        """)
        print("   ✅ audit_log table ready.")

        # Grant INSERT + SELECT on audit_log to all roles
        for role_def in ROLES:
            name = role_def["name"]
            cursor.execute(
                f"GRANT SELECT, INSERT ON audit_log TO {name};"
            )
        print("   ✅ All roles granted INSERT + SELECT on audit_log.")

        conn.close()
        print(f"\n✅ All RBAC roles created successfully.")
        return True

    except psycopg2.Error as e:
        print(f"\n❌ Setup failed: {e}")
        return False


# =========================================================
# VERIFICATION
# =========================================================

def verify_roles() -> bool:
    """
    Verify that each role has the correct privileges by testing
    allowed and denied operations.
    """
    print(f"\n{'=' * _W}")
    print("  RBAC VERIFICATION")
    print(f"{'=' * _W}")

    all_passed = True

    for role_def in ROLES:
        name = role_def["name"]
        password = role_def["password"]
        label = role_def["label"]

        dsn = (
            f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} "
            f"user={name} password={password}"
        )

        print(f"\n{'─' * _W}")
        print(f"  Testing {label} role: '{name}'")
        print(f"{'─' * _W}")

        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = False
            cursor = conn.cursor()

            # Test SELECT (should work for all)
            try:
                cursor.execute(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' LIMIT 1;"
                )
                row = cursor.fetchone()
                test_table = row[0] if row else "student"
                print(f"   ✅ SELECT works (found table: {test_table})")
                conn.rollback()
            except psycopg2.Error as e:
                print(f"   ❌ SELECT failed: {e}")
                all_passed = False
                conn.rollback()

            # Test INSERT (should fail for Viewer)
            try:
                cursor.execute(
                    f"INSERT INTO {test_table} "
                    f"SELECT * FROM {test_table} LIMIT 0;"
                )
                conn.rollback()
                if label == "Viewer":
                    print(f"   ❌ INSERT should be blocked for {label}!")
                    all_passed = False
                else:
                    print(f"   ✅ INSERT allowed (correct for {label})")
            except psycopg2.Error:
                conn.rollback()
                if label == "Viewer":
                    print(f"   ✅ INSERT blocked (correct for {label})")
                else:
                    print(f"   ❌ INSERT should be allowed for {label}!")
                    all_passed = False

            # Test CREATE TABLE (should only work for Admin)
            try:
                cursor.execute(
                    "CREATE TABLE _rbac_test_table_ (id INTEGER);"
                )
                # Clean up
                cursor.execute("DROP TABLE _rbac_test_table_;")
                conn.commit()
                if label == "Admin":
                    print(f"   ✅ CREATE TABLE allowed (correct for {label})")
                else:
                    print(f"   ❌ CREATE TABLE should be blocked for {label}!")
                    all_passed = False
            except psycopg2.Error:
                conn.rollback()
                if label == "Admin":
                    print(f"   ❌ CREATE TABLE should be allowed for {label}!")
                    all_passed = False
                else:
                    print(f"   ✅ CREATE TABLE blocked (correct for {label})")

            conn.close()

        except psycopg2.Error as e:
            print(f"   ❌ Could not connect as '{name}': {e}")
            all_passed = False

    # Summary
    print(f"\n{'=' * _W}")
    if all_passed:
        print("  ✅ ALL RBAC VERIFICATIONS PASSED")
    else:
        print("  ⚠️  SOME VERIFICATIONS FAILED — review above")
    print(f"{'=' * _W}")

    return all_passed


# =========================================================
# PRINT SUMMARY
# =========================================================

def print_summary() -> None:
    """Print a summary of the three RBAC roles and their privileges."""
    print(f"\n{'=' * _W}")
    print("  RBAC ROLE SUMMARY")
    print(f"{'=' * _W}")

    for role_def in ROLES:
        print(f"\n  🛡️  {role_def['label']} ({role_def['name']})")
        print(f"     Privileges: {role_def['privileges']}")

    print(f"\n  📝 Audit table: audit_log")
    print(f"     All roles can INSERT + SELECT audit records.")
    print(f"\n{'=' * _W}")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    print("\n🔧 Setting up PostgreSQL RBAC roles...\n")

    success = create_roles()

    if success:
        verify_roles()
        print_summary()
    else:
        print("\n❌ Setup failed. See errors above.")
        sys.exit(1)

    print("\n✅ RBAC setup complete!\n")
