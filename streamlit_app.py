"""
streamlit_app.py
================
Streamlit UI for the Text-to-SQL pipeline.

Connects to the FastAPI backend at http://localhost:8000.

Features
--------
  • Natural-language question input
  • Generated SQL with Monaco syntax highlighting (editable)
  • Sortable, filterable results data table + CSV download
  • Composite confidence score with per-signal breakdown chart
  • History panel with click-to-reload

Run:
    streamlit run streamlit_app.py
"""

import time
from datetime import datetime

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── streamlit-ace (optional — graceful fallback to st.code) ──────────
try:
    from streamlit_ace import st_ace
    HAS_ACE = True
except ImportError:
    HAS_ACE = False

# =========================================================
# PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="Text-to-SQL Explorer",
    page_icon="🗄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =========================================================
# CUSTOM CSS  — dark premium theme
# =========================================================

st.markdown("""
<style>
/* ---------- global ---------- */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* ---------- main background ---------- */
.stApp {
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%);
    color: #e2e8f0;
}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);
    border-right: 1px solid #334155;
}
[data-testid="stSidebar"] * { color: #cbd5e1 !important; }

/* ---------- cards ---------- */
.card {
    background: rgba(30, 41, 59, 0.8);
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 16px;
    backdrop-filter: blur(12px);
}

/* ---------- header ---------- */
.hero-header {
    background: linear-gradient(90deg, #6366f1, #8b5cf6, #06b6d4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-size: 2.4rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    line-height: 1.1;
}
.hero-sub {
    color: #94a3b8;
    font-size: 0.95rem;
    margin-top: 4px;
}

/* ---------- confidence badge ---------- */
.grade-badge {
    display: inline-block;
    padding: 4px 14px;
    border-radius: 20px;
    font-weight: 700;
    font-size: 1.1rem;
    letter-spacing: 0.5px;
}
.grade-A { background: #166534; color: #86efac; }
.grade-B { background: #713f12; color: #fde68a; }
.grade-C { background: #7c2d12; color: #fdba74; }
.grade-D { background: #450a0a; color: #fca5a5; }

/* ---------- anomaly cards ---------- */
.anomaly-warning {
    background: rgba(120, 53, 15, 0.3);
    border-left: 3px solid #f59e0b;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 6px 0;
    font-size: 0.88rem;
}
.anomaly-error {
    background: rgba(127, 29, 29, 0.3);
    border-left: 3px solid #ef4444;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 6px 0;
    font-size: 0.88rem;
}

/* ---------- section labels ---------- */
.section-label {
    color: #94a3b8;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    margin-bottom: 8px;
}

/* ---------- metric overrides ---------- */
[data-testid="stMetricValue"] {
    font-size: 1.7rem !important;
    font-weight: 700 !important;
    color: #a5b4fc !important;
}
[data-testid="stMetricLabel"] {
    color: #64748b !important;
    font-size: 0.78rem !important;
    text-transform: uppercase;
    letter-spacing: 0.8px;
}

/* ---------- buttons ---------- */
.stButton > button {
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    padding: 0.5rem 1.4rem;
    transition: all 0.2s ease;
}
.stButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 20px rgba(99, 102, 241, 0.4);
}

/* ---------- input fields ---------- */
.stTextArea textarea, .stTextInput input {
    background: #1e293b !important;
    border: 1px solid #334155 !important;
    border-radius: 8px !important;
    color: #e2e8f0 !important;
    font-family: 'Inter', sans-serif !important;
}
.stTextArea textarea:focus, .stTextInput input:focus {
    border-color: #6366f1 !important;
    box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.2) !important;
}

/* ---------- expander ---------- */
[data-testid="stExpander"] {
    background: rgba(30, 41, 59, 0.6) !important;
    border: 1px solid #334155 !important;
    border-radius: 8px !important;
}

/* ---------- dataframe ---------- */
[data-testid="stDataFrame"] {
    border-radius: 10px !important;
    overflow: hidden !important;
}

/* ---------- history item ---------- */
.hist-item {
    padding: 8px 10px;
    border-radius: 8px;
    background: rgba(99, 102, 241, 0.08);
    border: 1px solid #334155;
    margin-bottom: 6px;
    cursor: pointer;
    transition: background 0.15s;
}
.hist-item:hover { background: rgba(99, 102, 241, 0.18); }
.hist-question { font-size: 0.85rem; color: #e2e8f0; font-weight: 500; }
.hist-meta { font-size: 0.72rem; color: #64748b; margin-top: 2px; }

/* ---------- divider ---------- */
hr { border-color: #334155 !important; }
</style>
""", unsafe_allow_html=True)

