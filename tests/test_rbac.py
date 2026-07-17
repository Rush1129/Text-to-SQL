"""
test_rbac.py
============
Unit tests for the RBAC module.

Tests cover:
  - Permission mapping correctness
  - check_permission allows/denies correctly
  - has_permission returns bool without raising
  - permission_for_sql classification
  - get_dsn_for_role returns correct DSN per role
  - resolve_user_context header parsing and defaults

Run with:
    pytest tests/test_rbac.py -v
"""

import os
import pytest

from rbac import (
    Permission,
    PermissionDeniedError,
    Role,
    ROLE_PERMISSIONS,
    UserContext,
    check_permission,
    get_dsn_for_role,
    has_permission,
    permission_for_sql,
    resolve_user_context,
)


# =========================================================
# PERMISSION MATRIX TESTS
# =========================================================


class TestRolePermissions:
    """Verify the permission matrix is correct."""

    def test_viewer_has_read(self):
        assert Permission.QUERY_READ in ROLE_PERMISSIONS[Role.VIEWER]

    def test_viewer_lacks_write(self):
        assert Permission.QUERY_WRITE not in ROLE_PERMISSIONS[Role.VIEWER]

    def test_viewer_lacks_ddl(self):
        assert Permission.QUERY_DDL not in ROLE_PERMISSIONS[Role.VIEWER]

    def test_viewer_lacks_audit(self):
        assert Permission.VIEW_AUDIT_LOG not in ROLE_PERMISSIONS[Role.VIEWER]

    def test_viewer_has_schema(self):
        assert Permission.VIEW_SCHEMA in ROLE_PERMISSIONS[Role.VIEWER]

    def test_viewer_has_history(self):
        assert Permission.VIEW_HISTORY in ROLE_PERMISSIONS[Role.VIEWER]

    def test_editor_has_read(self):
        assert Permission.QUERY_READ in ROLE_PERMISSIONS[Role.EDITOR]

    def test_editor_has_write(self):
        assert Permission.QUERY_WRITE in ROLE_PERMISSIONS[Role.EDITOR]

    def test_editor_lacks_ddl(self):
        assert Permission.QUERY_DDL not in ROLE_PERMISSIONS[Role.EDITOR]

    def test_editor_lacks_audit(self):
        assert Permission.VIEW_AUDIT_LOG not in ROLE_PERMISSIONS[Role.EDITOR]

    def test_admin_has_all_permissions(self):
        for perm in Permission:
            assert perm in ROLE_PERMISSIONS[Role.ADMIN], \
                f"Admin should have {perm.value}"


# =========================================================
# check_permission TESTS
# =========================================================


class TestCheckPermission:
    """Test the raising variant of permission checks."""

    def test_allowed_returns_true(self):
        assert check_permission(Role.VIEWER, Permission.QUERY_READ) is True

    def test_denied_raises(self):
        with pytest.raises(PermissionDeniedError) as exc_info:
            check_permission(Role.VIEWER, Permission.QUERY_WRITE)
        assert "viewer" in str(exc_info.value).lower()
        assert exc_info.value.role == Role.VIEWER
        assert exc_info.value.permission == Permission.QUERY_WRITE

    def test_editor_write_allowed(self):
        assert check_permission(Role.EDITOR, Permission.QUERY_WRITE) is True

    def test_editor_ddl_denied(self):
        with pytest.raises(PermissionDeniedError):
            check_permission(Role.EDITOR, Permission.QUERY_DDL)

    def test_admin_ddl_allowed(self):
        assert check_permission(Role.ADMIN, Permission.QUERY_DDL) is True

    def test_admin_audit_allowed(self):
        assert check_permission(Role.ADMIN, Permission.VIEW_AUDIT_LOG) is True


# =========================================================
# has_permission TESTS
# =========================================================


class TestHasPermission:
    """Test the non-raising variant."""

    def test_viewer_read_true(self):
        assert has_permission(Role.VIEWER, Permission.QUERY_READ) is True

    def test_viewer_write_false(self):
        assert has_permission(Role.VIEWER, Permission.QUERY_WRITE) is False

    def test_editor_write_true(self):
        assert has_permission(Role.EDITOR, Permission.QUERY_WRITE) is True

    def test_admin_audit_true(self):
        assert has_permission(Role.ADMIN, Permission.VIEW_AUDIT_LOG) is True


# =========================================================
# permission_for_sql TESTS
# =========================================================


