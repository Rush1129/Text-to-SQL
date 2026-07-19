"""
pipeline.py
===========
Shared pipeline objects and the central `run_query()` function.

Both the CLI (local_main.py) and the API (main.py) import from here.
All pipeline-internal progress is emitted via `logging` — no print()
calls so HTTP responses stay clean.

Public surface
--------------
    QueryResult   – dataclass returned by run_query()
    run_query()   – runs the full Text-to-SQL pipeline for one question
    schema        – the loaded schema dict (for GET /v1/schema)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import chromadb
from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_groq import ChatGroq

from guardrails import SQLGuardrail, SandboxExecutor
from guardrails.sandbox_executor import build_dsn
from guardrails.sql_guardrails import RiskLevel
from models import StructuredSQLResponse, validate_sql_syntax
from rbac import Role, get_dsn_for_role
from schema.extractor import get_collection_name
from verification import (
    ConfidenceScorer,
    ResultSanityChecker,
    SQLVerifier,
)

load_dotenv()

# =========================================================
# LOGGING SETUP
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pipeline")

# =========================================================
# LOAD SCHEMA
# =========================================================

with open("outputs/schema.json", "r") as f:
    schema: dict = json.load(f)

logger.info("Schema loaded: %d tables.", len(schema))

# =========================================================
# LOAD CHROMADB COLLECTION
# =========================================================

chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_collection(name="table_schemas")
logger.info("ChromaDB collection loaded.")

# =========================================================
# LOAD FEW-SHOT EXAMPLES
# =========================================================

try:
    with open("examples/examples.json", "r") as f:
        examples: list = json.load(f)
    logger.info("Loaded %d few-shot examples.", len(examples))
except FileNotFoundError:
    examples = []
    logger.warning("examples/examples.json not found — running without examples.")

# =========================================================
# LLM SETUP
# =========================================================

llm = ChatGroq(model="openai/gpt-oss-20b", temperature=0)
structured_llm = llm.with_structured_output(StructuredSQLResponse)
logger.info("LLM initialised.")

# =========================================================
# INFRASTRUCTURE OBJECTS
# =========================================================

PG_DSN = build_dsn()

guardrail     = SQLGuardrail()
sandbox       = SandboxExecutor(dsn=PG_DSN, readonly=True)
verifier      = SQLVerifier(llm=llm, flag_threshold=0.65)
sanity_checker = ResultSanityChecker(
    null_pct_threshold=0.40,
    overflow_limit=1e9,
    date_min_year=1900,
    date_max_year=2100,
)
conf_scorer = ConfidenceScorer(schema=schema)

# Cache of role-specific sandbox executors
_role_sandbox_cache: dict[Role, SandboxExecutor] = {}


def _get_sandbox_for_role(role: Role) -> SandboxExecutor:
    """
    Return a SandboxExecutor using the PostgreSQL DSN that
    matches *role*.  Viewer gets readonly=True; Editor/Admin
    get readonly=False so DML writes can persist.

    Results are cached per role.
    """
    if role not in _role_sandbox_cache:
        dsn = get_dsn_for_role(role)
        readonly = (role == Role.VIEWER)
        _role_sandbox_cache[role] = SandboxExecutor(
            dsn=dsn, readonly=readonly,
        )
        logger.info(
            "Created sandbox for role=%s (readonly=%s).",
            role.value, readonly,
        )
    return _role_sandbox_cache[role]

logger.info("All pipeline components ready.")


# =========================================================
# MULTI-DB CONNECTION SUPPORT
# =========================================================

def _load_connection_resources(
    connection_id: str,
    user_id: str,
) -> tuple[dict, any, SandboxExecutor]:
    """
    Load schema, ChromaDB collection, and a SandboxExecutor for a
    user's database connection.

    Returns (conn_schema, conn_collection, conn_sandbox).
    Raises RuntimeError if the connection is not found or has no schema.
    """
    from db_models import get_connection  # Lazy import to avoid circular

    conn = get_connection(connection_id, user_id)
    if not conn:
        raise RuntimeError(f"Connection '{connection_id}' not found.")

    # Load cached schema from the connection
    conn_schema = conn.get("schema_json")
    if not conn_schema:
        raise RuntimeError(
            f"Connection '{conn.get('connection_name')}' has no cached schema. "
            f"Please reconnect to extract the schema."
        )

    # Load the connection-specific ChromaDB collection
    coll_name = get_collection_name(connection_id)
    try:
        conn_collection = chroma_client.get_collection(name=coll_name)
    except Exception:
        raise RuntimeError(
            f"Embeddings not found for connection '{conn.get('connection_name')}'. "
            f"Please reconnect to rebuild embeddings."
        )

    # Build a DSN from the connection details
    dsn = (
        f"host={conn['host']} port={conn['port']} "
        f"dbname={conn['database_name']} "
        f"user={conn['username']} password={conn['password']}"
    )
    conn_sandbox = SandboxExecutor(dsn=dsn, readonly=True)

    logger.info(
        "Loaded connection resources: conn=%s, tables=%d, collection=%s",
        connection_id, len(conn_schema), coll_name,
    )

    return conn_schema, conn_collection, conn_sandbox

# =========================================================
# PROMPT CHAIN (used by ambiguity judge)
# =========================================================

_prompt_template = PromptTemplate(
    input_variables=["prompt"],
    template="{prompt}",
)
_chain = _prompt_template | llm | StrOutputParser()

# =========================================================
# QUERY RESULT
# =========================================================

@dataclass
class QueryResult:
    """
    The complete, JSON-serialisable output of one pipeline run.

    Every field is a plain Python type (str, int, float, list, dict)
    so it can be returned directly by FastAPI without extra conversion.
    """

    question:                 str
    sql:                      str               # raw generated SQL
    safe_sql:                 str               # guardrail-modified SQL
    explanation:              str
    tables_accessed:          list[str]         = field(default_factory=list)
    columns_accessed:         list[dict]        = field(default_factory=list)

    # Execution
    execution_results:        list[dict]        = field(default_factory=list)
    dataframe:                Optional[Any]     = None   # raw pandas DataFrame from sandbox
    row_count:                int               = 0
    execution_time_ms:        float             = 0.0
    execution_error:          Optional[str]     = None

    # SQL validation
    sql_valid:                bool              = False
    validation_message:       str               = ""

    # Guardrails
    guardrail_allowed:        bool              = True
    guardrail_warnings:       list[str]         = field(default_factory=list)
    guardrail_limit_applied:  bool              = False
    risk_level:               str               = "safe"   # safe | moderate | risky
    risk_warning:             str               = ""       # Human-readable warning

    # Back-translation verification
    back_translated_question: str               = ""
    alignment_score:          float             = 0.0
    alignment_label:          str               = ""
    alignment_flagged:        bool              = False
    alignment_flag_reason:    Optional[str]     = None
    judge_reason:             Optional[str]     = None

    # Sanity
    sanity_anomalies:         list[dict]        = field(default_factory=list)
    sanity_pass_rate:         float             = 1.0
    sanity_summary:           str               = ""

    # Composite confidence
    confidence:               dict              = field(default_factory=dict)

    # Clarification (when question is ambiguous)
    needs_clarification:      bool              = False
    clarification_request:    Optional[dict]    = None

    # Top-level error (unexpected exception)
    error:                    Optional[str]     = None

    # RBAC: rows modified by DML operations
    rows_affected:            int               = 0

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON serialisation."""
        # Serialise the raw DataFrame to a list of dicts (NaN → None).
        # Falls back to execution_results if dataframe is not available.
        if self.dataframe is not None and not self.dataframe.empty:
            df_records = (
                self.dataframe
                .where(self.dataframe.notna(), other=None)
                .to_dict(orient="records")
            )
        else:
            df_records = self.execution_results  # already a list[dict]

        return {
            "question":                 self.question,
            "sql":                      self.sql,
            "safe_sql":                 self.safe_sql,
            "explanation":              self.explanation,
            "tables_accessed":          self.tables_accessed,
            "columns_accessed":         self.columns_accessed,
            "execution_results":        self.execution_results,
            "dataframe":                df_records,
            "row_count":                self.row_count,
            "execution_time_ms":        round(self.execution_time_ms, 3),
            "execution_error":          self.execution_error,
            "sql_valid":                self.sql_valid,
            "validation_message":       self.validation_message,
            "guardrail_allowed":        self.guardrail_allowed,
            "guardrail_warnings":       self.guardrail_warnings,
            "guardrail_limit_applied":  self.guardrail_limit_applied,
            "risk_level":               self.risk_level,
            "risk_warning":             self.risk_warning,
            "back_translated_question": self.back_translated_question,
            "alignment_score":          round(self.alignment_score, 4),
            "alignment_label":          self.alignment_label,
            "alignment_flagged":        self.alignment_flagged,
            "alignment_flag_reason":    self.alignment_flag_reason,
            "judge_reason":             self.judge_reason,
            "sanity_anomalies":         self.sanity_anomalies,
            "sanity_pass_rate":         round(self.sanity_pass_rate, 4),
            "sanity_summary":           self.sanity_summary,
            "confidence":               self.confidence,
            "needs_clarification":      self.needs_clarification,
            "clarification_request":    self.clarification_request,
            "error":                    self.error,
            "rows_affected":            self.rows_affected,
        }


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def get_relevant_tables(
    question: str,
    top_k: int = 3,
    coll: any = None,
) -> list[str]:
    """Query ChromaDB for relevant tables. Uses default or override collection."""
    target = coll or collection
    results = target.query(query_texts=[question], n_results=top_k)
    return [m["table_name"] for m in results["metadatas"][0]]


