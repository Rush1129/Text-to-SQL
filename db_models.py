"""
db_models.py
============
Database models and CRUD operations for users and database connections.

Tables
------
  ``users``           – Application users with hashed passwords and roles.
  ``db_connections``  – User-owned PostgreSQL connection configs with
                        encrypted credentials and cached schemas.

All functions use raw psycopg2 for consistency with the rest of
the codebase. Tables are auto-created via ``ensure_tables()``.

Public surface
--------------
    ensure_tables()          – Create both tables if they don't exist
    create_user()            – Register a new user
    get_user_by_email()      – Look up user by email
    get_user_by_id()         – Look up user by UUID
    create_db_connection()   – Store an encrypted DB connection
    list_user_connections()  – List all connections for a user
    get_connection()         – Get a single connection by ID + owner
    delete_connection()      – Remove a connection
    update_connection_schema() – Cache extracted schema JSON
    get_connection_by_db()   – Find existing connection by host+port+db+user
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from auth import encrypt_value, decrypt_value, hash_password

load_dotenv()

logger = logging.getLogger("db_models")


# =========================================================
# DSN HELPER
# =========================================================

def _admin_dsn() -> str:
    """Build a DSN using the admin DB user for table management."""
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    dbname = os.environ.get("PG_DB", "college_2")
    user = os.environ.get("PG_ADMIN_DB_USER", os.environ.get("PG_ADMIN_USER", "postgres"))
    pwd = os.environ.get("PG_ADMIN_DB_PASSWORD", os.environ.get("PG_ADMIN_PASSWORD", ""))
    return f"host={host} port={port} dbname={dbname} user={user} password={pwd}"


def _conn():
    """Open a psycopg2 connection to the application database."""
    conn = psycopg2.connect(_admin_dsn())
    conn.autocommit = True
    return conn


# =========================================================
# TABLE CREATION
# =========================================================

_CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(20)  NOT NULL DEFAULT 'viewer',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
"""

_CREATE_DB_CONNECTIONS_SQL = """
CREATE TABLE IF NOT EXISTS db_connections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connection_name VARCHAR(255) NOT NULL,
    host            VARCHAR(255) NOT NULL,
    port            INTEGER      NOT NULL DEFAULT 5432,
    database_name   VARCHAR(255) NOT NULL,
    username        VARCHAR(255) NOT NULL,
    password_enc    TEXT         NOT NULL,
    is_verified     BOOLEAN      DEFAULT FALSE,
    schema_json     JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, connection_name)
);

CREATE INDEX IF NOT EXISTS idx_db_connections_user_id
    ON db_connections (user_id);
"""


def ensure_tables() -> None:
    """Create users and db_connections tables if they don't exist."""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(_CREATE_USERS_SQL)
            cur.execute(_CREATE_DB_CONNECTIONS_SQL)
        conn.close()
        logger.info("users and db_connections tables ensured.")
    except psycopg2.Error as exc:
        logger.warning("Could not create tables: %s", exc)


# =========================================================
# USER CRUD
# =========================================================

def create_user(email: str, password: str, role: str = "viewer") -> dict:
    """
    Create a new user with a hashed password.

    Returns the user dict (without password_hash).
    Raises ValueError if the email already exists.
    """
    pw_hash = hash_password(password)
    user_id = str(uuid.uuid4())

    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, password_hash, role)
                VALUES (%s, %s, %s, %s)
                RETURNING id, email, role, created_at;
                """,
                (user_id, email.lower().strip(), pw_hash, role),
            )
            user = dict(cur.fetchone())
        conn.close()

        # Serialise for JSON
        user["id"] = str(user["id"])
        user["created_at"] = user["created_at"].isoformat()
        return user

    except psycopg2.errors.UniqueViolation:
        raise ValueError(f"A user with email '{email}' already exists.")
    except psycopg2.Error as exc:
        logger.error("create_user failed: %s", exc)
        raise


def get_user_by_email(email: str) -> Optional[dict]:
    """Return user dict (including password_hash) or None."""
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, email, password_hash, role, created_at "
                "FROM users WHERE email = %s;",
                (email.lower().strip(),),
            )
            row = cur.fetchone()
        conn.close()

        if row:
            row = dict(row)
            row["id"] = str(row["id"])
            row["created_at"] = row["created_at"].isoformat()
            return row
        return None

    except psycopg2.Error as exc:
        logger.error("get_user_by_email failed: %s", exc)
        return None


def get_user_by_id(user_id: str) -> Optional[dict]:
    """Return user dict (without password_hash) or None."""
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, email, role, created_at "
                "FROM users WHERE id = %s;",
                (user_id,),
            )
            row = cur.fetchone()
        conn.close()

        if row:
            row = dict(row)
            row["id"] = str(row["id"])
            row["created_at"] = row["created_at"].isoformat()
            return row
        return None

    except psycopg2.Error as exc:
        logger.error("get_user_by_id failed: %s", exc)
        return None


# =========================================================
# DB CONNECTION CRUD
# =========================================================

def create_db_connection(
    user_id: str,
    connection_name: str,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password: str,
    is_verified: bool = False,
    schema_json: dict | None = None,
) -> dict:
    """
    Store a new database connection with encrypted password.

    Returns the connection dict (password excluded).
    """
    password_enc = encrypt_value(password)
    conn_id = str(uuid.uuid4())

    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO db_connections
                    (id, user_id, connection_name, host, port,
                     database_name, username, password_enc,
                     is_verified, schema_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, user_id, connection_name, host, port,
                          database_name, username, is_verified,
                          schema_json, created_at;
                """,
                (
                    conn_id, user_id, connection_name, host, port,
                    database_name, username, password_enc,
                    is_verified,
                    json.dumps(schema_json) if schema_json else None,
                ),
            )
            row = dict(cur.fetchone())
        conn.close()

        row["id"] = str(row["id"])
        row["user_id"] = str(row["user_id"])
        row["created_at"] = row["created_at"].isoformat()
        return row

    except psycopg2.errors.UniqueViolation:
        raise ValueError(
            f"Connection name '{connection_name}' already exists for this user."
        )
    except psycopg2.Error as exc:
        logger.error("create_db_connection failed: %s", exc)
        raise