class TestPermissionForSql:
    """Test SQL classification into permissions."""

    def test_select(self):
        assert permission_for_sql("SELECT * FROM student") == Permission.QUERY_READ

    def test_select_with_join(self):
        sql = "SELECT s.name FROM student s JOIN department d ON s.dept_id = d.id"
        assert permission_for_sql(sql) == Permission.QUERY_READ

    def test_insert(self):
        sql = "INSERT INTO student (name) VALUES ('Alice')"
        assert permission_for_sql(sql) == Permission.QUERY_WRITE

    def test_update(self):
        sql = "UPDATE student SET name = 'Bob' WHERE id = 1"
        assert permission_for_sql(sql) == Permission.QUERY_WRITE

    def test_delete(self):
        sql = "DELETE FROM student WHERE id = 1"
        assert permission_for_sql(sql) == Permission.QUERY_WRITE

    def test_create_table(self):
        sql = "CREATE TABLE test_table (id INTEGER)"
        assert permission_for_sql(sql) == Permission.QUERY_DDL

    def test_drop_table(self):
        sql = "DROP TABLE student"
        assert permission_for_sql(sql) == Permission.QUERY_DDL

    def test_alter_table(self):
        sql = "ALTER TABLE student ADD COLUMN email VARCHAR(255)"
        assert permission_for_sql(sql) == Permission.QUERY_DDL

    def test_truncate(self):
        sql = "TRUNCATE TABLE student"
        assert permission_for_sql(sql) == Permission.QUERY_DDL

    def test_empty_string(self):
        assert permission_for_sql("") == Permission.QUERY_READ

    def test_with_cte(self):
        sql = "WITH cte AS (SELECT * FROM student) SELECT * FROM cte"
        # WITH is a keyword, not DML/DDL — defaults to READ
        assert permission_for_sql(sql) == Permission.QUERY_READ


# =========================================================
# get_dsn_for_role TESTS
# =========================================================


class TestGetDsnForRole:
    """Test DSN resolution per role."""

    def test_viewer_uses_viewer_user(self, monkeypatch):
        monkeypatch.setenv("PG_HOST", "testhost")
        monkeypatch.setenv("PG_PORT", "5433")
        monkeypatch.setenv("PG_DB", "testdb")
        monkeypatch.setenv("PG_VIEWER_USER", "test_viewer")
        monkeypatch.setenv("PG_VIEWER_PASSWORD", "viewerpass")

        dsn = get_dsn_for_role(Role.VIEWER)
        assert "user=test_viewer" in dsn
        assert "password=viewerpass" in dsn
        assert "host=testhost" in dsn
        assert "dbname=testdb" in dsn

    def test_editor_uses_editor_user(self, monkeypatch):
        monkeypatch.setenv("PG_EDITOR_USER", "test_editor")
        monkeypatch.setenv("PG_EDITOR_PASSWORD", "editorpass")

        dsn = get_dsn_for_role(Role.EDITOR)
        assert "user=test_editor" in dsn
        assert "password=editorpass" in dsn

    def test_admin_uses_admin_user(self, monkeypatch):
        monkeypatch.setenv("PG_ADMIN_DB_USER", "test_admin")
        monkeypatch.setenv("PG_ADMIN_DB_PASSWORD", "adminpass")

        dsn = get_dsn_for_role(Role.ADMIN)
        assert "user=test_admin" in dsn
        assert "password=adminpass" in dsn

    def test_each_role_returns_different_user(self, monkeypatch):
        monkeypatch.setenv("PG_VIEWER_USER", "v_user")
        monkeypatch.setenv("PG_EDITOR_USER", "e_user")
        monkeypatch.setenv("PG_ADMIN_DB_USER", "a_user")

        dsn_v = get_dsn_for_role(Role.VIEWER)
        dsn_e = get_dsn_for_role(Role.EDITOR)
        dsn_a = get_dsn_for_role(Role.ADMIN)

        assert "v_user" in dsn_v
        assert "e_user" in dsn_e
        assert "a_user" in dsn_a


# =========================================================
# resolve_user_context TESTS
# =========================================================


class TestResolveUserContext:
    """Test user context resolution from headers."""

    def test_valid_viewer(self):
        ctx = resolve_user_context(user_id="alice", role_str="viewer")
        assert ctx.user_id == "alice"
        assert ctx.role == Role.VIEWER

    def test_valid_editor(self):
        ctx = resolve_user_context(user_id="bob", role_str="editor")
        assert ctx.role == Role.EDITOR

    def test_valid_admin(self):
        ctx = resolve_user_context(user_id="charlie", role_str="admin")
        assert ctx.role == Role.ADMIN

    def test_case_insensitive_role(self):
        ctx = resolve_user_context(user_id="test", role_str="VIEWER")
        assert ctx.role == Role.VIEWER

    def test_whitespace_in_role(self):
        ctx = resolve_user_context(user_id="test", role_str="  editor  ")
        assert ctx.role == Role.EDITOR

    def test_defaults_to_viewer(self):
        ctx = resolve_user_context(user_id="test", role_str="")
        assert ctx.role == Role.VIEWER

    def test_defaults_to_viewer_when_none(self):
        ctx = resolve_user_context(user_id="test", role_str=None)
        assert ctx.role == Role.VIEWER

    def test_defaults_user_id_to_anonymous(self):
        ctx = resolve_user_context(user_id="", role_str="viewer")
        assert ctx.user_id == "anonymous"

    def test_defaults_user_id_when_none(self):
        ctx = resolve_user_context(user_id=None, role_str="viewer")
        assert ctx.user_id == "anonymous"

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="Invalid role"):
            resolve_user_context(user_id="test", role_str="superadmin")

    def test_ip_address_preserved(self):
        ctx = resolve_user_context(
            user_id="test", role_str="viewer", ip_address="192.168.1.1"
        )
        assert ctx.ip_address == "192.168.1.1"

    def test_context_is_frozen(self):
        ctx = resolve_user_context(user_id="test", role_str="viewer")
        with pytest.raises(AttributeError):
            ctx.user_id = "hacked"