def format_schema(
    relevant_tables: list[str],
    s: dict | None = None,
) -> str:
    """Format schema text for the prompt. Uses default or override schema."""
    target = s or schema
    out = ""
    for table in relevant_tables:
        info = target.get(table)
        if not info:
            continue
        out += f"\nTable: {table}\nColumns:\n"
        for col in info["columns"]:
            out += f"- {col['name']} ({col['type']})\n"
        if info.get("primary_keys"):
            out += "Primary Keys:\n"
            for pk in info["primary_keys"]:
                out += f"- {pk}\n"
    return out


def format_relationships(
    relevant_tables: list[str],
    s: dict | None = None,
) -> str:
    """Format relationship text for the prompt. Uses default or override schema."""
    target = s or schema
    out = "\nRelationships:\n"
    for table in relevant_tables:
        info = target.get(table)
        if not info:
            continue
        for fk in info.get("foreign_keys", []):
            out += (
                f"{table}.{fk['constrained_columns']} → "
                f"{fk['referred_table']}.{fk['referred_columns']}\n"
            )
    return out


def format_examples() -> str:
    if not examples:
        return ""
    out = "\nExamples:\n"
    for ex in examples:
        out += f"\nQuestion: {ex['question']}\nSQL: {ex['sql']}\n"
    return out