# =========================================================
# SESSION STATE DEFAULTS
# =========================================================

def _init_state():
    defaults = {
        "result":           None,
        "question":         "",
        "loaded_question":  None,
        "session_id":       "default",
        "api_base":         "http://localhost:8000",
        "history":          [],
        "schema":           None,
        "last_sql":         "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# =========================================================
# API HELPERS
# =========================================================

def _api(method: str, path: str, **kwargs):
    base = st.session_state.api_base.rstrip("/")
    try:
        r = httpx.request(method, f"{base}{path}", timeout=180, **kwargs)
        r.raise_for_status()
        return r.json(), None
    except httpx.ConnectError:
        return None, f"Cannot connect to API at `{base}`. Is the backend running?"
    except httpx.HTTPStatusError as e:
        return None, f"API error {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return None, str(e)


def api_query(question: str) -> tuple:
    return _api("POST", "/v1/query", json={
        "question":   question,
        "session_id": st.session_state.session_id,
    })


def api_history() -> tuple:
    return _api("GET", "/v1/history", params={
        "session_id": st.session_state.session_id,
        "limit":      50,
    })


def api_schema() -> tuple:
    return _api("GET", "/v1/schema")

# =========================================================
# COMPONENT RENDERERS
# =========================================================

def render_confidence(conf: dict):
    """Render the composite confidence banner + per-signal Plotly bar."""
    if not conf:
        return

    grade     = conf.get("grade", "?")
    composite = conf.get("composite_score", 0)
    label     = conf.get("grade_label", "")
    verdict   = conf.get("verdict", "")
    signals   = conf.get("signal_breakdown", {})
    weighted  = conf.get("weighted_breakdown", {})

    grade_class = f"grade-{grade}" if grade in "ABCD" else "grade-D"
    grade_icons = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}
    icon = grade_icons.get(grade, "⚪")

    # --- top row: score + grade badge ---
    c1, c2, c3 = st.columns([2, 2, 4])
    c1.metric("Composite Score", f"{composite:.0%}")
    c2.markdown(
        f"<div style='padding-top:18px'>"
        f"<span class='grade-badge {grade_class}'>{icon} Grade {grade}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    c3.markdown(
        f"<div style='padding-top:20px; color:#94a3b8; font-size:0.88rem'>{verdict}</div>",
        unsafe_allow_html=True,
    )

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    # --- plotly horizontal bar chart ---
    labels_map = {
        "syntax_validity":  "Syntax Validity",
        "back_translation": "Back-Translation",
        "sanity_pass_rate": "Sanity Checks",
        "schema_coverage":  "Schema Coverage",
    }
    max_pts_map = {
        "syntax_validity":  20,
        "back_translation": 35,
        "sanity_pass_rate": 30,
        "schema_coverage":  15,
    }

    keys   = list(labels_map.keys())
    labels = [labels_map[k] for k in keys]
    raw    = [signals.get(k, 0) for k in keys]
    maxes  = [max_pts_map[k] for k in keys]
    # Earned points = raw signal score × max points for that signal (int)
    earned = [round(raw[i] * maxes[i]) for i in range(len(keys))]
    text   = [f"{earned[i]}/{maxes[i]}" for i in range(len(keys))]
    colors = [
        "#22c55e" if r >= 0.70 else "#f59e0b" if r >= 0.40 else "#ef4444"
        for r in raw
    ]
    total_max = sum(maxes)  # 100

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=earned,
        y=labels,
        orientation="h",
        marker=dict(color=colors, line=dict(width=0)),
        text=text,
        textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="white", family="Inter", size=12),
        hovertemplate="<b>%{y}</b><br>Points: %{x:.1f}<extra></extra>",
    ))
    # background "empty" bars
    fig.add_trace(go.Bar(
        x=[maxes[i] - earned[i] for i in range(len(keys))],
        y=labels,
        orientation="h",
        marker=dict(color="rgba(51,65,85,0.5)", line=dict(width=0)),
        hoverinfo="skip",
        showlegend=False,
    ))
    fig.update_layout(
        barmode="stack",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter", color="#94a3b8"),
        height=180,
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=False,
        xaxis=dict(showgrid=False, showticklabels=False, zeroline=False, range=[0, max(maxes)]),
        yaxis=dict(showgrid=False, tickfont=dict(size=12, color="#cbd5e1")),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_sql_editor(sql: str) -> str:
    """Render the SQL editor. Returns the (possibly edited) SQL."""
    st.markdown("<p class='section-label'>Generated SQL</p>", unsafe_allow_html=True)
    if HAS_ACE:
        edited = st_ace(
            value=sql,
            language="sql",
            theme="tomorrow_night",
            key="sql_editor",
            height=220,
            auto_update=True,
            font_size=13,
            show_gutter=True,
            wrap=True,
        )
    else:
        st.code(sql, language="sql")
        edited = sql
    return edited


def render_results(result: dict):
    """Render execution results as a sortable DataFrame."""
    rows      = result.get("dataframe") or result.get("execution_results") or []
    row_count = result.get("row_count", 0)
    exec_ms   = result.get("execution_time_ms", 0)
    err       = result.get("execution_error")

    if err:
        st.error(f"❌ Execution error: {err}")
        return

    if not rows:
        st.info("ℹ️ Query returned 0 rows.")
        return

    df = pd.DataFrame(rows)
    st.caption(
        f"**{row_count}** row(s) returned in **{exec_ms:.1f} ms**"
        + (" *(display capped at 500)*" if row_count > 500 else "")
    )
    st.dataframe(df, use_container_width=True, height=380)

    # Download
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Download CSV",
        data=csv,
        file_name=f"query_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )


def render_sanity(result: dict):
    """Render sanity check anomalies."""
    anomalies   = result.get("sanity_anomalies", [])
    pass_rate   = result.get("sanity_pass_rate", 1.0)
    summary     = result.get("sanity_summary", "")

    if not anomalies:
        st.success(f"✅ {summary}")
        return

    with st.expander(f"🔬 Sanity Check Anomalies  —  {len(anomalies)} issue(s)  ({pass_rate:.0%} pass rate)", expanded=True):
        for a in anomalies:
            sev  = a.get("severity", "WARNING")
            cls  = "anomaly-error" if sev == "ERROR" else "anomaly-warning"
            icon = "❌" if sev == "ERROR" else "⚠️"
            col  = f" · Column: <code>{a['column']}</code>" if a.get("column") else ""
            st.markdown(
                f"<div class='{cls}'>"
                f"<strong>{icon} {a.get('check', '')}</strong>{col}<br>"
                f"<span style='color:#cbd5e1'>{a.get('message','')}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )


def render_guardrail_warnings(result: dict):
    warnings = result.get("guardrail_warnings", [])
    limit    = result.get("guardrail_limit_applied", False)
    if limit:
        st.info("🔒 Guardrail: LIMIT clause was automatically appended.")
    if warnings:
        for w in warnings:
            st.warning(f"⚠️ Guardrail: {w}")


def render_verification(result: dict):
    back_q  = result.get("back_translated_question", "")
    score   = result.get("alignment_score", 0)
    label   = result.get("alignment_label", "")
    flagged = result.get("alignment_flagged", False)
    reason  = result.get("judge_reason", "")

    col1, col2 = st.columns([3, 1])
    col1.markdown(
        f"<p class='section-label'>Back-Translated Question</p>"
        f"<p style='color:#e2e8f0; font-size:0.95rem; font-style:italic;'>"
        f"\"{back_q}\"</p>",
        unsafe_allow_html=True,
    )
    bar_filled = round(score * 10)
    bar_empty  = 10 - bar_filled
    bar_str    = "█" * bar_filled + "░" * bar_empty
    col2.markdown(
        f"<div style='text-align:right; padding-top:18px'>"
        f"<span style='font-size:1.2rem; font-weight:700; color:#a5b4fc'>{score:.0%}</span>"
        f"<span style='color:#475569; font-size:0.8rem'> [{label}]</span><br>"
        f"<code style='color:#6366f1; font-size:0.8rem'>{bar_str}</code>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if reason:
        st.caption(f"💬 {reason}")
    if flagged:
        st.warning("⚠️ LOW ALIGNMENT — SQL may not correctly answer the intended question.")


def render_history_sidebar(history: list):
    """Render the history panel in the sidebar."""
    if not history:
        st.markdown(
            "<p style='color:#475569; font-size:0.85rem; text-align:center; padding:12px 0'>"
            "No queries yet in this session.</p>",
            unsafe_allow_html=True,
        )
        return

    for i, entry in enumerate(history):
        q      = entry.get("question", "")[:52]
        grade  = entry.get("confidence", {}).get("grade", "?")
        rows   = entry.get("row_count", 0)
        ts     = entry.get("timestamp", "")[:16].replace("T", " ")
        grade_icons = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}
        g_icon = grade_icons.get(grade, "⚪")

        with st.expander(f"{g_icon} {q}{'…' if len(entry.get('question','')) > 52 else ''}", expanded=False):
            safe_sql = entry.get("safe_sql") or entry.get("sql", "")
            st.code(safe_sql[:300] + ("…" if len(safe_sql) > 300 else ""), language="sql")
            st.caption(f"Grade: **{grade}** · {rows} rows · {ts}")
            if st.button("↩ Load this question", key=f"load_{i}_{ts}"):
                st.session_state.loaded_question = entry.get("question", "")
                st.rerun()


# =========================================================
# SIDEBAR
# =========================================================

with st.sidebar:
    # Logo / title
    st.markdown(
        "<h2 style='color:#a5b4fc; font-weight:700; margin-bottom:0'>🗄️ Text-to-SQL</h2>"
        "<p style='color:#64748b; font-size:0.8rem; margin-top:2px'>Powered by Groq LLM</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    # Settings
    with st.expander("⚙️ Settings", expanded=False):
        st.session_state.api_base = st.text_input(
            "API Base URL",
            value=st.session_state.api_base,
            placeholder="http://localhost:8000",
        )
        st.session_state.session_id = st.text_input(
            "Session ID",
            value=st.session_state.session_id,
            placeholder="default",
        )

    # Schema explorer
    with st.expander("📐 Schema Explorer", expanded=False):
        schema_data, schema_err = api_schema()
        if schema_err:
            st.error(schema_err)
        elif schema_data:
            tables = schema_data.get("tables", {})
            st.caption(f"{len(tables)} tables")
            for tname, tinfo in list(tables.items())[:30]:
                cols = tinfo.get("columns", [])
                with st.expander(f"📋 {tname}", expanded=False):
                    for c in cols:
                        st.markdown(
                            f"<span style='color:#6366f1; font-family:monospace; font-size:0.82rem'>"
                            f"{c['name']}</span>"
                            f"<span style='color:#475569; font-size:0.78rem'> · {c['type']}</span>",
                            unsafe_allow_html=True,
                        )

    st.divider()

    # History
    st.markdown("<p class='section-label'>Query History</p>", unsafe_allow_html=True)
    hist_data, hist_err = api_history()
    if hist_err:
        st.caption(f"History unavailable: {hist_err}")
        history_entries = []
    else:
        history_entries = hist_data.get("queries", []) if hist_data else []

    render_history_sidebar(history_entries)

# =========================================================
# MAIN PANEL
# =========================================================

# Hero header
st.markdown(
    "<h1 class='hero-header'>Text-to-SQL Explorer</h1>"
    "<p class='hero-sub'>Ask questions in plain English — get instant SQL, results, and confidence signals.</p>",
    unsafe_allow_html=True,
)
st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

# ── Question Input ─────────────────────────────────────────────────────
st.markdown("<div class='card'>", unsafe_allow_html=True)
st.markdown("<p class='section-label'>Ask a Question</p>", unsafe_allow_html=True)

# Pre-fill from history click
if st.session_state.loaded_question:
    st.session_state.question = st.session_state.loaded_question
    st.session_state.loaded_question = None

question = st.text_area(
    label="question_input",
    label_visibility="collapsed",
    value=st.session_state.question,
    placeholder="e.g. How many students are enrolled in each department?",
    height=90,
    key="question_input",
)

col_btn1, col_btn2, col_spacer = st.columns([1.2, 1.2, 6])
run_btn   = col_btn1.button("▶ Run Query", type="primary", use_container_width=True)
clear_btn = col_btn2.button("🗑 Clear", use_container_width=True)

if clear_btn:
    st.session_state.result   = None
    st.session_state.question = ""
    st.session_state.last_sql = ""
    st.rerun()

st.markdown("</div>", unsafe_allow_html=True)

# ── Run pipeline ───────────────────────────────────────────────────────
if run_btn and question.strip():
    st.session_state.question = question.strip()
    with st.spinner("⏳ Running pipeline — generating SQL, executing, verifying…"):
        data, err = api_query(question.strip())

    if err:
        st.error(f"❌ {err}")
        st.session_state.result = None
    else:
        st.session_state.result   = data
        st.session_state.last_sql = data.get("safe_sql", "")

# ── Display results ────────────────────────────────────────────────────
result = st.session_state.result

if result:

    # ── Clarification needed ────────────────────────────────────────
    if result.get("needs_clarification"):
        cr   = result.get("clarification_request", {})
        msg  = cr.get("message", "Please clarify your question.")
        opts = cr.get("interpretations", [])

        st.warning(f"🤔 **Clarification Required**\n\n{msg}")
        if opts:
            choices = [o.get("example_query", o.get("label", "")) for o in opts]
            chosen  = st.radio("Choose an interpretation:", choices, index=0)
            if st.button("Submit clarification"):
                st.session_state.question = chosen
                st.rerun()
        st.stop()

    # ── Guardrail blocked ───────────────────────────────────────────
    if not result.get("guardrail_allowed", True):
        st.error("🚫 **Query Blocked by Guardrails**")
        for v in result.get("guardrail_warnings", []):
            st.markdown(f"- {v}")
        with st.expander("View raw SQL"):
            st.code(result.get("sql", ""), language="sql")
        st.stop()

    # ── Pipeline error ──────────────────────────────────────────────
    if result.get("error"):
        st.error(f"❌ Pipeline error: {result['error']}")
        st.stop()

    # ── Tabs layout ─────────────────────────────────────────────────
    tab_sql, tab_results, tab_confidence, tab_verify = st.tabs([
        "📝 SQL",
        "📊 Results",
        "🎯 Confidence",
        "🔁 Verification",
    ])

    # ─── TAB 1: SQL ─────────────────────────────────────────────────
    with tab_sql:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        render_guardrail_warnings(result)

        edited_sql = render_sql_editor(result.get("safe_sql", ""))
        st.session_state.last_sql = edited_sql

        # SQL validation status
        col_v1, col_v2 = st.columns([1, 5])
        if result.get("sql_valid"):
            col_v1.success("✅ Valid SQL")
        else:
            col_v1.error("❌ Invalid SQL")
        col_v2.caption(result.get("validation_message", ""))

        # Metadata
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.markdown("<p class='section-label'>Tables Accessed</p>", unsafe_allow_html=True)
            for t in result.get("tables_accessed", []):
                st.markdown(
                    f"<code style='background:#1e3a5f; color:#93c5fd; padding:2px 8px; border-radius:4px; font-size:0.82rem'>{t}</code> ",
                    unsafe_allow_html=True,
                )
        with col_m2:
            st.markdown("<p class='section-label'>Columns Accessed</p>", unsafe_allow_html=True)
            cols_str = ", ".join(
                f"{c['table']}.{c['column']}"
                for c in result.get("columns_accessed", [])
            )
            st.caption(cols_str or "—")

        st.markdown("</div>", unsafe_allow_html=True)

        # Explanation
        with st.expander("💬 Query Explanation", expanded=True):
            st.markdown(
                f"<p style='color:#cbd5e1; font-size:0.92rem; line-height:1.6'>"
                f"{result.get('explanation', '—')}</p>",
                unsafe_allow_html=True,
            )

    # ─── TAB 2: RESULTS ─────────────────────────────────────────────
    with tab_results:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        render_results(result)
        st.markdown("</div>", unsafe_allow_html=True)
        render_sanity(result)

    # ─── TAB 3: CONFIDENCE ──────────────────────────────────────────
    with tab_confidence:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        conf = result.get("confidence", {})
        if conf:
            render_confidence(conf)
        else:
            st.info("No confidence data available.")
        st.markdown("</div>", unsafe_allow_html=True)

        # Signal detail table
        if conf:
            bd = conf.get("signal_breakdown", {})
            wd = conf.get("weighted_breakdown", {})
            max_pts = {"syntax_validity": 20, "back_translation": 35,
                       "sanity_pass_rate": 30, "schema_coverage": 15}
            labels  = {"syntax_validity": "Syntax Validity",
                       "back_translation": "Back-Translation Alignment",
                       "sanity_pass_rate": "Sanity Check Pass Rate",
                       "schema_coverage": "Schema Coverage"}

            rows_conf = []
            for k, lbl in labels.items():
                raw = bd.get(k, 0)
                mx  = max_pts[k]
                # Earned points = raw signal score × max points
                rows_conf.append({
                    "Signal":      lbl,
                    "Raw Score":   f"{raw:.0%}",
                    "Weight":      f"{mx}%",
                    "Points":      f"{round(raw * mx)} / {mx}",
                    "Status":      "✅ Good" if raw >= 0.70 else "🟡 Fair" if raw >= 0.40 else "❌ Poor",
                })
            st.dataframe(
                pd.DataFrame(rows_conf),
                use_container_width=True,
                hide_index=True,
            )

    # ─── TAB 4: VERIFICATION ────────────────────────────────────────
    with tab_verify:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        st.markdown("<p class='section-label'>SQL-to-Question Back-Translation</p>", unsafe_allow_html=True)
        st.markdown(
            f"<p style='color:#94a3b8; font-size:0.82rem; margin-bottom:12px'>"
            f"The generated SQL was sent back to the LLM asking "
            f"<em>\"What question does this SQL answer?\"</em> — "
            f"the result is compared to your original question.</p>",
            unsafe_allow_html=True,
        )
        render_verification(result)
        st.markdown("</div>", unsafe_allow_html=True)

elif not run_btn:
    # Empty state
    st.markdown(
        "<div style='text-align:center; padding: 80px 0; color:#334155'>"
        "<div style='font-size:3.5rem'>🗄️</div>"
        "<h3 style='color:#475569; font-weight:500; margin-top:12px'>Ready to query</h3>"
        "<p style='color:#374151; font-size:0.9rem'>Type a question above and hit <strong>▶ Run Query</strong></p>"
        "</div>",
        unsafe_allow_html=True,
    )
