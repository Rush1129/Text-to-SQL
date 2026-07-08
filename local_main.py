"""
main.py
=======
CLI entry point for the Text-to-SQL pipeline.

All shared logic (LLM, guardrails, sandbox, verification, schema) lives in
pipeline.py.  This file is purely responsible for user-facing display.

Usage:
    python main.py
"""

import json
from pipeline import run_query, schema

# =========================================================
# DISPLAY HELPERS
# =========================================================

_W = 55  # console width constant


def _bar(score: float, total: int = 10) -> str:
    filled = round(score * total)
    return "█" * filled + "░" * (total - filled)


def _print_confidence_banner(conf: dict, sanity_anomaly_count: int) -> None:
    grade_icons = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}
    icon = grade_icons.get(conf["grade"], "⚪")

    print("\n" + "╔" + "═" * (_W - 2) + "╗")
    title = (
        f"  {icon} COMPOSITE CONFIDENCE: "
        f"{conf['composite_score']:.0%}  "
        f"[{conf['grade']}]  {conf['grade_label']}"
    )
    print(f"║{title:<{_W - 2}}║")
    print("╠" + "═" * (_W - 2) + "╣")

    bd = conf["signal_breakdown"]

    # Syntax
    syn = bd["syntax_validity"]
    syn_str = "✅ 20/20" if syn == 1.0 else "❌  0/20"
    print(f"║{'  Syntax validity      ' + syn_str:<{_W - 2}}║")

    # Back-translation
    aln = bd["back_translation"]
    aln_wgt = round(aln * 35)
    aln_icon = "✅" if aln >= 0.65 else ("🟡" if aln >= 0.40 else "❌")
    print(f"║{'  Back-translation  ' + aln_icon + f' {aln_wgt}/35  ({aln:.0%} align.)':<{_W - 2}}║")

    # Sanity
    san = bd["sanity_pass_rate"]
    san_wgt = round(san * 30)
    san_icon = "✅" if san == 1.0 else ("🟡" if san >= 0.70 else "❌")
    san_desc = "all passed" if san == 1.0 else f"{sanity_anomaly_count} issue(s)"
    print(f"║{'  Sanity checks     ' + san_icon + f' {san_wgt}/30  ({san_desc})':<{_W - 2}}║")

    # Schema coverage
    sch = bd["schema_coverage"]
    sch_wgt = round(sch * 15)
    sch_icon = "✅" if sch == 1.0 else ("🟡" if sch >= 0.70 else "❌")
    print(f"║{'  Schema coverage   ' + sch_icon + f' {sch_wgt}/15  ({sch:.0%})':<{_W - 2}}║")

    print("╠" + "═" * (_W - 2) + "╣")
    verdict_line = f"  {conf['verdict'][:_W - 5]}"
    print(f"║{verdict_line:<{_W - 2}}║")
    print("╚" + "═" * (_W - 2) + "╝")