def _extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM did not return a JSON object: {text}")
    return json.loads(cleaned[start:end + 1])


def build_ambiguity_prompt(
    user_question: str,
    s: dict | None = None,
    coll: any = None,
) -> str:
    target = s or schema
    all_tables       = list(target.keys())
    schema_text      = format_schema(all_tables, s=target)
    relationship_text = format_relationships(all_tables, s=target)
    example_text     = format_examples()
    return f"""
You are an ambiguity judge for a text-to-SQL system.

Your job is to decide whether the user's question has multiple plausible
SQL interpretations given the database schema, relationships, and examples.

Return ONLY valid JSON. Do not wrap it in markdown.

Use this exact JSON shape:
{{
  "is_ambiguous": true,
  "clarification_request": {{
    "type": "clarification_request",
    "reason": "ambiguous_user_question",
    "ambiguous_term": "short phrase that caused ambiguity",
    "message": "Ask the user to clarify the intended interpretation.",
    "interpretations": [
      {{
        "label": "snake_case_label",
        "description": "What this interpretation means.",
        "example_query": "A clarified natural language question."
      }}
    ],
    "original_question": "The original user question."
  }}
}}

If the question is not ambiguous, return:
{{
  "is_ambiguous": false,
  "clarification_request": null
}}

Guidelines:
1. Mark ambiguous ONLY when two or more materially different SQL queries
   are EQUALLY plausible and a wrong choice would produce a clearly wrong answer.
2. If one interpretation is clearly more natural or common than others,
   choose it silently — do NOT ask for clarification.
3. Do not mark broad but clear requests as ambiguous.
4. Do not guess business definitions.
5. Simple rewording or paraphrasing of the same logical question is NOT ambiguity.
6. List interpretations only when the difference is large enough that the user
   would be confused or misled by any single choice.
7. Use only concepts supported by the schema and examples.
8. Prefer returning is_ambiguous=false unless you are highly confident
   that the question genuinely requires human disambiguation.

Database Schema:
{schema_text}

{relationship_text}

{example_text}

User Question:
{user_question}
"""


def detect_ambiguity(
    user_question: str,
    s: dict | None = None,
    coll: any = None,
) -> Optional[dict]:
    logger.debug("Running ambiguity check for: %r", user_question)
    prompt = build_ambiguity_prompt(user_question, s=s, coll=coll)
    try:
        raw = _chain.invoke({"prompt": prompt})
        parsed = _extract_json_object(raw)
        if parsed.get("is_ambiguous"):
            logger.info("Question flagged as ambiguous: %r", user_question)
            return parsed.get("clarification_request")
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Ambiguity judge failed to parse: %s", exc)
        return {
            "type":           "clarification_request",
            "reason":         "ambiguity_judge_invalid_response",
            "ambiguous_term": "unknown",
            "message": (
                "I could not reliably determine whether the question is "
                "ambiguous. Please rephrase with the exact metric, entity, "
                "time period, and filters you want."
            ),
            "interpretations": [{
                "label":       "rephrased_question",
                "description": (
                    "A more specific version of the question with the "
                    "intended meaning made explicit."
                ),
                "example_query": "Count enrolled students by department for Fall 2010.",
            }],
            "original_question": user_question,
        }


