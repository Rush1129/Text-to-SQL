"""
verification/verifier.py
========================
SQL-to-Question back-translation verification.

After SQL is generated, this module:
  1. Sends the SQL back to the LLM asking what question it answers
     (back-translation).
  2. Asks the LLM to score how semantically similar the back-translated
     question is to the original user question (LLM-as-judge).
  3. Returns a VerificationResult with the score, label, and a flag if
     alignment is below the configured threshold.
"""

from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# =========================================================
# ALIGNMENT THRESHOLDS
# =========================================================

LABEL_HIGH   = "HIGH"
LABEL_MEDIUM = "MEDIUM"
LABEL_LOW    = "LOW"

DEFAULT_FLAG_THRESHOLD = 0.65   # flag when score < this value
HIGH_THRESHOLD         = 0.80   # score >= 0.80  → HIGH
MEDIUM_THRESHOLD       = 0.60   # score >= 0.60  → MEDIUM
                                 # score <  0.60  → LOW


# =========================================================
# RESULT DATA CLASS
# =========================================================

@dataclass
class VerificationResult:
    """
    Holds the outcome of one SQL-to-question back-translation check.

    Attributes
    ----------
    back_translated_question : str
        The question the LLM believes the SQL query answers.
    alignment_score : float
        Semantic alignment between the original and back-translated
        question, normalised to [0.0, 1.0].
    alignment_label : str
        Human-readable tier: "HIGH", "MEDIUM", or "LOW".
    is_flagged : bool
        True when alignment_score is below the configured threshold.
    flag_reason : str | None
        Plain-English explanation of why the query was flagged, or
        None when not flagged.
    judge_reason : str | None
        The LLM judge's own rationale for the score it gave.
    """

    back_translated_question: str
    alignment_score: float
    alignment_label: str
    is_flagged: bool
    flag_reason: str | None
    judge_reason: str | None = None

    # ── Convenience helpers ───────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        return {
            "back_translated_question": self.back_translated_question,
            "alignment_score":          round(self.alignment_score, 4),
            "alignment_label":          self.alignment_label,
            "is_flagged":               self.is_flagged,
            "flag_reason":              self.flag_reason,
            "judge_reason":             self.judge_reason,
        }

    def __repr__(self) -> str:
        return (
            f"VerificationResult("
            f"score={self.alignment_score:.0%}, "
            f"label={self.alignment_label}, "
            f"flagged={self.is_flagged})"
        )


# =========================================================
# PROMPTS
# =========================================================

_BACK_TRANSLATE_PROMPT = """\
You are given a SQL query. Your task is to describe, in one concise sentence, \
the exact business question this SQL query is designed to answer.

Rules:
- Write exactly one sentence.
- Do NOT explain how the query works technically.
- Focus on the *data question* a non-technical analyst would ask.
- Do not add any preamble, bullet points, or JSON.

SQL Query:
{sql}

Question answered by this SQL:"""


_JUDGE_PROMPT = """\
You are a semantic similarity judge evaluating a Text-to-SQL system.

Original Question  : {original}
Back-Translated Q  : {back_translated}

Score how well the Back-Translated Question captures the intent of the \
Original Question on a scale of 0 to 10, where:
  10 = identical intent and scope
   7 = same core intent, minor wording difference
   5 = partially overlapping intent
   3 = related topic but different intent
   0 = completely unrelated

Critical rule — IGNORE the following when scoring:
- Any mention of row limits, result size, or "top N" / "first N" rows.
  (These are added automatically by the system and do not reflect the
  user's original intent.)
- Minor wording differences such as "count" vs "number of", or
  "retrieve" vs "show".

Return ONLY valid JSON with this exact shape (no markdown, no extra keys):
{{
  "score": <integer 0-10>,
  "reason": "<one sentence explaining the score>"
}}"""


# =========================================================
# MAIN VERIFIER CLASS
# =========================================================

