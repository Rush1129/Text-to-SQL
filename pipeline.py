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
from models import StructuredSQLResponse, validate_sql_syntax
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

logger.info("All pipeline components ready.")

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

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON serialisation."""
        return {
            "question":                 self.question,
            "sql":                      self.sql,
            "safe_sql":                 self.safe_sql,
            "explanation":              self.explanation,
            "tables_accessed":          self.tables_accessed,
            "columns_accessed":         self.columns_accessed,
            "execution_results":        self.execution_results,
            "row_count":                self.row_count,
            "execution_time_ms":        round(self.execution_time_ms, 3),
            "execution_error":          self.execution_error,
            "sql_valid":                self.sql_valid,
            "validation_message":       self.validation_message,
            "guardrail_allowed":        self.guardrail_allowed,
            "guardrail_warnings":       self.guardrail_warnings,
            "guardrail_limit_applied":  self.guardrail_limit_applied,
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
        }


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def get_relevant_tables(question: str, top_k: int = 3) -> list[str]:
    results = collection.query(query_texts=[question], n_results=top_k)
    return [m["table_name"] for m in results["metadatas"][0]]


def format_schema(relevant_tables: list[str]) -> str:
    out = ""
    for table in relevant_tables:
        info = schema[table]
        out += f"\nTable: {table}\nColumns:\n"
        for col in info["columns"]:
            out += f"- {col['name']} ({col['type']})\n"
        if info["primary_keys"]:
            out += "Primary Keys:\n"
            for pk in info["primary_keys"]:
                out += f"- {pk}\n"
    return out


def format_relationships(relevant_tables: list[str]) -> str:
    out = "\nRelationships:\n"
    for table in relevant_tables:
        for fk in schema[table]["foreign_keys"]:
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


def build_ambiguity_prompt(user_question: str) -> str:
    all_tables       = list(schema.keys())
    schema_text      = format_schema(all_tables)
    relationship_text = format_relationships(all_tables)
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
1. Mark ambiguous only when two or more materially different SQL queries
   are reasonable for the same question.
2. Do not mark broad but clear requests as ambiguous.
3. Do not guess business definitions.
4. List each plausible interpretation with an example clarified query.
5. Use only concepts supported by the schema and examples.

Database Schema:
{schema_text}

{relationship_text}

{example_text}

User Question:
{user_question}
"""


def detect_ambiguity(user_question: str) -> Optional[dict]:
    logger.debug("Running ambiguity check for: %r", user_question)
    prompt = build_ambiguity_prompt(user_question)
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


def build_sql_prompt(user_question: str) -> str:
    relevant_tables   = get_relevant_tables(user_question)
    schema_text       = format_schema(relevant_tables)
    relationship_text = format_relationships(relevant_tables)
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
) -> tuple[StructuredSQLResponse, bool, str]:
    """Generate SQL, return (response, is_valid, validation_msg)."""
    prompt_text = build_sql_prompt(user_question)
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

def run_query(question: str) -> QueryResult:
    """
    Run the complete Text-to-SQL pipeline for *question*.

    Steps
    -----
    1. Ambiguity detection
    2. SQL generation + syntax validation
    3. Guardrail check
    4. Back-translation verification
    5. Sandbox execution
    6. Result sanity check
    7. Confidence scoring

    Returns a QueryResult dataclass.  On unexpected errors the
    ``error`` field is set and all other fields have safe defaults.
    """

    logger.info("run_query: %r", question)

    result = QueryResult(question=question, sql="", safe_sql="", explanation="")

    try:
        # ── 1. Ambiguity ──────────────────────────────────
        clarification = detect_ambiguity(question)
        if clarification:
            result.needs_clarification   = True
            result.clarification_request = clarification
            return result

        # ── 2. SQL generation ─────────────────────────────
        response, is_valid, val_msg = generate_structured_sql(question)

        result.sql                = response.sql_query
        result.explanation        = response.explanation
        result.tables_accessed    = response.tables_accessed
        result.columns_accessed   = [
            {"table": c.table, "column": c.column}
            for c in response.columns_accessed
        ]
        result.sql_valid          = is_valid
        result.validation_message = val_msg

        # ── 3. Guardrails ─────────────────────────────────
        guardrail_result = guardrail.validate(response.sql_query, dsn=PG_DSN)

        if not guardrail_result.allowed:
            logger.warning(
                "Query blocked by guardrails: %s", guardrail_result.violations
            )
            result.guardrail_allowed  = False
            result.guardrail_warnings = guardrail_result.violations
            result.safe_sql           = response.sql_query
            return result

        safe_sql                        = guardrail_result.sql
        result.safe_sql                 = safe_sql
        result.guardrail_warnings       = guardrail_result.violations
        result.guardrail_limit_applied  = (safe_sql != response.sql_query)

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
        sandbox_result = sandbox.execute(safe_sql)

        if sandbox_result.success:
            result.row_count         = sandbox_result.row_count
            result.execution_time_ms = sandbox_result.execution_time * 1000
            if (
                sandbox_result.dataframe is not None
                and not sandbox_result.dataframe.empty
            ):
                # Serialise rows as list-of-dicts; replace NaN with None
                result.execution_results = (
                    sandbox_result.dataframe
                    .where(sandbox_result.dataframe.notna(), other=None)
                    .to_dict(orient="records")
                )
            logger.info(
                "Sandbox execution OK: %d row(s) in %.2f ms",
                result.row_count, result.execution_time_ms,
            )
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