def build_sql_prompt(
    user_question: str,
    s: dict | None = None,
    coll: any = None,
) -> str:
    relevant_tables   = get_relevant_tables(user_question, coll=coll)
    schema_text       = format_schema(relevant_tables, s=s)
    relationship_text = format_relationships(relevant_tables, s=s)
    example_text      = format_examples()
    return f"""
You are an expert PostgreSQL SQL generator.

Rules:
1. Use ONLY tables and columns provided in the schema.
2. Generate syntactically correct PostgreSQL SQL.
3. Use proper JOINs using the relationships provided.
4. Do NOT use SQLite-specific syntax (e.g. no strftime, no AUTOINCREMENT).
   Use PostgreSQL equivalents: TO_CHAR, SERIAL/GENERATED ALWAYS AS IDENTITY, etc.
5. If the question is ambiguous, do not guess.
6. For the confidence_score, rate your confidence from 0.0 to 1.0.
7. In tables_accessed, list every table name referenced in the query.
8. In columns_accessed, list every column with its table name.
9. In explanation, provide a clear natural language description.

Database Schema:
{schema_text}

{relationship_text}

{example_text}

User Question:
{user_question}
"""


def generate_structured_sql(
    user_question: str,
    s: dict | None = None,
    coll: any = None,
) -> tuple[StructuredSQLResponse, bool, str]:
    """Generate SQL, return (response, is_valid, validation_msg)."""
    prompt_text = build_sql_prompt(user_question, s=s, coll=coll)
    response    = structured_llm.invoke(prompt_text)
    is_valid, msg = validate_sql_syntax(response.sql_query)
    logger.info(
        "SQL generated | valid=%s | tables=%s",
        is_valid, response.tables_accessed,
    )
    return response, is_valid, msg


# =========================================================
# MAIN PIPELINE FUNCTION
# =========================================================

