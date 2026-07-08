"""
verification/confidence_scorer.py
==================================
Unified confidence scoring for the Text-to-SQL pipeline.

Combines four independent quality signals into a single composite
score in [0.0, 1.0] and assigns a letter grade for quick at-a-glance
quality assessment.

Signal weights
--------------
  SQL syntax validity    20 %  – binary: valid or not
  Back-translation align 35 %  – VerificationResult.alignment_score
  Sanity check pass rate 30 %  – SanityReport.pass_rate
  Schema coverage        15 %  – fraction of tables_accessed in the schema

Letter grades
-------------
  A  ≥ 0.85   Excellent
  B  ≥ 0.70   Good
  C  ≥ 0.55   Fair — review before using
  D  < 0.55   Poor — significant concerns
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# =========================================================
# WEIGHTS  (must sum to 1.0)
# =========================================================

W_SYNTAX    = 0.20
W_ALIGN     = 0.35
W_SANITY    = 0.30
W_SCHEMA    = 0.15

assert abs(W_SYNTAX + W_ALIGN + W_SANITY + W_SCHEMA - 1.0) < 1e-9, \
    "Confidence signal weights must sum to 1.0"


# =========================================================
# GRADE THRESHOLDS
# =========================================================

_GRADES = [
    (0.85, "A", "Excellent"),
    (0.70, "B", "Good"),
    (0.55, "C", "Fair — review before using"),
    (0.00, "D", "Poor — significant concerns"),
]


# =========================================================
# REPORT DATA CLASS
# =========================================================

@dataclass
class ConfidenceReport:
    """
    Full breakdown of the composite confidence score.

    Attributes
    ----------
    composite_score   : Final weighted score in [0.0, 1.0].
    grade             : Letter grade: ``"A"``, ``"B"``, ``"C"``, or ``"D"``.
    grade_label       : Human-readable label for the grade.
    signal_breakdown  : Dict mapping signal name → raw score (before weighting).
    weighted_breakdown: Dict mapping signal name → weighted contribution.
    verdict           : One-line plain-English summary.
    """

    composite_score:    float
    grade:              str
    grade_label:        str
    signal_breakdown:   dict[str, float] = field(default_factory=dict)
    weighted_breakdown: dict[str, float] = field(default_factory=dict)
    verdict:            str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "composite_score":    round(self.composite_score, 4),
            "grade":              self.grade,
            "grade_label":        self.grade_label,
            "signal_breakdown":   {
                k: round(v, 4) for k, v in self.signal_breakdown.items()
            },
            "weighted_breakdown": {
                k: round(v, 4) for k, v in self.weighted_breakdown.items()
            },
            "verdict":            self.verdict,
        }


# =========================================================
# SCORER
# =========================================================

class ConfidenceScorer:
    """
    Aggregates quality signals into a single composite confidence score.

    Parameters
    ----------
    schema : dict
        The loaded schema dict (keys are table names).  Used to compute
        schema coverage for the tables referenced by the query.
    w_syntax : float
        Weight for SQL syntax validity signal (default 0.20).
    w_align : float
        Weight for back-translation alignment signal (default 0.35).
    w_sanity : float
        Weight for result sanity check pass rate (default 0.30).
    w_schema : float
        Weight for schema coverage signal (default 0.15).
    """

    def __init__(
        self,
        schema: dict,
        w_syntax: float = W_SYNTAX,
        w_align:  float = W_ALIGN,
        w_sanity: float = W_SANITY,
        w_schema: float = W_SCHEMA,
    ):
        self._schema   = schema
        self._w_syntax = w_syntax
        self._w_align  = w_align
        self._w_sanity = w_sanity
        self._w_schema = w_schema

    # ── Public API ────────────────────────────────────────────────

    def score(
        self,
        *,
        syntax_valid:    bool,
        alignment_score: float,
        sanity_pass_rate: float,
        tables_accessed: list[str],
    ) -> ConfidenceReport:
        """
        Compute the composite confidence score.

        Parameters
        ----------
        syntax_valid     : True when ``validate_sql_syntax`` passed.
        alignment_score  : VerificationResult.alignment_score (0–1).
        sanity_pass_rate : SanityReport.pass_rate (0–1).
        tables_accessed  : List of table names from StructuredSQLResponse.

        Returns
        -------
        ConfidenceReport
        """

        # ── Raw signal scores ─────────────────────────────────────
        s_syntax = 1.0 if syntax_valid else 0.0
        s_align  = float(max(0.0, min(1.0, alignment_score)))
        s_sanity = float(max(0.0, min(1.0, sanity_pass_rate)))
        s_schema = self._schema_coverage(tables_accessed)

        # ── Weighted contributions ────────────────────────────────
        w_syntax_contrib = self._w_syntax * s_syntax
        w_align_contrib  = self._w_align  * s_align
        w_sanity_contrib = self._w_sanity * s_sanity
        w_schema_contrib = self._w_schema * s_schema

        composite = (
            w_syntax_contrib
            + w_align_contrib
            + w_sanity_contrib
            + w_schema_contrib
        )
        composite = max(0.0, min(1.0, composite))

        # ── Grade ─────────────────────────────────────────────────
        grade, grade_label = _assign_grade(composite)

        # ── Verdict ───────────────────────────────────────────────
        verdict = _build_verdict(
            composite, grade,
            s_syntax, s_align, s_sanity, s_schema,
        )

        return ConfidenceReport(
            composite_score=composite,
            grade=grade,
            grade_label=grade_label,
            signal_breakdown={
                "syntax_validity":    round(s_syntax, 4),
                "back_translation":   round(s_align,  4),
                "sanity_pass_rate":   round(s_sanity, 4),
                "schema_coverage":    round(s_schema, 4),
            },
            weighted_breakdown={
                "syntax_validity":    round(w_syntax_contrib, 4),
                "back_translation":   round(w_align_contrib,  4),
                "sanity_pass_rate":   round(w_sanity_contrib, 4),
                "schema_coverage":    round(w_schema_contrib, 4),
            },
            verdict=verdict,
        )

    # ── Private helpers ───────────────────────────────────────────

    def _schema_coverage(self, tables_accessed: list[str]) -> float:
        """
        Fraction of tables referenced in the query that exist in the
        loaded schema.  Returns 1.0 when tables_accessed is empty
        (no penalty for single-table queries with no explicit table list).
        """
        if not tables_accessed:
            return 1.0
        known = sum(
            1 for t in tables_accessed
            if t in self._schema
        )
        return known / len(tables_accessed)


# =========================================================
# INTERNAL UTILITIES
# =========================================================

def _assign_grade(score: float) -> tuple[str, str]:
    """Map a numeric score to a (letter, label) grade tuple."""
    for threshold, letter, label in _GRADES:
        if score >= threshold:
            return letter, label
    return "D", "Poor — significant concerns"


def _build_verdict(
    composite:   float,
    grade:       str,
    s_syntax:    float,
    s_align:     float,
    s_sanity:    float,
    s_schema:    float,
) -> str:
    """
    Produce a one-line plain-English verdict that calls out the
    weakest signal when the grade is C or D.
    """
    if grade in ("A", "B"):
        return (
            f"Query looks correct ({composite:.0%} confidence). "
            "Results are likely trustworthy."
        )

    # Identify the worst-performing signal
    signals = {
        "SQL syntax":          s_syntax,
        "back-translation":    s_align,
        "sanity checks":       s_sanity,
        "schema coverage":     s_schema,
    }
    weakest_name, weakest_val = min(signals.items(), key=lambda x: x[1])

    return (
        f"Confidence is low ({composite:.0%}, grade {grade}). "
        f"Weakest signal: {weakest_name} ({weakest_val:.0%}). "
        "Review the query and results carefully before using."
    )
