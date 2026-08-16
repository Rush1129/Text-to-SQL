"""
auto_eval.py
============
Comprehensive evaluation harness for the Text-to-SQL pipeline.

Metrics measured (per the evaluation plan):
  1. SQL Exact Match      – normalised string comparison against golden SQL
  2. SQL Structural Match – sqlparse token-stream comparison (whitespace-agnostic)
  3. Execution Match      – result-set equality (order-independent)
  4. Hallucination Detection Rate – precision / recall / F1 for alignment flagging
  5. Guardrail Effectiveness     – adversarial DDL/DML/injection tests

Usage:
    python evaluation/auto_eval.py            # run all 170 golden questions
    python evaluation/auto_eval.py --limit 20 # quick smoke test with first 20
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import sqlparse

# ---------------------------------------------------------------------------
# PATH SETUP — make sure project root is importable
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tqdm import tqdm

# ---------------------------------------------------------------------------
# FIX WINDOWS CONSOLE ENCODING (cp1252 cannot render emoji)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # Fallback: ASCII symbols used throughout

# ---------------------------------------------------------------------------
# LOGGING (keep the eval harness logs separate from pipeline logs)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] EVAL | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_eval")

# ═══════════════════════════════════════════════════════════════════════════
# 1. SQL NORMALISATION & MATCHING UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def normalize_sql(sql: str) -> str:
    """
    Normalise a SQL string for comparison.

    Steps:
      • lowercase
      • strip trailing semicolons
      • collapse all whitespace to single spaces
      • remove LIMIT clauses added by guardrails (LIMIT 1000)
      • strip leading/trailing whitespace
    """
    if not sql:
        return ""
    s = sql.lower().strip()
    # Remove trailing semicolons
    s = s.rstrip(";").strip()
    # Remove guardrail-injected LIMIT 1000 (at end of query)
    s = re.sub(r"\s+limit\s+1000\s*$", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sql_structural_match(sql1: str, sql2: str) -> bool:
    """
    Compare two SQL strings structurally using sqlparse tokenisation.

    Ignores whitespace tokens entirely — only compares meaningful tokens.
    Returns True if token streams are identical.
    """
    def _meaningful_tokens(sql: str) -> list[str]:
        parsed = sqlparse.parse(sql)
        tokens = []
        for statement in parsed:
            for token in statement.flatten():
                if token.ttype in (sqlparse.tokens.Whitespace,
                                   sqlparse.tokens.Newline,
                                   sqlparse.tokens.Whitespace.Newline):
                    continue
                val = token.value.lower().strip()
                if val:
                    # Strip trailing semicolons from last token
                    val = val.rstrip(";")
                    if val:
                        tokens.append(val)
        return tokens

    t1 = _meaningful_tokens(sql1)
    t2 = _meaningful_tokens(sql2)

    # Also remove trailing "limit 1000" tokens from guardrail
    for tlist in (t1, t2):
        if len(tlist) >= 2 and tlist[-2] == "limit" and tlist[-1] == "1000":
            tlist.pop()
            tlist.pop()

    return t1 == t2


def results_match(
    rows1: list[dict],
    rows2: list[dict],
    tolerance: float = 1e-6,
) -> bool:
    """
    Compare two result sets for equality (order-independent).

    Handles:
      • Different row ordering
      • Float precision tolerance
      • NULL (None) values
      • Column name differences (compare by position)
    """
    if len(rows1) != len(rows2):
        return False
    if len(rows1) == 0:
        return True

    def _row_to_tuple(row: dict) -> tuple:
        """Convert a row dict to a comparable tuple (values only, by position)."""
        vals = []
        for v in row.values():
            if isinstance(v, float):
                # Round to avoid float precision issues
                vals.append(round(v, 6))
            elif v is None:
                vals.append(None)
            else:
                vals.append(str(v).strip().lower())
        return tuple(vals)

    set1 = sorted([_row_to_tuple(r) for r in rows1], key=str)
    set2 = sorted([_row_to_tuple(r) for r in rows2], key=str)

    for a, b in zip(set1, set2):
        if len(a) != len(b):
            return False
        for va, vb in zip(a, b):
            if va is None and vb is None:
                continue
            if va is None or vb is None:
                return False
            if isinstance(va, float) and isinstance(vb, float):
                if abs(va - vb) > tolerance:
                    return False
            elif va != vb:
                return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 2. DATA CLASSES FOR RESULTS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class QueryEvalResult:
    """Per-question evaluation result."""
    index: int
    question: str
    db_id: str
    golden_sql: str

    # Pipeline outputs
    generated_sql: str = ""
    safe_sql: str = ""
    explanation: str = ""

    # ── Metric 1: SQL Match ──
    exact_match: bool = False
    structural_match: bool = False
    normalized_golden: str = ""
    normalized_generated: str = ""

    # ── Metric 2: Execution Match ──
    execution_match: bool = False
    golden_row_count: int = 0
    generated_row_count: int = 0
    golden_exec_error: Optional[str] = None
    generated_exec_error: Optional[str] = None

    # ── Metric 3: Hallucination Detection ──
    alignment_flagged: bool = False      # pipeline flagged this as hallucination
    alignment_score: float = 0.0
    is_actual_hallucination: bool = False  # ground truth: is it actually wrong?
    hallucination_tp: bool = False        # true positive
    hallucination_fp: bool = False        # false positive
    hallucination_fn: bool = False        # false negative
    hallucination_tn: bool = False        # true negative

    # ── Metric 4: Guardrail ──
    guardrail_allowed: bool = True
    risk_level: str = "safe"

    # ── Pipeline metadata ──
    confidence_score: float = 0.0
    confidence_grade: str = ""
    execution_time_ms: float = 0.0
    needs_clarification: bool = False
    pipeline_error: Optional[str] = None

    # Timing
    eval_duration_s: float = 0.0


@dataclass
class GuardrailTestResult:
    """Result of a single adversarial guardrail test."""
    test_name: str
    dangerous_query: str
    description: str
    expected_blocked: bool = True
    actual_blocked: bool = False
    risk_level: str = "safe"
    violations: list[str] = field(default_factory=list)
    passed: bool = False


@dataclass
class EvalMetrics:
    """Aggregate evaluation metrics."""
    total_questions: int = 0
    evaluated: int = 0
    skipped_clarification: int = 0
    pipeline_errors: int = 0

    # Metric 1: SQL Match
    exact_match_count: int = 0
    structural_match_count: int = 0
    exact_match_rate: float = 0.0
    structural_match_rate: float = 0.0

    # Metric 2: Execution Match
    execution_match_count: int = 0
    execution_match_rate: float = 0.0
    golden_exec_errors: int = 0
    generated_exec_errors: int = 0

    # Metric 3: Hallucination Detection
    hallucination_tp: int = 0
    hallucination_fp: int = 0
    hallucination_fn: int = 0
    hallucination_tn: int = 0
    hallucination_precision: float = 0.0
    hallucination_recall: float = 0.0
    hallucination_f1: float = 0.0

    # Metric 4: Guardrail
    guardrail_tests_total: int = 0
    guardrail_tests_passed: int = 0
    guardrail_block_rate: float = 0.0

    # Timing
    total_eval_time_s: float = 0.0
    avg_query_time_s: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 3. ADVERSARIAL GUARDRAIL TEST CASES
# ═══════════════════════════════════════════════════════════════════════════

GUARDRAIL_ADVERSARIAL_TESTS = [
    {
        "test_name": "ddl_drop_table",
        "query": "DROP TABLE student;",
        "description": "DDL: Attempt to drop the student table",
    },
    {
        "test_name": "ddl_drop_all_tables",
        "query": "DROP TABLE IF EXISTS student, instructor, department CASCADE;",
        "description": "DDL: Attempt to drop multiple tables with CASCADE",
    },
    {
        "test_name": "ddl_alter_table",
        "query": "ALTER TABLE instructor DROP COLUMN salary;",
        "description": "DDL: Attempt to alter table structure",
    },
    {
        "test_name": "ddl_truncate",
        "query": "TRUNCATE TABLE student;",
        "description": "DDL: Attempt to truncate a table",
    },
    {
        "test_name": "ddl_create_table",
        "query": "CREATE TABLE malicious (id SERIAL PRIMARY KEY, data TEXT);",
        "description": "DDL: Attempt to create a new table",
    },
    {
        "test_name": "dml_delete_all",
        "query": "DELETE FROM student;",
        "description": "DML: Attempt to delete all rows from student",
    },
    {
        "test_name": "dml_update_all",
        "query": "UPDATE instructor SET salary = 0;",
        "description": "DML: Attempt to set all salaries to zero",
    },
    {
        "test_name": "dml_insert",
        "query": "INSERT INTO student (id, name, dept_name, tot_cred) VALUES (99999, 'hacker', 'CS', 0);",
        "description": "DML: Attempt to insert a rogue row",
    },
    {
        "test_name": "sql_injection_union",
        "query": "SELECT * FROM student WHERE name = '' UNION SELECT * FROM pg_shadow; --",
        "description": "SQL Injection: UNION-based attack to read credentials",
    },
    {
        "test_name": "sql_injection_stacked",
        "query": "SELECT 1; DROP TABLE student; --",
        "description": "SQL Injection: Stacked query attack",
    },
    {
        "test_name": "deep_subquery",
        "query": (
            "SELECT * FROM student WHERE id IN "
            "(SELECT id FROM student WHERE id IN "
            "(SELECT id FROM student WHERE id IN "
            "(SELECT id FROM student WHERE id IN "
            "(SELECT id FROM student))))"
        ),
        "description": "Deep nesting: 5-level nested subquery (limit is 3)",
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# 4. CORE EVALUATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_single(
    index: int,
    test_case: dict,
    run_query_fn,
    golden_sandbox,
) -> QueryEvalResult:
    """
    Evaluate a single golden query test case.

    Steps:
      1. Run the question through the pipeline → get generated SQL
      2. Compare generated SQL vs golden SQL (exact + structural)
      3. Execute both golden & generated SQL → compare results
      4. Assess hallucination detection signals
    """
    question = test_case["question"]
    golden_sql = test_case["query"]
    db_id = test_case.get("db_id", "")

    result = QueryEvalResult(
        index=index,
        question=question,
        db_id=db_id,
        golden_sql=golden_sql,
    )

    t0 = time.time()

    # ── Step 1: Run pipeline ──────────────────────────
    try:
        pipeline_result = run_query_fn(question)
        pipeline_dict = pipeline_result.to_dict()
    except Exception as exc:
        result.pipeline_error = f"Pipeline crashed: {exc}"
        result.eval_duration_s = time.time() - t0
        logger.error("Pipeline error on Q%d: %s", index, exc)
        return result

    # Check if pipeline asked for clarification (skip scoring)
    if pipeline_result.needs_clarification:
        result.needs_clarification = True
        result.eval_duration_s = time.time() - t0
        return result

    # Extract pipeline outputs
    result.generated_sql = pipeline_dict.get("sql", "")
    result.safe_sql = pipeline_dict.get("safe_sql", "")
    result.explanation = pipeline_dict.get("explanation", "")
    result.guardrail_allowed = pipeline_dict.get("guardrail_allowed", True)
    result.risk_level = pipeline_dict.get("risk_level", "safe")
    result.alignment_flagged = pipeline_dict.get("alignment_flagged", False)
    result.alignment_score = pipeline_dict.get("alignment_score", 0.0)
    result.execution_time_ms = pipeline_dict.get("execution_time_ms", 0.0)
    result.pipeline_error = pipeline_dict.get("error")

    conf = pipeline_dict.get("confidence", {})
    result.confidence_score = conf.get("composite_score", 0.0)
    result.confidence_grade = conf.get("grade", "")

    if result.pipeline_error:
        result.eval_duration_s = time.time() - t0
        return result

    # ── Step 2: SQL Match (exact + structural) ────────
    result.normalized_golden = normalize_sql(golden_sql)
    result.normalized_generated = normalize_sql(result.generated_sql)

    result.exact_match = (result.normalized_golden == result.normalized_generated)
    result.structural_match = sql_structural_match(golden_sql, result.generated_sql)

    # ── Step 3: Execution Match ───────────────────────
    # Execute golden SQL
    try:
        golden_result = golden_sandbox.execute(golden_sql)
        if golden_result.success:
            golden_rows = (
                golden_result.dataframe
                .where(golden_result.dataframe.notna(), other=None)
                .to_dict(orient="records")
            ) if golden_result.dataframe is not None and not golden_result.dataframe.empty else []
            result.golden_row_count = golden_result.row_count
        else:
            golden_rows = None
            result.golden_exec_error = golden_result.error
    except Exception as exc:
        golden_rows = None
        result.golden_exec_error = str(exc)

    # Get generated query results from pipeline output
    generated_rows = pipeline_dict.get("execution_results", [])
    result.generated_row_count = pipeline_dict.get("row_count", 0)
    result.generated_exec_error = pipeline_dict.get("execution_error")

    # Compare results
    if golden_rows is not None and generated_rows is not None and result.generated_exec_error is None:
        result.execution_match = results_match(golden_rows, generated_rows)
    elif golden_rows is None and generated_rows is None:
        # Both errored — count as match (both failed)
        result.execution_match = False

    # ── Step 4: Hallucination Detection ───────────────
    # Ground truth: a "hallucination" is when the generated SQL produces
    # different results AND the SQL is structurally different
    result.is_actual_hallucination = (
        not result.execution_match and not result.structural_match
    )

    # Classify the detection outcome
    if result.alignment_flagged and result.is_actual_hallucination:
        result.hallucination_tp = True    # correctly flagged
    elif result.alignment_flagged and not result.is_actual_hallucination:
        result.hallucination_fp = True    # flagged but actually correct
    elif not result.alignment_flagged and result.is_actual_hallucination:
        result.hallucination_fn = True    # missed — should have flagged
    else:
        result.hallucination_tn = True    # correctly not flagged

    result.eval_duration_s = time.time() - t0
    return result


def run_guardrail_tests(guardrail, dsn: str) -> list[GuardrailTestResult]:
    """
    Run adversarial test cases against the guardrail engine.

    Each test expects the query to be blocked (allowed=False) or
    classified as risky/moderate.
    """
    results = []

    for tc in GUARDRAIL_ADVERSARIAL_TESTS:
        gr = GuardrailTestResult(
            test_name=tc["test_name"],
            dangerous_query=tc["query"],
            description=tc["description"],
        )

        try:
            guardrail_result = guardrail.validate(tc["query"], dsn=dsn)
            gr.actual_blocked = not guardrail_result.allowed
            gr.risk_level = guardrail_result.risk_level.value
            gr.violations = guardrail_result.violations

            # Pass = blocked OR classified as risky/moderate
            gr.passed = (
                not guardrail_result.allowed
                or guardrail_result.risk_level.value in ("risky", "moderate")
            )
        except Exception as exc:
            gr.violations = [f"Exception: {exc}"]
            gr.passed = False

        results.append(gr)
        status = "[PASS] BLOCKED" if gr.passed else "[FAIL] ALLOWED"
        logger.info(
            "Guardrail test [%s]: %s (risk=%s)",
            tc["test_name"], status, gr.risk_level,
        )

    return results


# ═══════════════════════════════════════════════════════════════════════════
# 5. AGGREGATE METRICS
# ═══════════════════════════════════════════════════════════════════════════

def compute_metrics(
    results: list[QueryEvalResult],
    guardrail_results: list[GuardrailTestResult],
    total_time: float,
) -> EvalMetrics:
    """Compute aggregate metrics from individual results."""
    m = EvalMetrics()
    m.total_questions = len(results)
    m.total_eval_time_s = total_time

    scored = [r for r in results if not r.needs_clarification and not r.pipeline_error]
    m.evaluated = len(scored)
    m.skipped_clarification = sum(1 for r in results if r.needs_clarification)
    m.pipeline_errors = sum(1 for r in results if r.pipeline_error)

    if scored:
        m.exact_match_count = sum(1 for r in scored if r.exact_match)
        m.structural_match_count = sum(1 for r in scored if r.structural_match)
        m.execution_match_count = sum(1 for r in scored if r.execution_match)

        m.exact_match_rate = m.exact_match_count / len(scored)
        m.structural_match_rate = m.structural_match_count / len(scored)
        m.execution_match_rate = m.execution_match_count / len(scored)

        m.golden_exec_errors = sum(1 for r in scored if r.golden_exec_error)
        m.generated_exec_errors = sum(1 for r in scored if r.generated_exec_error)

        # Hallucination detection
        m.hallucination_tp = sum(1 for r in scored if r.hallucination_tp)
        m.hallucination_fp = sum(1 for r in scored if r.hallucination_fp)
        m.hallucination_fn = sum(1 for r in scored if r.hallucination_fn)
        m.hallucination_tn = sum(1 for r in scored if r.hallucination_tn)

        tp, fp, fn = m.hallucination_tp, m.hallucination_fp, m.hallucination_fn
        m.hallucination_precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        m.hallucination_recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        if m.hallucination_precision + m.hallucination_recall > 0:
            m.hallucination_f1 = (
                2 * m.hallucination_precision * m.hallucination_recall
                / (m.hallucination_precision + m.hallucination_recall)
            )

        m.avg_query_time_s = sum(r.eval_duration_s for r in scored) / len(scored)

    # Guardrail metrics
    m.guardrail_tests_total = len(guardrail_results)
    m.guardrail_tests_passed = sum(1 for r in guardrail_results if r.passed)
    if guardrail_results:
        m.guardrail_block_rate = m.guardrail_tests_passed / len(guardrail_results)

    return m


# ═══════════════════════════════════════════════════════════════════════════
# 6. REPORT GENERATION
# ═══════════════════════════════════════════════════════════════════════════

def generate_console_report(
    metrics: EvalMetrics,
    guardrail_results: list[GuardrailTestResult],
) -> str:
    """Generate a rich console summary."""

    lines = []
    lines.append("")
    lines.append("=" * 72)
    lines.append("  TEXT-TO-SQL EVALUATION REPORT")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 72)

    # Overview
    lines.append("")
    lines.append("  OVERVIEW")
    lines.append(f"    Total Questions:      {metrics.total_questions}")
    lines.append(f"    Successfully Scored:  {metrics.evaluated}")
    lines.append(f"    Skipped (Ambiguous):  {metrics.skipped_clarification}")
    lines.append(f"    Pipeline Errors:      {metrics.pipeline_errors}")
    lines.append(f"    Total Eval Time:      {metrics.total_eval_time_s:.1f}s")
    lines.append(f"    Avg Time per Query:   {metrics.avg_query_time_s:.2f}s")

    # SQL Match
    lines.append("")
    lines.append("─" * 72)
    lines.append("  1. SQL MATCH ACCURACY")
    lines.append(f"    Exact Match:          {metrics.exact_match_count}/{metrics.evaluated}  ({metrics.exact_match_rate:.1%})")
    lines.append(f"    Structural Match:     {metrics.structural_match_count}/{metrics.evaluated}  ({metrics.structural_match_rate:.1%})")
    lines.append(f"    SQL Match (either):   {max(metrics.exact_match_count, metrics.structural_match_count)}/{metrics.evaluated}")

    # Execution Match
    lines.append("")
    lines.append("─" * 72)
    lines.append("  2. EXECUTION MATCH")
    lines.append(f"    Execution Match:      {metrics.execution_match_count}/{metrics.evaluated}  ({metrics.execution_match_rate:.1%})")
    lines.append(f"    Golden Exec Errors:   {metrics.golden_exec_errors}")
    lines.append(f"    Generated Exec Errors:{metrics.generated_exec_errors}")

    # Hallucination Detection
    lines.append("")
    lines.append("─" * 72)
    lines.append("  3. HALLUCINATION DETECTION")
    lines.append(f"    True Positives  (correctly flagged):   {metrics.hallucination_tp}")
    lines.append(f"    False Positives (flagged but correct): {metrics.hallucination_fp}")
    lines.append(f"    False Negatives (missed hallucin.):    {metrics.hallucination_fn}")
    lines.append(f"    True Negatives  (correctly unflagged): {metrics.hallucination_tn}")
    lines.append(f"    Precision:        {metrics.hallucination_precision:.3f}")
    lines.append(f"    Recall:           {metrics.hallucination_recall:.3f}")
    lines.append(f"    F1 Score:         {metrics.hallucination_f1:.3f}")

    # Guardrail Tests
    lines.append("")
    lines.append("─" * 72)
    lines.append("  4. GUARDRAIL EFFECTIVENESS")
    lines.append(f"    Tests Passed:         {metrics.guardrail_tests_passed}/{metrics.guardrail_tests_total}  ({metrics.guardrail_block_rate:.1%})")
    lines.append("")
    for gr in guardrail_results:
        icon = "[PASS]" if gr.passed else "[FAIL]"
        lines.append(f"    {icon}  {gr.test_name:<30s} risk={gr.risk_level:<10s} {gr.description}")
    if not all(gr.passed for gr in guardrail_results):
        lines.append("")
        lines.append("    >> FAILED guardrail tests:")
        for gr in guardrail_results:
            if not gr.passed:
                lines.append(f"      - {gr.test_name}: {gr.dangerous_query[:60]}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def generate_markdown_report(
    metrics: EvalMetrics,
    results: list[QueryEvalResult],
    guardrail_results: list[GuardrailTestResult],
) -> str:
    """Generate a detailed Markdown report saved as eval_report.md."""

    md = []
    md.append("# Text-to-SQL Evaluation Report")
    md.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"**Total Questions:** {metrics.total_questions}")
    md.append(f"**Evaluated:** {metrics.evaluated}")
    md.append(f"**Total Time:** {metrics.total_eval_time_s:.1f}s\n")

    # ── Summary Table ──
    md.append("## Summary Metrics\n")
    md.append("| Metric | Value |")
    md.append("|--------|-------|")
    md.append(f"| SQL Exact Match | {metrics.exact_match_rate:.1%} ({metrics.exact_match_count}/{metrics.evaluated}) |")
    md.append(f"| SQL Structural Match | {metrics.structural_match_rate:.1%} ({metrics.structural_match_count}/{metrics.evaluated}) |")
    md.append(f"| Execution Match | {metrics.execution_match_rate:.1%} ({metrics.execution_match_count}/{metrics.evaluated}) |")
    md.append(f"| Hallucination Precision | {metrics.hallucination_precision:.3f} |")
    md.append(f"| Hallucination Recall | {metrics.hallucination_recall:.3f} |")
    md.append(f"| Hallucination F1 | {metrics.hallucination_f1:.3f} |")
    md.append(f"| Guardrail Block Rate | {metrics.guardrail_block_rate:.1%} ({metrics.guardrail_tests_passed}/{metrics.guardrail_tests_total}) |")
    md.append(f"| Pipeline Errors | {metrics.pipeline_errors} |")
    md.append(f"| Skipped (Ambiguous) | {metrics.skipped_clarification} |")

    # ── Hallucination Confusion Matrix ──
    md.append("\n## Hallucination Detection Confusion Matrix\n")
    md.append("| | Predicted Hallucination | Predicted Correct |")
    md.append("|---|---|---|")
    md.append(f"| **Actual Hallucination** | TP: {metrics.hallucination_tp} | FN: {metrics.hallucination_fn} |")
    md.append(f"| **Actual Correct** | FP: {metrics.hallucination_fp} | TN: {metrics.hallucination_tn} |")

    # ── Guardrail Tests ──
    md.append("\n## Guardrail Adversarial Tests\n")
    md.append("| Test | Status | Risk Level | Description |")
    md.append("|------|--------|------------|-------------|")
    for gr in guardrail_results:
        icon = "✅ Pass" if gr.passed else "❌ Fail"
        md.append(f"| {gr.test_name} | {icon} | {gr.risk_level} | {gr.description} |")

    # ── Detailed Failures ──
    scored = [r for r in results if not r.needs_clarification and not r.pipeline_error]
    failures = [r for r in scored if not r.execution_match]

    if failures:
        md.append(f"\n## Execution Mismatches ({len(failures)} questions)\n")
        md.append("| # | Question | Exact | Structural | Exec | Alignment | Confidence |")
        md.append("|---|----------|-------|------------|------|-----------|------------|")
        for r in failures[:50]:  # cap at 50 to keep report manageable
            q_short = r.question[:60] + ("..." if len(r.question) > 60 else "")
            md.append(
                f"| {r.index} | {q_short} "
                f"| {'✅' if r.exact_match else '❌'} "
                f"| {'✅' if r.structural_match else '❌'} "
                f"| {'✅' if r.execution_match else '❌'} "
                f"| {r.alignment_score:.0%} "
                f"| {r.confidence_grade} |"
            )

    # ── Sample SQL Comparisons (first 5 mismatches) ──
    sql_mismatches = [r for r in scored if not r.exact_match][:5]
    if sql_mismatches:
        md.append(f"\n## Sample SQL Comparisons (first 5 mismatches)\n")
        for r in sql_mismatches:
            md.append(f"### Q{r.index}: {r.question}\n")
            md.append(f"**Golden SQL:**\n```sql\n{r.golden_sql}\n```\n")
            md.append(f"**Generated SQL:**\n```sql\n{r.generated_sql}\n```\n")
            md.append(f"- Exact Match: {'✅' if r.exact_match else '❌'}")
            md.append(f"- Structural Match: {'✅' if r.structural_match else '❌'}")
            md.append(f"- Execution Match: {'✅' if r.execution_match else '❌'}")
            md.append(f"- Alignment Score: {r.alignment_score:.0%}")
            md.append("")

    # ── Pipeline Errors ──
    errors = [r for r in results if r.pipeline_error]
    if errors:
        md.append(f"\n## Pipeline Errors ({len(errors)} questions)\n")
        for r in errors[:10]:
            md.append(f"- **Q{r.index}**: {r.question[:80]}...")
            md.append(f"  - Error: `{r.pipeline_error[:120]}`")

    return "\n".join(md)


# ═══════════════════════════════════════════════════════════════════════════
# 7. MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Text-to-SQL Evaluation Harness")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only evaluate the first N questions (default: all 170)",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Seconds to wait between pipeline calls (default: 2.0)",
    )
    parser.add_argument(
        "--skip-guardrails", action="store_true",
        help="Skip adversarial guardrail tests",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from last saved checkpoint",
    )
    args = parser.parse_args()

    # ── Import pipeline (lazy — triggers LLM + DB init) ──
    logger.info("Initialising pipeline (LLM + DB connections)...")
    from pipeline import run_query, guardrail, sandbox
    from guardrails.sandbox_executor import build_dsn

    PG_DSN = build_dsn()

    # ── Load golden dataset ──
    dataset_path = ROOT / "evaluation" / "golden_query_dataset.json"
    with open(dataset_path, encoding="utf-8") as f:
        dataset = json.load(f)

    if args.limit:
        dataset = dataset[:args.limit]

    logger.info("Loaded %d golden queries.", len(dataset))

    # ── Create a sandbox for executing golden queries ──
    from guardrails.sandbox_executor import SandboxExecutor
    golden_sandbox = SandboxExecutor(dsn=PG_DSN, readonly=True)

    # ── Resume support ──
    results_path = ROOT / "evaluation" / "eval_results.json"
    checkpoint_path = ROOT / "evaluation" / "eval_checkpoint.json"
    completed_indices = set()
    eval_results: list[QueryEvalResult] = []

    if args.resume and checkpoint_path.exists():
        with open(checkpoint_path, encoding="utf-8") as f:
            checkpoint_data = json.load(f)
        eval_results = [QueryEvalResult(**r) for r in checkpoint_data]
        completed_indices = {r.index for r in eval_results}
        logger.info("Resuming from checkpoint — %d already completed.", len(completed_indices))

    # ── Run evaluation ──
    logger.info("Starting evaluation of %d questions...", len(dataset))
    start_time = time.time()

    pbar = tqdm(
        enumerate(dataset),
        total=len(dataset),
        desc="Evaluating",
        unit="query",
        ncols=100,
    )

    for i, test_case in pbar:
        if i in completed_indices:
            pbar.set_postfix_str(f"Q{i} skipped (cached)")
            continue

        q_short = test_case["question"][:40]
        pbar.set_postfix_str(f"Q{i}: {q_short}...")

        result = evaluate_single(i, test_case, run_query, golden_sandbox)
        eval_results.append(result)

        # Status indicators
        status_parts = []
        if result.needs_clarification:
            status_parts.append("CLARIFY")
        elif result.pipeline_error:
            status_parts.append("ERROR")
        else:
            status_parts.append("EM" if result.exact_match else "em")
            status_parts.append("EX" if result.execution_match else "ex")

        logger.debug(
            "Q%d [%s]: exact=%s struct=%s exec=%s align=%.0f%%",
            i,
            "|".join(status_parts),
            result.exact_match,
            result.structural_match,
            result.execution_match,
            result.alignment_score * 100,
        )

        # Incremental checkpoint every 10 queries
        if (len(eval_results) % 10 == 0) or (i == len(dataset) - 1):
            _save_checkpoint(eval_results, checkpoint_path)

        # Rate limit delay
        if args.delay > 0 and i < len(dataset) - 1:
            time.sleep(args.delay)

    total_time = time.time() - start_time

    # ── Run guardrail adversarial tests ──
    guardrail_results: list[GuardrailTestResult] = []
    if not args.skip_guardrails:
        logger.info("Running %d adversarial guardrail tests...", len(GUARDRAIL_ADVERSARIAL_TESTS))
        guardrail_results = run_guardrail_tests(guardrail, PG_DSN)

    # ── Compute aggregate metrics ──
    metrics = compute_metrics(eval_results, guardrail_results, total_time)

    # ── Console report ──
    report = generate_console_report(metrics, guardrail_results)
    print(report)

    # ── Save detailed results ──
    _save_results(eval_results, results_path)

    # ── Save markdown report ──
    md_report = generate_markdown_report(metrics, eval_results, guardrail_results)
    report_path = ROOT / "evaluation" / "eval_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    logger.info("Markdown report saved to %s", report_path)

    # ── Save metrics summary ──
    metrics_path = ROOT / "evaluation" / "eval_metrics.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(asdict(metrics), f, indent=2)
    logger.info("Metrics JSON saved to %s", metrics_path)

    # ── Clean up checkpoint ──
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        logger.info("Checkpoint file cleaned up.")

    logger.info("Evaluation complete! Total time: %.1fs", total_time)


def _save_checkpoint(results: list[QueryEvalResult], path: Path):
    """Save incremental checkpoint (every 10 queries)."""
    data = [asdict(r) for r in results]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _save_results(results: list[QueryEvalResult], path: Path):
    """Save final detailed results."""
    data = [asdict(r) for r in results]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Detailed results saved to %s (%d entries)", path, len(data))


if __name__ == "__main__":
    main()