def _print_query_result(result) -> None:
    """Pretty-print a QueryResult to the console."""

    # ── Clarification needed ────────────────────────────
    if result.needs_clarification:
        print("\nClarification Required:\n")
        print(json.dumps(result.clarification_request, indent=2))
        return

    # ── Pipeline error ──────────────────────────────────
    if result.error:
        print(f"\n❌ Pipeline error: {result.error}")
        return

    # ── Guardrail blocked ───────────────────────────────
    if not result.guardrail_allowed:
        print("\n" + "=" * _W)
        print("  🚫 QUERY BLOCKED BY GUARDRAILS")
        print("=" * _W)
        print(f"\n📝 Original Query:\n{result.sql}")
        print("\n⛔ Violations:")
        for v in result.guardrail_warnings:
            print(f"   • {v}")
        print("\n" + "=" * _W)
        return

    # ── Back-translation verification ───────────────────
    print("\n" + "-" * _W)
    print("  🔁 SQL-TO-QUESTION VERIFICATION")
    print("-" * _W)
    print(f"\n❓ Original Question:\n   {result.question}")
    print(f"\n🔄 Back-Translated Question:\n   {result.back_translated_question}")

    bar_str = _bar(result.alignment_score)
    print(
        f"\n📊 Alignment Score: {result.alignment_score:.0%}  "
        f"[{result.alignment_label}]  |{bar_str}|"
    )
    if result.judge_reason:
        print(f"   💬 Judge: {result.judge_reason}")

    if result.alignment_flagged:
        print("\n   ⚠️  LOW ALIGNMENT — SQL may not answer the intended question.")
        if result.alignment_flag_reason:
            short = result.alignment_flag_reason[:120] + (
                "..." if len(result.alignment_flag_reason) > 120 else ""
            )
            print(f"   📌 {short}")
    else:
        print("\n   ✅ Alignment is acceptable.")
    print("-" * _W)

    # ── SQL response ─────────────────────────────────────
    print("\n" + "=" * _W)
    print("  STRUCTURED SQL RESPONSE")
    print("=" * _W)
    print(f"\n📝 SQL Query:\n{result.safe_sql}")

    if result.guardrail_limit_applied:
        print("\n🔒 Guardrail: LIMIT clause was automatically appended.")

    print(f"\n💬 Explanation:\n{result.explanation}")
    print(f"\n🎯 LLM Confidence Score: {result.confidence.get('composite_score', 0):.0%}")

    print("\n📊 Tables Accessed:")
    for table in result.tables_accessed:
        print(f"   • {table}")

    print("\n📋 Columns Accessed:")
    for col in result.columns_accessed:
        print(f"   • {col['table']}.{col['column']}")

    # ── SQL Validation ───────────────────────────────────
    print("\n🔍 SQL Validation:")
    if result.sql_valid:
        print(f"   ✅ {result.validation_message}")
    else:
        print(f"   ❌ {result.validation_message}")

    # ── Sandbox Execution ────────────────────────────────
    print("\n" + "-" * _W)
    print("  🔒 SANDBOX EXECUTION")
    print("-" * _W)

    if result.execution_error:
        print(f"\n❌ Sandbox blocked execution: {result.execution_error}")
    else:
        print(
            f"\n✅ Query executed in {result.execution_time_ms:.2f} ms "
            f"— {result.row_count} row(s) returned"
        )

        if result.execution_results:
            rows     = result.execution_results
            columns  = list(rows[0].keys()) if rows else []
            display  = rows[:20]

            col_widths = [
                max(len(c), max((len(str(r.get(c, ""))) for r in display), default=0))
                for c in columns
            ]
            header    = " | ".join(f"{c:<{w}}" for c, w in zip(columns, col_widths))
            separator = "-" * len(header)
            print(f"\n   {header}")
            print(f"   {separator}")
            for row in display:
                row_str = " | ".join(
                    f"{str(row.get(c, '')):<{w}}"
                    for c, w in zip(columns, col_widths)
                )
                print(f"   {row_str}")

            if result.row_count > len(display):
                remaining = result.row_count - len(display)
                print(f"\n   ... and {remaining} more row(s)")

        print("\n📁 Execution audit written to: guardrails/execution_audit.log")

    # ── Composite Confidence Banner ──────────────────────
    _print_confidence_banner(result.confidence, len(result.sanity_anomalies))

    # ── Sanity Anomalies ─────────────────────────────────
    if result.sanity_anomalies:
        print("\n" + "-" * _W)
        print("  🔬 SANITY CHECK ANOMALIES")
        print("-" * _W)
        icons = {"ERROR": "❌", "WARNING": "⚠️ "}
        for a in result.sanity_anomalies:
            icon = icons.get(a["severity"], "•")
            print(f"\n  {icon} [{a['severity']}] {a['check']}")
            print(f"     {a['message']}")
            if a.get("column"):
                print(f"     Column: {a['column']}")
        print("-" * _W)

    print("\n" + "=" * _W)


# =========================================================
# MAIN LOOP
# =========================================================

if __name__ == "__main__":
    while True:
        try:
            user_question = input("\nEnter your question (or type exit): ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if user_question.strip().lower() == "exit":
            break

        if not user_question.strip():
            continue

        result = run_query(user_question)
        _print_query_result(result)