def list_user_connections(user_id: str) -> list[dict]:
    """Return all connections for a user (passwords excluded)."""
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, user_id, connection_name, host, port,
                       database_name, username, is_verified,
                       schema_json, created_at, updated_at
                FROM db_connections
                WHERE user_id = %s
                ORDER BY created_at DESC;
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        conn.close()

        result = []
        for row in rows:
            row = dict(row)
            row["id"] = str(row["id"])
            row["user_id"] = str(row["user_id"])
            row["created_at"] = row["created_at"].isoformat()
            row["updated_at"] = row["updated_at"].isoformat()
            # Count tables in cached schema
            if row.get("schema_json"):
                row["table_count"] = len(row["schema_json"])
            else:
                row["table_count"] = 0
            result.append(row)

        return result

    except psycopg2.Error as exc:
        logger.error("list_user_connections failed: %s", exc)
        return []


def get_connection(conn_id: str, user_id: str) -> Optional[dict]:
    """
    Get a single connection by ID, scoped to the user.

    Returns the connection dict WITH decrypted password (for use in DSN).
    """
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, user_id, connection_name, host, port,
                       database_name, username, password_enc,
                       is_verified, schema_json, created_at
                FROM db_connections
                WHERE id = %s AND user_id = %s;
                """,
                (conn_id, user_id),
            )
            row = cur.fetchone()
        conn.close()

        if row:
            row = dict(row)
            row["id"] = str(row["id"])
            row["user_id"] = str(row["user_id"])
            row["created_at"] = row["created_at"].isoformat()
            # Decrypt password for runtime use
            row["password"] = decrypt_value(row.pop("password_enc"))
            return row
        return None

    except psycopg2.Error as exc:
        logger.error("get_connection failed: %s", exc)
        return None


def get_connection_by_db(
    user_id: str,
    host: str,
    port: int,
    database_name: str,
    username: str,
) -> Optional[dict]:
    """
    Find an existing connection matching host+port+db+username for a user.

    Used to detect re-connections and skip schema re-extraction.
    Returns connection dict (without decrypted password) or None.
    """
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, user_id, connection_name, host, port,
                       database_name, username, is_verified,
                       schema_json, created_at
                FROM db_connections
                WHERE user_id = %s AND host = %s AND port = %s
                      AND database_name = %s AND username = %s
                LIMIT 1;
                """,
                (user_id, host, port, database_name, username),
            )
            row = cur.fetchone()
        conn.close()

        if row:
            row = dict(row)
            row["id"] = str(row["id"])
            row["user_id"] = str(row["user_id"])
            row["created_at"] = row["created_at"].isoformat()
            if row.get("schema_json"):
                row["table_count"] = len(row["schema_json"])
            else:
                row["table_count"] = 0
            return row
        return None

    except psycopg2.Error as exc:
        logger.error("get_connection_by_db failed: %s", exc)
        return None


def delete_connection(conn_id: str, user_id: str) -> bool:
    """Delete a connection (scoped to user). Returns True if deleted."""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM db_connections WHERE id = %s AND user_id = %s;",
                (conn_id, user_id),
            )
            deleted = cur.rowcount > 0
        conn.close()
        return deleted

    except psycopg2.Error as exc:
        logger.error("delete_connection failed: %s", exc)
        return False


def update_connection_schema(conn_id: str, schema_json: dict) -> None:
    """Cache the extracted schema JSON for a connection."""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE db_connections
                SET schema_json = %s,
                    is_verified = TRUE,
                    updated_at = NOW()
                WHERE id = %s;
                """,
                (json.dumps(schema_json), conn_id),
            )
        conn.close()

    except psycopg2.Error as exc:
        logger.error("update_connection_schema failed: %s", exc)


def update_connection_password(conn_id: str, user_id: str, password: str) -> None:
    """Update the encrypted password for a connection."""
    password_enc = encrypt_value(password)
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE db_connections
                SET password_enc = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s;
                """,
                (password_enc, conn_id, user_id),
            )
        conn.close()

    except psycopg2.Error as exc:
        logger.error("update_connection_password failed: %s", exc)
