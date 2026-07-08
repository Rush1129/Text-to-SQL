"""
verification/sanity_checker.py
==============================
Post-execution result sanity checking.

Inspects the pandas DataFrame returned by the sandbox executor and
emits typed SanityAnomaly objects for every data-quality issue detected.
No LLM calls — purely statistical / structural analysis.

Checks performed
----------------
1.  empty_result       – 0 rows returned
2.  null_heavy_column  – any column > NULL_PCT_THRESHOLD % NULL
3.  negative_aggregate – numeric aggregate column has a negative value
4.  overflow_aggregate – numeric aggregate column exceeds OVERFLOW_LIMIT
5.  zero_count         – a column whose name looks like a count = 0
6.  date_out_of_range  – date/datetime column outside plausible span
7.  single_group       – GROUP BY result has only 1 row (when > 1 expected)
8.  uniform_column     – non-trivial column where every value is identical
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd


# =========================================================
# THRESHOLDS  (all configurable via ResultSanityChecker.__init__)
# =========================================================

NULL_PCT_THRESHOLD = 0.40   # flag column if >40 % NULL
OVERFLOW_LIMIT     = 1e9    # flag aggregate if > 1 billion
DATE_MIN_YEAR      = 1900
DATE_MAX_YEAR      = 2100
# Minimum rows before "single group" check fires
MIN_ROWS_FOR_GROUP_CHECK = 3

# Patterns that suggest a column holds a COUNT / aggregate
_COUNT_COL_RE  = re.compile(
    r"\b(count|total|num|n_|cnt|qty|quantity)\b",
    re.IGNORECASE,
)
_AGG_COL_RE    = re.compile(
    r"\b(sum|avg|average|mean|total|amount|revenue|salary|score|"
    r"gpa|grade|points|fee|cost|price)\b",
    re.IGNORECASE,
)
_DATE_DTYPE_RE = re.compile(r"datetime|date|timestamp", re.IGNORECASE)


# =========================================================
# ANOMALY MODEL
# =========================================================

@dataclass
class SanityAnomaly:
    """
    One detected data-quality issue in the query result.

    Attributes
    ----------
    check_name       : Internal identifier for the check (snake_case).
    severity         : ``"WARNING"`` or ``"ERROR"``.
    message          : Human-readable explanation of the anomaly.
    affected_column  : Column name involved, or None for row-level checks.
    """

    check_name:      str
    severity:        str          # "WARNING" | "ERROR"
    message:         str
    affected_column: Optional[str] = None

    def icon(self) -> str:
        return "❌" if self.severity == "ERROR" else "⚠️ "


# =========================================================
# SANITY REPORT
# =========================================================

@dataclass
class SanityReport:
    """
    Aggregated result of all sanity checks on a query's DataFrame.

    Attributes
    ----------
    anomalies   : List of every issue found (may be empty).
    total_checks: Number of individual checks that were attempted.
    pass_rate   : Fraction of checks that passed, in [0.0, 1.0].
                  Used as the sanity signal in the confidence scorer.
    has_errors  : True when at least one ERROR-severity anomaly exists.
    summary     : One-line human-readable verdict.
    """

    anomalies:    list[SanityAnomaly] = field(default_factory=list)
    total_checks: int   = 0
    pass_rate:    float = 1.0
    has_errors:   bool  = False
    summary:      str   = "No checks performed."

    # ── Convenience ──────────────────────────────────────────────

    def passed_checks(self) -> int:
        return self.total_checks - len(self.anomalies)

    def as_dict(self) -> dict:
        return {
            "pass_rate":    round(self.pass_rate, 4),
            "total_checks": self.total_checks,
            "anomalies": [
                {
                    "check":   a.check_name,
                    "severity": a.severity,
                    "message":  a.message,
                    "column":   a.affected_column,
                }
                for a in self.anomalies
            ],
        }


# =========================================================
# CHECKER
# =========================================================

class ResultSanityChecker:
    """
    Runs a battery of data-quality checks on a sandbox result DataFrame.

    Parameters
    ----------
    null_pct_threshold : float
        Fraction of NULLs in a column above which it is flagged as
        NULL-heavy (default 0.40 = 40 %).
    overflow_limit : float
        Numeric aggregate values above this are flagged as implausibly
        large (default 1e9).
    date_min_year : int
        Dates before this year are considered out of range (default 1900).
    date_max_year : int
        Dates after this year are considered out of range (default 2100).
    """

    def __init__(
        self,
        null_pct_threshold: float = NULL_PCT_THRESHOLD,
        overflow_limit:     float = OVERFLOW_LIMIT,
        date_min_year:      int   = DATE_MIN_YEAR,
        date_max_year:      int   = DATE_MAX_YEAR,
    ):
        self._null_pct   = null_pct_threshold
        self._overflow   = overflow_limit
        self._date_min   = date_min_year
        self._date_max   = date_max_year

    # ── Public API ────────────────────────────────────────────────

    def check(
        self,
        df: Optional[Any],          # pandas.DataFrame or None
        row_count: int = 0,
        sql: str = "",
    ) -> SanityReport:
        """
        Run all sanity checks on *df* and return a SanityReport.

        Parameters
        ----------
        df        : The DataFrame from SandboxResult.dataframe.
        row_count : SandboxResult.row_count (actual rows before cap).
        sql       : The executed SQL string (used for heuristics).
        """

        anomalies: list[SanityAnomaly] = []
        attempted = 0

        # ── 1. Empty result ───────────────────────────────────────
        attempted += 1
        if df is None or df.empty:
            anomalies.append(SanityAnomaly(
                check_name="empty_result",
                severity="WARNING",
                message=(
                    "Query returned 0 rows — possible bad JOIN, "
                    "overly strict WHERE filter, or no matching data."
                ),
            ))
            # No DataFrame to analyse further
            return self._build_report(anomalies, attempted)

        # ── 2. NULL-heavy columns ─────────────────────────────────
        for col in df.columns:
            attempted += 1
            null_pct = df[col].isna().mean()
            if null_pct > self._null_pct:
                anomalies.append(SanityAnomaly(
                    check_name="null_heavy_column",
                    severity="WARNING",
                    message=(
                        f"Column '{col}' is {null_pct:.0%} NULL — "
                        "likely caused by a bad JOIN or missing data "
                        "on one side of the relationship."
                    ),
                    affected_column=col,
                ))

        # ── 3. Aggregate range checks ─────────────────────────────
        for col in df.columns:
            if not _is_numeric(df[col]):
                continue

            numeric_vals = df[col].dropna()
            if numeric_vals.empty:
                continue

            # 3a. Count = 0
            if _COUNT_COL_RE.search(col):
                attempted += 1
                if (numeric_vals == 0).all():
                    anomalies.append(SanityAnomaly(
                        check_name="zero_count",
                        severity="WARNING",
                        message=(
                            f"Count column '{col}' = 0 for all rows — "
                            "the WHERE filter may be too restrictive or "
                            "the JOIN condition may match nothing."
                        ),
                        affected_column=col,
                    ))

            # 3b. Negative aggregate
            if _AGG_COL_RE.search(col):
                attempted += 1
                min_val = numeric_vals.min()
                if min_val < 0:
                    anomalies.append(SanityAnomaly(
                        check_name="negative_aggregate",
                        severity="WARNING",
                        message=(
                            f"Aggregate column '{col}' contains a "
                            f"negative value ({min_val:,.2f}) — "
                            "check for data entry errors or incorrect "
                            "arithmetic in the query."
                        ),
                        affected_column=col,
                    ))

            # 3c. Overflow / implausibly large
            if _AGG_COL_RE.search(col):
                attempted += 1
                max_val = numeric_vals.max()
                if max_val > self._overflow:
                    anomalies.append(SanityAnomaly(
                        check_name="overflow_aggregate",
                        severity="WARNING",
                        message=(
                            f"Aggregate column '{col}' has a very large "
                            f"value ({max_val:,.0f}) — possible cartesian "
                            "product or unit mismatch (e.g. cents vs dollars)."
                        ),
                        affected_column=col,
                    ))

        # ── 4. Date range checks ──────────────────────────────────
        for col in df.columns:
            if not _is_date_like(df[col]):
                continue
            attempted += 1
            try:
                parsed = pd.to_datetime(df[col], errors="coerce")
                valid  = parsed.dropna()
                if valid.empty:
                    continue
                min_yr = valid.dt.year.min()
                max_yr = valid.dt.year.max()
                if min_yr < self._date_min:
                    anomalies.append(SanityAnomaly(
                        check_name="date_out_of_range",
                        severity="WARNING",
                        message=(
                            f"Date column '{col}' contains the year "
                            f"{min_yr}, which is before "
                            f"{self._date_min} — check for epoch-zero "
                            "defaults or data corruption."
                        ),
                        affected_column=col,
                    ))
                if max_yr > self._date_max:
                    anomalies.append(SanityAnomaly(
                        check_name="date_out_of_range",
                        severity="WARNING",
                        message=(
                            f"Date column '{col}' contains the year "
                            f"{max_yr}, which is after "
                            f"{self._date_max} — possible placeholder "
                            "or sentinel date value."
                        ),
                        affected_column=col,
                    ))
            except Exception:
                pass  # non-fatal; skip column

        # ── 5. Single-group result ────────────────────────────────
        attempted += 1
        if (
            len(df) == 1
            and row_count == 1
            and _looks_like_group_by(sql)
            and len(df.columns) >= 2
        ):
            anomalies.append(SanityAnomaly(
                check_name="single_group",
                severity="WARNING",
                message=(
                    "GROUP BY query returned only 1 group — the grouping "
                    "column may be too coarse, or the filter may be "
                    "collapsing all rows into one bucket."
                ),
            ))

        # ── 6. Suspicious column uniformity ──────────────────────
        if len(df) >= MIN_ROWS_FOR_GROUP_CHECK:
            for col in df.columns:
                attempted += 1
                if df[col].nunique(dropna=True) == 1 and len(df) > 1:
                    # Skip if the column is obviously an aggregate label
                    if _COUNT_COL_RE.search(col) or _AGG_COL_RE.search(col):
                        continue
                    anomalies.append(SanityAnomaly(
                        check_name="uniform_column",
                        severity="WARNING",
                        message=(
                            f"Column '{col}' has only one distinct value "
                            f"across all {len(df)} rows — possible cartesian "
                            "product, missing GROUP BY dimension, or "
                            "constant literal in SELECT."
                        ),
                        affected_column=col,
                    ))

        return self._build_report(anomalies, attempted)

    # ── Private helpers ───────────────────────────────────────────

    def _build_report(
        self,
        anomalies: list[SanityAnomaly],
        total: int,
    ) -> SanityReport:
        n_issues  = len(anomalies)
        passed    = max(0, total - n_issues)
        pass_rate = passed / total if total > 0 else 1.0
        has_err   = any(a.severity == "ERROR" for a in anomalies)

        if n_issues == 0:
            summary = f"All {total} sanity checks passed ✅"
        else:
            summary = (
                f"{n_issues} issue(s) detected out of "
                f"{total} check(s) — review anomalies below."
            )

        return SanityReport(
            anomalies=anomalies,
            total_checks=total,
            pass_rate=pass_rate,
            has_errors=has_err,
            summary=summary,
        )


# =========================================================
# COLUMN TYPE HELPERS
# =========================================================

def _is_numeric(series: pd.Series) -> bool:
    """Return True when a Series holds numeric data."""
    return pd.api.types.is_numeric_dtype(series)


def _is_date_like(series: pd.Series) -> bool:
    """
    Return True when a Series is a datetime dtype OR when its name
    contains common date-related keywords.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if series.name and _DATE_DTYPE_RE.search(str(series.name)):
        return True
    return False


def _looks_like_group_by(sql: str) -> bool:
    """Heuristic: does the SQL contain a GROUP BY clause?"""
    return bool(re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE))
