# Query Sandboxing — Design Notes

Short reference on **why we chose what** and **what it does**.

---

## The Problem

Our Text-to-SQL system generates SQL from natural language. Even with guardrails
(DDL/DML blocking, subquery depth limits, row limits), there's always a risk:

- **Prompt injection** — A crafted input tricks the LLM into generating `DROP TABLE`
- **Parser bypass** — Obfuscated SQL (e.g., `DR/**/OP TABLE`) slips past regex/sqlparse checks
- **New attack vectors** — Tomorrow's exploit that today's guardrails don't know about

**One layer of defense is never enough.** We need defense-in-depth.

---

## Why Defense-in-Depth?

```
  User Question
       │
       ▼
  ┌─────────────┐
  │  Guardrails  │  ← Layer 1: Pattern-based blocking (sql_guardrails.py)
  │  (software)  │     Catches known bad patterns BEFORE execution
  └──────┬──────┘
         │ Query passed guardrails
         ▼
  ┌─────────────┐
  │   Sandbox    │  ← Layer 2: Execution-time protection (sandbox_executor.py)
  │  (runtime)   │     Even if guardrails miss it, the DB engine blocks it
  └──────┬──────┘
         │ Results only
         ▼
  ┌─────────────┐
  │  Auto-       │  ← Layer 3: Rollback safety net
  │  Rollback    │     Even if both above fail, nothing persists
  └─────────────┘
```

Each layer operates **independently**. If one fails, the others still protect the database.

---

## Layer-by-Layer Breakdown

### 1. Read-Only Connection (`?mode=ro`)

**What:** SQLite URI parameter that opens the database file in read-only mode at the
**filesystem level**.

**Why we chose it:**
- It's the **lowest-level** protection — enforced by SQLite's VFS layer, not by SQL parsing
- Cannot be bypassed by SQL tricks (`PRAGMA`, `ATTACH`, creative SQL)
- Zero performance cost — same as a normal connection, just with a flag

**What it stops:** Any attempt to modify the database file, including:
- DDL (CREATE, DROP, ALTER)
- DML writes (INSERT, UPDATE, DELETE)
- PRAGMA changes that write to disk

```python
# How we use it
conn = sqlite3.connect("file:database/college_2.sqlite?mode=ro", uri=True)
```

---

### 2. PRAGMA query_only = ON

**What:** A SQLite connection-level setting that tells the engine to reject **any**
write operation on this specific connection.

**Why we chose it:**
- Acts as a **second independent lock** — even if `?mode=ro` were somehow bypassed
- More granular than file permissions — operates at the SQL statement level
- SQLite returns a clear error: `"attempt to write a readonly database"`

**What it stops:** Same as `?mode=ro`, but at the connection/session level rather than
the filesystem level. Catches edge cases where `?mode=ro` might not apply (e.g., in-memory
databases, or attached databases).

```python
cursor.execute("PRAGMA query_only = ON;")
```

---

### 3. Explicit Auto-Rollback

**What:** Every query runs inside a `BEGIN...ROLLBACK` transaction. The `ROLLBACK`
is in a `finally` block, so it executes **even if an exception occurs**.

**Why we chose it:**
- **Belt-and-suspenders** — if layers 1 and 2 both fail, rollback undoes the damage
- We **never intend to write**, so rollback is always the correct action
- `finally` guarantees execution even during crashes or unhandled exceptions

**Why rollback even on success?**
Because our system is **read-only by design**. We only want to *read* query results,
never to persist changes. Rolling back a read-only transaction is a no-op (no data
was changed), but it ensures the pattern is consistent and safe.

```python
try:
    cursor.execute("BEGIN;")
    cursor.execute(sql)
    rows = cursor.fetchall()
finally:
    conn.rollback()  # Always, even on success
    conn.close()
```

---

### 4. Read-Only File Permissions (OS-Level)

**What:** The `setup_readonly_user.py` script creates a copy of the database with
OS-level read-only permissions (`chmod 444`).

**Why we chose it:**
- Protection exists **outside the application** — even if our Python code has bugs
- Any process (not just ours) is blocked from writing
- Useful in production where multiple services might access the same DB file

**What it stops:** Any process on the system from modifying the database file, regardless
of what SQL they send or what connection mode they use.

---

### 5. SELECT-Only Database User (Production)

**What:** For PostgreSQL/MySQL, a dedicated database user with only `SELECT` permissions.

**Why we chose it (for production):**
- The **database engine itself** enforces permissions — not our application code
- Survives application bugs, config errors, and code deployments
- Industry standard for read-only workloads

**SQLite limitation:** SQLite has no user/role system, so we simulate it with the
layers above. The `setup_readonly_user.py` file includes SQL templates for PostgreSQL
and MySQL to make migration straightforward.

---

## Quick Reference

| # | Layer | Level | Enforced By | Bypassable? |
|---|-------|-------|-------------|-------------|
| 1 | Guardrails | Application | Our Python code | Yes (parser tricks) |
| 2 | `?mode=ro` | Filesystem | SQLite VFS | No (OS-level) |
| 3 | `PRAGMA query_only` | Connection | SQLite engine | No (engine-level) |
| 4 | Auto-rollback | Transaction | Our Python code | No (in `finally`) |
| 5 | File permissions | OS | Operating system | No (root only) |
| 6 | DB user perms | Database | DB engine | No (DBA only) |

---

## Key Takeaway

> **No single layer is bulletproof. The combination is.**
>
> Guardrails catch 99% of bad queries cheaply and fast. The sandbox catches the 1%
> that slip through. File permissions and DB users prevent damage even if our entire
> application is compromised.