class SQLVerifier:
    """
    Verifies that a generated SQL query actually answers the user's
    original question by back-translating the SQL into a question and
    scoring the semantic alignment.

    Parameters
    ----------
    llm : Any
        A LangChain-compatible chat LLM instance (e.g. ChatGroq).
        Must support `.invoke(prompt: str) -> AIMessage`.
    flag_threshold : float
        Queries with alignment_score < flag_threshold will have
        is_flagged=True in their VerificationResult. Default 0.65.
    """

    def __init__(self, llm: Any, flag_threshold: float = DEFAULT_FLAG_THRESHOLD):
        self._llm = llm
        self._threshold = max(0.0, min(1.0, flag_threshold))

    # ── Public API ────────────────────────────────────────────────

    def verify(
        self,
        original_question: str,
        sql: str,
    ) -> VerificationResult:
        """
        Run the full back-translation + alignment-scoring pipeline.

        Parameters
        ----------
        original_question : str
            The natural-language question the user asked.
        sql : str
            The generated (and guardrail-validated) SQL query.

        Returns
        -------
        VerificationResult
        """

        # Step 1: Back-translate SQL → question
        back_translated = self.back_translate(sql)

        # Step 2: Score alignment
        score, judge_reason = self.score_alignment(
            original_question,
            back_translated,
        )

        # Step 3: Derive label and flag
        label = self._label(score)
        flagged = score < self._threshold
        flag_reason = self._flag_reason(
            score, original_question, back_translated
        ) if flagged else None

        return VerificationResult(
            back_translated_question=back_translated,
            alignment_score=score,
            alignment_label=label,
            is_flagged=flagged,
            flag_reason=flag_reason,
            judge_reason=judge_reason,
        )

    def back_translate(self, sql: str) -> str:
        """
        Ask the LLM: "What question does this SQL query answer?"

        Any guardrail- or sandbox-injected LIMIT clause is stripped
        before sending so the LLM describes the full query intent
        rather than "the first N rows of …".

        Parameters
        ----------
        sql : str
            The SQL query to back-translate (may contain a guardrail
            LIMIT appended after generation).

        Returns
        -------
        str
            The LLM's one-sentence description of the question.
        """

        clean_sql = _strip_limit(sql)
        prompt = _BACK_TRANSLATE_PROMPT.format(sql=clean_sql)

        try:
            response = self._llm.invoke(prompt)
            text = _extract_text(response)
            return text.strip()
        except Exception as exc:
            logger.warning("Back-translation failed: %s", exc)
            return "(back-translation unavailable)"

    def score_alignment(
        self,
        original: str,
        back_translated: str,
    ) -> tuple[float, str | None]:
        """
        Ask the LLM to score semantic alignment between two questions.

        Returns
        -------
        (score, reason)
            score   : float in [0.0, 1.0]
            reason  : the judge's plain-English rationale
        """

        prompt = _JUDGE_PROMPT.format(
            original=original,
            back_translated=back_translated,
        )

        try:
            response = self._llm.invoke(prompt)
            text = _extract_text(response)
            parsed = _parse_judge_json(text)
            raw_score = int(parsed["score"])
            # Clamp to [0, 10] then normalise
            raw_score = max(0, min(10, raw_score))
            normalised = raw_score / 10.0
            reason = str(parsed.get("reason", "")).strip()
            return normalised, reason or None

        except Exception as exc:
            logger.warning("Alignment scoring failed: %s", exc)
            # Graceful degradation: return neutral score, no reason
            return 0.5, None

    # ── Private helpers ───────────────────────────────────────────

    def _label(self, score: float) -> str:
        if score >= HIGH_THRESHOLD:
            return LABEL_HIGH
        if score >= MEDIUM_THRESHOLD:
            return LABEL_MEDIUM
        return LABEL_LOW

    def _flag_reason(
        self,
        score: float,
        original: str,
        back_translated: str,
    ) -> str:
        pct = f"{score:.0%}"
        return (
            f"Alignment is only {pct} (below threshold "
            f"{self._threshold:.0%}). The SQL may not correctly answer "
            f"the intended question. "
            f"Original: \"{original}\" — "
            f"SQL answers: \"{back_translated}\""
        )


# =========================================================
# INTERNAL UTILITIES
# =========================================================

# Matches a trailing LIMIT clause (with optional OFFSET) at the very
# end of a SQL string, case-insensitively.  Examples stripped:
#   LIMIT 1000
#   LIMIT 500 OFFSET 0
#   limit\n100
_LIMIT_RE = re.compile(
    r"\bLIMIT\s+\d+(?:\s+OFFSET\s+\d+)?\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _strip_limit(sql: str) -> str:
    """
    Remove a trailing LIMIT (and optional OFFSET) clause that may have
    been appended by the guardrail or sandbox layer.  The rest of the
    query is left completely unchanged.

    Parameters
    ----------
    sql : str
        The SQL string to clean.

    Returns
    -------
    str
        The SQL with the trailing LIMIT clause removed, or the
        original string if no such clause was found.
    """
    return _LIMIT_RE.sub("", sql).rstrip().rstrip(";")

def _extract_text(response: Any) -> str:
    """
    Pull a plain string out of whatever the LLM returns.
    Works for LangChain AIMessage objects or bare strings.
    """
    if hasattr(response, "content"):
        return response.content
    return str(response)


def _parse_judge_json(text: str) -> dict:
    """
    Extract and parse the first JSON object found in `text`.
    Handles responses wrapped in markdown code fences.
    """

    # Strip markdown fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)

    # Find first {...} block
    match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in judge response: {text!r}")

    return json.loads(match.group())