def run_query(
    question: str,
    confirmed: bool = False,
    role: str | None = None,
    connection_id: str | None = None,
    user_id: str | None = None,
) -> QueryResult:
    """
    Run the complete Text-to-SQL pipeline for *question*.

    Steps
    -----
    1. Ambiguity detection
    2. SQL generation + syntax validation
    3. Guardrail check + risk classification
    4. Back-translation verification
    5. Sandbox execution  (skipped when risk > SAFE and not confirmed)
    6. Result sanity check
    7. Confidence scoring

    Parameters
    ----------
    question      : Natural-language question from the user.
    confirmed     : If True, the user has acknowledged the risk warning
                    and wants to proceed with execution regardless.
    role          : User's role (viewer / editor / admin). Determines
                    which PostgreSQL user and sandbox mode to use.
    connection_id : UUID of a user's database connection. When provided,
                    the pipeline uses that connection's schema, embeddings,
                    and DSN instead of the defaults.
    user_id       : UUID of the authenticated user (required when
                    connection_id is provided).

    Returns a QueryResult dataclass.  On unexpected errors the
    ``error`` field is set and all other fields have safe defaults.
    """

    logger.info("run_query: %r (connection=%s)", question, connection_id or "default")

    result = QueryResult(question=question, sql="", safe_sql="", explanation="")

    # Load connection-specific resources or use defaults
    conn_schema = None
    conn_collection = None
    conn_sandbox = None

    if connection_id and user_id:
        try:
            conn_schema, conn_collection, conn_sandbox = (
                _load_connection_resources(connection_id, user_id)
            )
        except RuntimeError as exc:
            result.error = str(exc)
            return result

    # Resolve which sandbox to use
    if conn_sandbox:
        active_sandbox = conn_sandbox
    elif role:
        try:
            role_enum = Role(role)
        except ValueError:
            role_enum = Role.VIEWER
        active_sandbox = _get_sandbox_for_role(role_enum)
    else:
        active_sandbox = sandbox  # Default readonly sandbox

    # Use connection-specific schema & collection or defaults
    active_schema = conn_schema or schema
    active_collection = conn_collection or collection

    try:
        # ── 1. Ambiguity ──────────────────────────────────
        clarification = detect_ambiguity(
            question, s=active_schema, coll=active_collection,
        )
        if clarification:
            result.needs_clarification   = True
            result.clarification_request = clarification
            return result

        # ── 2. SQL generation ─────────────────────────────
        response, is_valid, val_msg = generate_structured_sql(
            question, s=active_schema, coll=active_collection,
        )

        result.sql                = response.sql_query
        result.explanation        = response.explanation
        result.tables_accessed    = response.tables_accessed
        result.columns_accessed   = [
            {"table": c.table, "column": c.column}
            for c in response.columns_accessed
        ]
        result.sql_valid          = is_valid
        result.validation_message = val_msg

        # ── 3. Guardrails + Risk Classification ──────────
        guardrail_result = guardrail.validate(response.sql_query, dsn=PG_DSN)

        safe_sql = guardrail_result.sql
        result.safe_sql                = safe_sql
        result.guardrail_allowed       = guardrail_result.allowed
        result.guardrail_warnings      = guardrail_result.violations
        result.guardrail_limit_applied = (safe_sql != response.sql_query)
        result.risk_level              = guardrail_result.risk_level.value
        result.risk_warning            = guardrail_result.risk_warning

        if not guardrail_result.allowed:
            # Hard block (deep subquery / expensive scan) — stop here
            logger.warning(
                "Query hard-blocked by guardrails: %s", guardrail_result.violations
            )
            return result

        # If risk > SAFE and user has not confirmed, return early so the
        # frontend can show the warning and ask for confirmation.
        if guardrail_result.risk_level != RiskLevel.SAFE and not confirmed:
            logger.info(
                "Query has risk_level=%s — awaiting user confirmation.",
                guardrail_result.risk_level.value,
            )
            return result

        # ── 4. Back-translation verification ─────────────
        verif = verifier.verify(question, safe_sql)
        result.back_translated_question = verif.back_translated_question
        result.alignment_score          = verif.alignment_score
        result.alignment_label          = verif.alignment_label
        result.alignment_flagged        = verif.is_flagged
        result.alignment_flag_reason    = verif.flag_reason
        result.judge_reason             = verif.judge_reason
        logger.info(
            "Back-translation alignment: %.0f%% [%s]",
            verif.alignment_score * 100, verif.alignment_label,
        )

        # ── 5. Sandbox execution ──────────────────────────
        sandbox_result = active_sandbox.execute(safe_sql)

        if sandbox_result.success:
            result.row_count         = sandbox_result.row_count
            result.execution_time_ms = sandbox_result.execution_time * 1000
            # Store the raw DataFrame directly for CLI / downstream consumers
            result.dataframe = sandbox_result.dataframe
            if (
                sandbox_result.dataframe is not None
                and not sandbox_result.dataframe.empty
            ):
                # Also keep a JSON-safe copy for the API (to_dict)
                result.execution_results = (
                    sandbox_result.dataframe
                    .where(sandbox_result.dataframe.notna(), other=None)
                    .to_dict(orient="records")
                )
            logger.info(
                "Sandbox execution OK: %d row(s), %d affected in %.2f ms",
                result.row_count, sandbox_result.rows_affected,
                result.execution_time_ms,
            )
            result.rows_affected = sandbox_result.rows_affected
        else:
            result.execution_error = sandbox_result.error
            logger.warning("Sandbox execution failed: %s", sandbox_result.error)

        # ── 6. Sanity check ───────────────────────────────
        sanity = sanity_checker.check(
            df=sandbox_result.dataframe,
            row_count=sandbox_result.row_count,
            sql=safe_sql,
        )
        result.sanity_anomalies  = [
            {
                "check":    a.check_name,
                "severity": a.severity,
                "message":  a.message,
                "column":   a.affected_column,
            }
            for a in sanity.anomalies
        ]
        result.sanity_pass_rate  = sanity.pass_rate
        result.sanity_summary    = sanity.summary
        logger.info(
            "Sanity: pass_rate=%.0f%% | anomalies=%d",
            sanity.pass_rate * 100, len(sanity.anomalies),
        )

        # ── 7. Confidence scoring ─────────────────────────
        conf = conf_scorer.score(
            syntax_valid=is_valid,
            alignment_score=verif.alignment_score,
            sanity_pass_rate=sanity.pass_rate,
            tables_accessed=response.tables_accessed,
        )
        result.confidence = conf.as_dict()
        logger.info(
            "Composite confidence: %.0f%% [%s]",
            conf.composite_score * 100, conf.grade,
        )

    except Exception as exc:
        logger.exception("Unexpected error in run_query: %s", exc)
        result.error = str(exc)

    return result