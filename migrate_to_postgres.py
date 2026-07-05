"""
SQLite → PostgreSQL Migration Script
=====================================

Migrates the college_2.sqlite database to PostgreSQL using
SQLAlchemy + pandas. No external tools needed.

What it does:
  1. Reads every table from SQLite
  2. Creates equivalent tables in PostgreSQL (if they don't exist)
  3. Copies all rows using pandas + to_sql()
  4. Preserves column names and basic types
  5. Prints a summary of rows migrated per table

Usage:
    .venv\\Scripts\\python.exe migrate_to_postgres.py

Requirements:
  - .env with PG_HOST, PG_PORT, PG_DB, PG_ADMIN_USER, PG_ADMIN_PASSWORD
  - psycopg2-binary installed (pip install psycopg2-binary)
  - SQLAlchemy installed
"""

import os
import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

# =========================================================
# CONFIGURATION
# =========================================================

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH  = os.path.join(PROJECT_ROOT, "database", "college_2.sqlite")

PG_HOST     = os.environ.get("PG_HOST",           "localhost")
PG_PORT     = os.environ.get("PG_PORT",           "5432")
PG_DB       = os.environ.get("PG_DB",             "college_2")
PG_USER     = os.environ.get("PG_ADMIN_USER",     "postgres")
PG_PASSWORD = os.environ.get("PG_ADMIN_PASSWORD", "")

SQLITE_URL = f"sqlite:///{SQLITE_PATH}"
PG_URL     = (
    f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}"
    f"@{PG_HOST}:{PG_PORT}/{PG_DB}"
)

# Chunk size for large tables — reduces memory usage
CHUNK_SIZE = 5000


# =========================================================
# MAIN MIGRATION
# =========================================================

def migrate():
    print("=" * 60)
    print("  SQLite → PostgreSQL Migration")
    print("=" * 60)
    print(f"\n📂 Source : {SQLITE_PATH}")
    print(f"📡 Target : {PG_HOST}:{PG_PORT}/{PG_DB}\n")

    # ── Connect ──────────────────────────────────────────
    print("Connecting to SQLite...")
    sqlite_engine = create_engine(SQLITE_URL)

    print("Connecting to PostgreSQL...")
    try:
        pg_engine = create_engine(PG_URL)
        with pg_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("✅ Connected to PostgreSQL.\n")
    except Exception as e:
        print(f"❌ Could not connect to PostgreSQL: {e}")
        print(
            "\nCheck your .env values:\n"
            "  PG_HOST, PG_PORT, PG_DB, "
            "PG_ADMIN_USER, PG_ADMIN_PASSWORD"
        )
        sys.exit(1)

    # ── Discover tables ───────────────────────────────────
    sqlite_inspector = inspect(sqlite_engine)
    tables = sqlite_inspector.get_table_names()

    print(f"📋 Found {len(tables)} table(s) in SQLite:")
    for t in tables:
        print(f"   • {t}")
    print()

    # ── Migrate each table ────────────────────────────────
    results = []

    for table in tables:
        print(f"⏳ Migrating: {table}")

        try:
            # Read all rows from SQLite in chunks
            total_rows = 0

            for i, chunk in enumerate(
                pd.read_sql_table(
                    table,
                    sqlite_engine,
                    chunksize=CHUNK_SIZE,
                )
            ):
                # First chunk: replace (creates/truncates table in PG)
                # Subsequent chunks: append
                if_exists_mode = "replace" if i == 0 else "append"

                chunk.to_sql(
                    table,
                    pg_engine,
                    if_exists=if_exists_mode,
                    index=False,       # Don't add a pandas index column
                    method="multi",    # Batch INSERT for speed
                )
                total_rows += len(chunk)

            results.append((table, total_rows, None))
            print(f"   ✅ {total_rows:,} rows migrated")

        except Exception as e:
            results.append((table, 0, str(e)))
            print(f"   ❌ Failed: {e}")

    # ── Summary ───────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  MIGRATION SUMMARY")
    print("=" * 60)

    ok      = [r for r in results if r[2] is None]
    failed  = [r for r in results if r[2] is not None]

    for table, rows, _ in ok:
        print(f"   ✅ {table:<35} {rows:>8,} rows")

    for table, _, err in failed:
        print(f"   ❌ {table:<35} ERROR: {err[:60]}")

    total_rows_migrated = sum(r[1] for r in ok)
    print(f"\n   Total: {len(ok)}/{len(results)} tables, "
          f"{total_rows_migrated:,} rows migrated")

    if failed:
        print(f"\n⚠️  {len(failed)} table(s) failed. "
              f"See errors above.")
        sys.exit(1)
    else:
        print("\n✅ Migration complete!")
        print(
            "\nNext step: Run the read-only role setup:\n"
            "  .venv\\Scripts\\python.exe "
            "guardrails/setup_readonly_user.py"
        )


if __name__ == "__main__":
    migrate()
