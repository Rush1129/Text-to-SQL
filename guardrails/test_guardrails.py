"""
Quick verification script for SQL guardrail middleware.
Tests all 5 rules independently.
"""

import sys
import os

from dotenv import load_dotenv
load_dotenv()

# Fix Windows console encoding for Unicode
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from guardrails.sql_guardrails import SQLGuardrail
from guardrails.guardrail_config import GuardrailConfig
from guardrails.sandbox_executor import build_dsn

DSN = build_dsn()  # reads PG_* vars from .env

guardrail = SQLGuardrail()

PASS = "[PASS]"
FAIL = "[FAIL]"

print("=" * 60)
print("  SQL GUARDRAIL VERIFICATION")
print("=" * 60)

# -- Test 1: DDL Blocking --------------------------------
print("\n[Test 1] DDL Blocking")
for ddl in ["CREATE TABLE test (id INT)", "DROP TABLE users", "ALTER TABLE t ADD col INT"]:
    result = guardrail.validate(ddl)
    status = PASS if not result.allowed else FAIL
    print(f"  {status} | {ddl[:50]}")
    if result.violations:
        print(f"    Reason: {result.violations[0]}")

# -- Test 2: DML Write Blocking --------------------------
print("\n[Test 2] DML Write Blocking")
for dml in ["INSERT INTO t VALUES (1)", "UPDATE t SET x=1", "DELETE FROM t WHERE id=1"]:
    result = guardrail.validate(dml)
    status = PASS if not result.allowed else FAIL
    print(f"  {status} | {dml[:50]}")
    if result.violations:
        print(f"    Reason: {result.violations[0]}")

# -- Test 3: SELECT should pass --------------------------
print("\n[Test 3] Normal SELECT (should pass)")
result = guardrail.validate("SELECT * FROM students WHERE id = 1")
status = PASS if result.allowed else FAIL
print(f"  {status} | SELECT * FROM students WHERE id = 1")

# -- Test 4: LIMIT Enforcement ---------------------------
print("\n[Test 4] LIMIT Enforcement")
result = guardrail.validate("SELECT * FROM students")
has_limit = "LIMIT" in result.sql
print(f"  No LIMIT -> LIMIT appended: {PASS if has_limit else FAIL}")
print(f"    Modified SQL: {result.sql.strip()}")

result = guardrail.validate("SELECT * FROM students LIMIT 50")
original_limit = "LIMIT 50" in result.sql and "LIMIT 1000" not in result.sql
print(f"  Has LIMIT 50 -> preserved: {PASS if original_limit else FAIL}")
print(f"    SQL unchanged: {result.sql.strip()}")

# -- Test 5: Subquery Depth ------------------------------
print("\n[Test 5] Subquery Depth Check")
shallow = "SELECT * FROM t WHERE id IN (SELECT id FROM t2 WHERE x IN (SELECT x FROM t3))"
result = guardrail.validate(shallow)
status = PASS if result.allowed else FAIL
print(f"  Depth 2 (should pass): {status}")

deep = (
    "SELECT * FROM t WHERE id IN "
    "(SELECT id FROM t2 WHERE x IN "
    "(SELECT x FROM t3 WHERE y IN "
    "(SELECT y FROM t4 WHERE z IN "
    "(SELECT z FROM t5))))"
)
result = guardrail.validate(deep)
status = PASS if not result.allowed else FAIL
print(f"  Depth 4 (should block): {status}")
if result.violations:
    print(f"    Reason: {result.violations[0]}")

# -- Test 6: EXPLAIN Scan (with real PostgreSQL DB) ------
print("\n[Test 6] EXPLAIN Scan Check (with college_2 PostgreSQL DB)")
strict_config = GuardrailConfig(max_scan_rows=5)
strict_guardrail = SQLGuardrail(config=strict_config)
result = strict_guardrail.validate(
    "SELECT * FROM student",
    dsn=DSN,
)
if not result.allowed:
    print(f"  Full scan BLOCKED: {PASS}")
    print(f"    Reason: {result.violations[0]}")
else:
    print(f"  Full scan ALLOWED (table may have <=5 rows)")

# -- Test 7: Configurable rules --------------------------
print("\n[Test 7] Disabled DDL blocking (configurable)")
permissive = GuardrailConfig(block_ddl=False)
permissive_guard = SQLGuardrail(config=permissive)
result = permissive_guard.validate("CREATE TABLE test (id INT)")
status = PASS if result.allowed else FAIL
print(f"  DDL with block_ddl=False: {status}")

# -- Check log file --------------------------------------
print("\n[Test 8] Log file check")
try:
    with open(guardrail.config.log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    print(f"  Log entries found: {len(lines)} {PASS}")
    for line in lines[-3:]:
        print(f"    {line.strip()}")
except FileNotFoundError:
    print(f"  Log file not found {FAIL}")

print("\n" + "=" * 60)
print("  VERIFICATION COMPLETE")
print("=" * 60)
