"""
rbac.py
=======
Role-Based Access Control (RBAC) for the Text-to-SQL system.

Defines three roles (Viewer, Editor, Admin) with a permission matrix
that governs which SQL operations each role may execute.  Also provides
a DSN resolver so each role connects through its own PostgreSQL user,
enforcing privilege separation at the database level.

Public surface
--------------
    Role              – Enum of allowed roles
    Permission        – Enum of granular permissions
    ROLE_PERMISSIONS  – Mapping of Role → set[Permission]
    UserContext       – Dataclass carrying user identity + role
    check_permission  – Raises PermissionDeniedError on denial
    get_dsn_for_role  – Returns the PostgreSQL DSN for a given role
    resolve_user_context – Extracts UserContext from request headers
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# =========================================================
# EXCEPTIONS
# =========================================================

class PermissionDeniedError(Exception):
    """Raised when a user's role lacks a required permission."""

    def __init__(self, role: "Role", permission: "Permission", detail: str = ""):
        self.role = role
        self.permission = permission
        self.detail = detail or (
            f"Role '{role.value}' does not have permission '{permission.value}'."
        )
        super().__init__(self.detail)


# =========================================================
# ROLE ENUM
# =========================================================

class Role(str, Enum):
    """Application roles ordered by increasing privilege."""
    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN  = "admin"


# =========================================================
# PERMISSION ENUM
# =========================================================

class Permission(str, Enum):
    """Granular permissions checked before operations."""
    QUERY_READ     = "query_read"       # Run SELECT queries
    QUERY_WRITE    = "query_write"      # Run INSERT / UPDATE / DELETE
    QUERY_DDL      = "query_ddl"        # Run CREATE / ALTER / DROP
    VIEW_SCHEMA    = "view_schema"      # Access GET /v1/schema
    VIEW_HISTORY   = "view_history"     # Access GET /v1/history
    VIEW_AUDIT_LOG = "view_audit_log"   # Access GET /v1/audit


# =========================================================
# PERMISSION MATRIX
# =========================================================

ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.VIEWER: {
        Permission.QUERY_READ,
        Permission.VIEW_SCHEMA,
        Permission.VIEW_HISTORY,
    },
    Role.EDITOR: {
        Permission.QUERY_READ,
        Permission.QUERY_WRITE,
        Permission.VIEW_SCHEMA,
        Permission.VIEW_HISTORY,
    },
    Role.ADMIN: {
        Permission.QUERY_READ,
        Permission.QUERY_WRITE,
        Permission.QUERY_DDL,
        Permission.VIEW_SCHEMA,
        Permission.VIEW_HISTORY,
        Permission.VIEW_AUDIT_LOG,
    },
}


# =========================================================
# USER CONTEXT
# =========================================================

@dataclass(frozen=True)
class UserContext:
    """
    Immutable user identity resolved from request headers.

    Attributes
    ----------
    user_id : str
        Unique identifier for the user (from ``X-User-Id`` header).
    role : Role
        The role claimed by the user (from ``X-User-Role`` header).
    ip_address : str
        Client IP address (for audit logging).
    """

    user_id: str
    role: Role
    ip_address: str = ""


# =========================================================
# PERMISSION CHECK
# =========================================================

def check_permission(role: Role, permission: Permission) -> bool:
    """
    Return True if *role* has *permission*, else raise PermissionDeniedError.
    """
    if permission in ROLE_PERMISSIONS.get(role, set()):
        return True
    raise PermissionDeniedError(role, permission)


def has_permission(role: Role, permission: Permission) -> bool:
    """
    Non-raising variant: return True/False without exceptions.
    """
    return permission in ROLE_PERMISSIONS.get(role, set())


# =========================================================
# REQUIRED PERMISSION FOR SQL TYPE
# =========================================================

# Keywords that indicate DML writes
_DML_WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"}

# Keywords that indicate DDL
_DDL_KEYWORDS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME"}


def permission_for_sql(sql: str) -> Permission:
    """
    Determine which Permission is required to execute *sql*.

    Inspects the first meaningful keyword to classify as
    READ / WRITE / DDL.
    """
    import sqlparse

    parsed = sqlparse.parse(sql.strip())
    if not parsed:
        return Permission.QUERY_READ

    for token in parsed[0].tokens:
        if token.ttype in (
            sqlparse.tokens.Keyword.DDL,
            sqlparse.tokens.Keyword.DML,
            sqlparse.tokens.Keyword,
        ):
            kw = token.normalized.upper()
            if kw in _DDL_KEYWORDS:
                return Permission.QUERY_DDL
            if kw in _DML_WRITE_KEYWORDS:
                return Permission.QUERY_WRITE
            break
        if token.ttype in (
            sqlparse.tokens.Whitespace,
            sqlparse.tokens.Newline,
            sqlparse.tokens.Comment.Single,
            sqlparse.tokens.Comment.Multiline,
        ):
            continue
        break

    return Permission.QUERY_READ


# =========================================================
# DSN RESOLVER  (per-role PostgreSQL user)
# =========================================================

def get_dsn_for_role(role: Role) -> str:
    """
    Return a psycopg2 DSN string using the PostgreSQL user
    that matches *role*.

    Environment variables
    ---------------------
    PG_HOST, PG_PORT, PG_DB          – shared across all roles
    PG_VIEWER_USER / PG_VIEWER_PASSWORD
    PG_EDITOR_USER / PG_EDITOR_PASSWORD
    PG_ADMIN_DB_USER / PG_ADMIN_DB_PASSWORD
    """
    host   = os.environ.get("PG_HOST",  "localhost")
    port   = os.environ.get("PG_PORT",  "5432")
    dbname = os.environ.get("PG_DB",    "college_2")

    user_map = {
        Role.VIEWER: (
            os.environ.get("PG_VIEWER_USER",     "viewer_user"),
            os.environ.get("PG_VIEWER_PASSWORD",  ""),
        ),
        Role.EDITOR: (
            os.environ.get("PG_EDITOR_USER",     "editor_user"),
            os.environ.get("PG_EDITOR_PASSWORD",  ""),
        ),
        Role.ADMIN: (
            os.environ.get("PG_ADMIN_DB_USER",   "admin_user"),
            os.environ.get("PG_ADMIN_DB_PASSWORD", ""),
        ),
    }

    user, password = user_map[role]
    return (
        f"host={host} port={port} dbname={dbname} "
        f"user={user} password={password}"
    )


# =========================================================
# HEADER RESOLUTION (for FastAPI dependency)
# =========================================================

def resolve_user_context(
    user_id: Optional[str] = None,
    role_str: Optional[str] = None,
    ip_address: str = "",
) -> UserContext:
    """
    Build a UserContext from raw header values.

    Defaults to ``viewer`` when no role is provided.
    Defaults to ``anonymous`` when no user_id is provided.

    Raises ValueError if role_str is not a valid Role.
    """
    # Default to viewer
    if not role_str:
        role_str = "viewer"

    # Default user id
    if not user_id:
        user_id = "anonymous"

    try:
        role = Role(role_str.lower().strip())
    except ValueError:
        raise ValueError(
            f"Invalid role '{role_str}'. "
            f"Must be one of: {', '.join(r.value for r in Role)}"
        )

    return UserContext(user_id=user_id, role=role, ip_address=ip_address)
