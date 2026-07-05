from pydantic import BaseModel, Field
# pyrefly: ignore [missing-import]
import sqlparse


# =========================================================
# PYDANTIC MODELS FOR STRUCTURED OUTPUT
# =========================================================

class ColumnAccess(BaseModel):
    """Represents a single column accessed in the SQL query."""

    table: str = Field(
        description="The table name this column belongs to"
    )
    column: str = Field(
        description="The column name accessed"
    )


class StructuredSQLResponse(BaseModel):
    """Structured response from the LLM for SQL generation."""

    sql_query: str = Field(
        description=(
            "The generated SQL query. Must be valid "
            "PostgreSQL syntax."
        )
    )

    explanation: str = Field(
        description=(
            "A clear, natural language explanation "
            "of what this SQL query does."
        )
    )

    confidence_score: float = Field(
        description=(
            "Confidence score between 0.0 and 1.0 "
            "indicating how confident the model is "
            "that this query correctly answers the "
            "user's question."
        ),
        ge=0.0,
        le=1.0
    )

    tables_accessed: list[str] = Field(
        description=(
            "List of all table names accessed "
            "in the query."
        )
    )

    columns_accessed: list[ColumnAccess] = Field(
        description=(
            "List of all columns accessed in the "
            "query, each with its table name."
        )
    )


# =========================================================
# SQL SYNTAX VALIDATION USING SQLPARSE
# =========================================================

# Recognized DML statement types
VALID_STATEMENT_TYPES = {
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
}


def validate_sql_syntax(sql: str) -> tuple[bool, str]:
    """
    Validate SQL syntax using sqlparse.

    Checks:
      1. SQL string is non-empty
      2. Parses into exactly one statement
      3. Statement type is a recognized DML type

    Args:
        sql: The SQL query string to validate.

    Returns:
        A tuple of (is_valid, message).
        - (True, "Valid SQL") if the query passes
          all checks.
        - (False, reason) if any check fails.
    """

    # ── Check 1: Non-empty ──────────────────────────
    cleaned = sql.strip()

    if not cleaned:
        return (False, "SQL query is empty.")

    # ── Check 2: Parse and count statements ─────────
    try:
        parsed_statements = sqlparse.parse(cleaned)
    except Exception as e:
        return (False, f"SQL parse error: {e}")

    if not parsed_statements:
        return (
            False,
            "SQL could not be parsed into any "
            "statements."
        )

    # Filter out empty / whitespace-only statements
    non_empty = [
        stmt for stmt in parsed_statements
        if stmt.tokens
        and str(stmt).strip()
    ]

    if len(non_empty) == 0:
        return (
            False,
            "SQL contains no valid statements."
        )

    if len(non_empty) > 1:
        return (
            False,
            f"Expected exactly 1 SQL statement, "
            f"found {len(non_empty)}. "
            f"Please provide a single query."
        )

    # ── Check 3: Recognized statement type ──────────
    statement = non_empty[0]
    stmt_type = statement.get_type()

    if stmt_type is None:
        return (
            False,
            "Could not determine SQL statement type. "
            "The query may be malformed."
        )

    if stmt_type.upper() not in VALID_STATEMENT_TYPES:
        return (
            False,
            f"Unsupported statement type: "
            f"'{stmt_type}'. "
            f"Expected one of: "
            f"{', '.join(sorted(VALID_STATEMENT_TYPES))}."
        )

    # ── Check 4: Format and verify round-trip ───────
    try:
        formatted = sqlparse.format(
            cleaned,
            reindent=True,
            keyword_case="upper"
        )

        if not formatted.strip():
            return (
                False,
                "SQL formatting produced empty output."
            )
    except Exception as e:
        return (
            False,
            f"SQL formatting error: {e}"
        )

    return (True, "Valid SQL")
