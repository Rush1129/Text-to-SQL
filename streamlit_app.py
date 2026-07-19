"""
streamlit_app.py
================
Streamlit UI for the Text-to-SQL pipeline — Conversational Chat Interface.

Features
--------
  • Chat-based conversational interface with context-aware SQL generation
  • Editable SQL with AI-powered validation (syntax + schema correctness)
  • Role-gated query execution with clear status indicators
  • Multi-page layout: Chat, Results & Analysis, Audit Log
  • Conversation history sent to LLM for follow-up queries

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

/* ---------- risk banners ---------- */
.risk-banner {
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 18px;
    backdrop-filter: blur(12px);
    border: 1px solid;
}
.risk-safe {
    background: rgba(20, 83, 45, 0.35);
    border-color: #22c55e;
}
.risk-moderate {
    background: rgba(120, 53, 15, 0.35);
    border-color: #f59e0b;
}
.risk-risky {
    background: rgba(127, 29, 29, 0.40);
    border-color: #ef4444;
}
.risk-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 14px;
    border-radius: 20px;
    font-weight: 700;
    font-size: 0.85rem;
    letter-spacing: 0.4px;
    margin-bottom: 10px;
}
.risk-badge-safe     { background: rgba(34,197,94,0.18);  color: #86efac; border: 1px solid #22c55e; }
.risk-badge-moderate { background: rgba(245,158,11,0.18); color: #fde68a; border: 1px solid #f59e0b; }
.risk-badge-risky    { background: rgba(239,68,68,0.18);  color: #fca5a5; border: 1px solid #ef4444; }
.risk-warning-text {
    color: #e2e8f0;
    font-size: 0.93rem;
    line-height: 1.65;
    margin: 8px 0 14px 0;
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

/* ---------- divider ---------- */
hr { border-color: #334155 !important; }

/* ---------- chat-specific ---------- */
.executed-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(34, 197, 94, 0.12);
    border: 1px solid rgba(34, 197, 94, 0.4);
    border-radius: 8px;
    padding: 10px 18px;
    color: #86efac;
    font-size: 0.88rem;
    font-weight: 500;
    margin: 8px 0;
    width: 100%;
}

.validation-pass {
    background: rgba(34, 197, 94, 0.10);
    border: 1px solid rgba(34, 197, 94, 0.35);
    border-radius: 8px;
    padding: 12px 16px;
    margin: 8px 0;
    color: #86efac;
}
.validation-fail {
    background: rgba(239, 68, 68, 0.10);
    border: 1px solid rgba(239, 68, 68, 0.35);
    border-radius: 8px;
    padding: 12px 16px;
    margin: 8px 0;
    color: #fca5a5;
}

.nav-btn-active {
    background: linear-gradient(135deg, #6366f1, #8b5cf6) !important;
    color: white !important;
    font-weight: 600 !important;
}

/* ---------- page back link ---------- */
.back-link {
    color: #94a3b8;
    font-size: 0.85rem;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin-bottom: 12px;
    cursor: pointer;
}
.back-link:hover { color: #a5b4fc; }
</style>
""", unsafe_allow_html=True)

# =========================================================
# SESSION STATE DEFAULTS
# =========================================================

def _init_state():
    defaults = {
        # Auth
        "token":                None,
        "user_id":              None,
        "user_email":           None,
        "user_role":            "viewer",
        "logged_in":            False,
        # Settings
        "session_id":           "default",
        "api_base":             "http://localhost:8000",
        # Connections
        "connections":          [],
        "active_connection_id": None,
        "active_connection_name": None,
        # Chat
        "messages":             [],        # Conversation messages
        "current_page":         "chat",    # "chat" | "details" | "audit"
        "detail_result":        None,      # Result shown on details page
        # Editing
        "editing_msg_idx":      None,      # Index of msg being edited
        "edit_sql":             "",        # SQL in editor
        "validation_result":    None,      # AI validation result
        # Schema
        "schema":               None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# =========================================================
# API HELPERS
# =========================================================

def _auth_headers() -> dict:
    """Return JWT auth headers for API requests."""
    if st.session_state.token:
        return {"Authorization": f"Bearer {st.session_state.token}"}
    return {}


def _api(method: str, path: str, **kwargs):
    base = st.session_state.api_base.rstrip("/")
    headers = kwargs.pop("headers", {})
    headers.update(_auth_headers())
    try:
        r = httpx.request(
            method, f"{base}{path}",
            timeout=180, headers=headers, **kwargs,
        )
        r.raise_for_status()
        return r.json(), None
    except httpx.ConnectError:
        return None, f"Cannot connect to API at `{base}`. Is the backend running?"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            st.session_state.token = None
            st.session_state.logged_in = False
            return None, "Session expired. Please log in again."
        return None, f"API error {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return None, str(e)


def api_signup(email: str, password: str) -> tuple:
    return _api("POST", "/v1/auth/signup", json={
        "email": email, "password": password,
    })

def api_login(email: str, password: str) -> tuple:
    return _api("POST", "/v1/auth/login", json={
        "email": email, "password": password,
    })

def api_query(question: str, confirmed: bool = False,
              conversation_history: list | None = None) -> tuple:
    payload = {
        "question":   question,
        "session_id": st.session_state.session_id,
        "confirmed":  confirmed,
    }
    if st.session_state.active_connection_id:
        payload["connection_id"] = st.session_state.active_connection_id
    if conversation_history:
        payload["conversation_history"] = conversation_history
    return _api("POST", "/v1/query", json=payload)

def api_validate_sql(sql: str) -> tuple:
    payload = {"sql": sql}
    if st.session_state.active_connection_id:
        payload["connection_id"] = st.session_state.active_connection_id
    return _api("POST", "/v1/validate-sql", json=payload)

def api_execute_sql(sql: str, question: str = "",
                    confirmed: bool = False) -> tuple:
    payload = {
        "sql":        sql,
        "question":   question,
        "session_id": st.session_state.session_id,
        "confirmed":  confirmed,
    }
    if st.session_state.active_connection_id:
        payload["connection_id"] = st.session_state.active_connection_id
    return _api("POST", "/v1/execute-sql", json=payload)

def api_history() -> tuple:
    return _api("GET", "/v1/history", params={
        "session_id": st.session_state.session_id,
        "limit":      50,
    })

def api_schema() -> tuple:
    params = {}
    if st.session_state.active_connection_id:
        params["connection_id"] = st.session_state.active_connection_id
    return _api("GET", "/v1/schema", params=params)

def api_audit(limit: int = 50, offset: int = 0) -> tuple:
    return _api("GET", "/v1/audit", params={"limit": limit, "offset": offset})

def api_connect_db(name, host, port, db, user, password) -> tuple:
    return _api("POST", "/v1/connections", json={
        "connection_name": name,
        "host": host, "port": port,
        "database_name": db,
        "username": user, "password": password,
    })

def api_list_connections() -> tuple:
    return _api("GET", "/v1/connections")

def api_delete_connection(conn_id: str) -> tuple:
    return _api("DELETE", f"/v1/connections/{conn_id}")


# =========================================================
# HELPER: BUILD CONVERSATION HISTORY FOR API
# =========================================================

def _build_conv_history() -> list[dict]:
    """Extract last 10 conversation turns for context-aware generation."""
    history = []
    for msg in st.session_state.messages[-20:]:  # last 20 messages ≈ 10 turns
        if msg["role"] == "user":
            history.append({
                "role":    "user",
                "content": msg["content"],
            })
        elif msg["role"] == "assistant" and not msg.get("error"):
            history.append({
                "role":        "assistant",
                "content":     msg.get("content", ""),
                "sql":         msg.get("sql", ""),
                "explanation": msg.get("content", ""),
            })
    return history


# =========================================================
# HELPER FUNCTIONS (defined after pages to avoid forward refs)
# =========================================================

def _render_edit_mode(idx: int, msg: dict):
    """Render inline SQL editor with AI validation for a chat message."""
    sql = msg.get("sql", "")

    st.markdown(
        "<p class='section-label'>✏️ Edit SQL</p>",
        unsafe_allow_html=True,
    )

    edited_sql = st.text_area(
        "Edit SQL",
        value=st.session_state.edit_sql or sql,
        height=200,
        key=f"sql_editor_{idx}",
        label_visibility="collapsed",
    )
    st.session_state.edit_sql = edited_sql

    col_v, col_e, col_c, _ = st.columns([1.3, 1.3, 0.8, 3])

    # Validate button
    if col_v.button(
        "🔍 Validate",
        key=f"validate_{idx}",
        use_container_width=True,
    ):
        with st.spinner("🔍 AI is validating your SQL..."):
            val_data, val_err = api_validate_sql(edited_sql)
        if val_err:
            st.session_state.validation_result = {
                "is_valid": False,
                "issues": [val_err],
                "suggestions": "",
                "corrected_sql": None,
                "risk_assessment": "safe",
            }
        else:
            st.session_state.validation_result = val_data
        st.rerun()

    # Execute edited SQL button
    if col_e.button(
        "▶ Execute",
        key=f"exec_edited_{idx}",
        type="primary",
        use_container_width=True,
    ):
        with st.spinner("⏳ Executing edited SQL…"):
            data, err = api_execute_sql(
                sql=edited_sql,
                question=msg.get("original_question", ""),
                confirmed=False,
            )
        if err:
            st.error(f"❌ {err}")
        elif data:
            risk = data.get("risk_level", "safe")
            has_results = bool(
                data.get("execution_results")
                or data.get("dataframe")
            )
            # Update message with new SQL and result
            msg["sql"] = edited_sql
            if risk in ("moderate", "risky") and not has_results:
                msg["needs_confirmation"] = True
                msg["result"] = data
                msg["executed"] = False
            else:
                msg["executed"] = True
                msg["result"] = data
                msg["needs_confirmation"] = False
            st.session_state.editing_msg_idx = None
            st.session_state.validation_result = None
        st.rerun()

    # Cancel button
    if col_c.button(
        "Cancel",
        key=f"cancel_edit_{idx}",
        use_container_width=True,
    ):
        st.session_state.editing_msg_idx = None
        st.session_state.edit_sql = ""
        st.session_state.validation_result = None
        st.rerun()

    # Show validation result
    val_result = st.session_state.validation_result
    if val_result:
        if val_result.get("is_valid"):
            st.markdown(
                "<div class='validation-pass'>"
                "✅ <strong>SQL is valid</strong> — "
                "All checks passed.</div>",
                unsafe_allow_html=True,
            )
            suggestions = val_result.get("suggestions", "")
            if suggestions:
                st.caption(f"💡 {suggestions}")
        else:
            issues = val_result.get("issues", [])
            issues_html = "".join(
                f"<li>{issue}</li>" for issue in issues
            )
            st.markdown(
                f"<div class='validation-fail'>"
                f"❌ <strong>Issues found:</strong>"
                f"<ul style='margin:8px 0 0 0; padding-left:20px'>"
                f"{issues_html}</ul></div>",
                unsafe_allow_html=True,
            )
            suggestions = val_result.get("suggestions", "")
            if suggestions:
                st.caption(f"💡 {suggestions}")
            corrected = val_result.get("corrected_sql")
            if corrected:
                with st.expander("📝 Suggested correction", expanded=True):
                    st.code(corrected, language="sql")
                    if st.button(
                        "Apply correction",
                        key=f"apply_correction_{idx}",
                    ):
                        st.session_state.edit_sql = corrected
                        st.session_state.validation_result = None
                        st.rerun()

        risk = val_result.get("risk_assessment", "safe")
        if risk != "safe":
            risk_colors = {
                "moderate": ("🟡", "#fde68a"),
                "risky": ("🔴", "#fca5a5"),
            }
            r_icon, r_color = risk_colors.get(
                risk, ("🟡", "#fde68a"),
            )
            st.caption(
                f"{r_icon} Risk: **{risk}**"
            )


def _process_api_response(data, err, question: str):
    """Process the API response and append assistant message."""
    if err:
        st.session_state.messages.append({
            "role":    "assistant",
            "content": f"Error: {err}",
            "error":   True,
        })
        return

    if not data:
        st.session_state.messages.append({
            "role":    "assistant",
            "content": "No response from the API.",
            "error":   True,
        })
        return

    # Clarification needed
    if data.get("needs_clarification"):
        cr = data.get("clarification_request", {})
        st.session_state.messages.append({
            "role":                "assistant",
            "content":             cr.get("message", "Please clarify."),
            "needs_clarification": True,
            "clarification_data":  cr,
        })
        return

    # Hard block
    if not data.get("guardrail_allowed", True):
        st.session_state.messages.append({
            "role":              "assistant",
            "content":           data.get("explanation", ""),
            "sql":               data.get("sql", ""),
            "blocked":           True,
            "result":            data,
            "original_question": question,
        })
        return

    # Check risk level
    risk = data.get("risk_level", "safe")
    has_results = bool(
        data.get("execution_results") or data.get("dataframe")
    )

    if risk in ("moderate", "risky") and not has_results:
        # Needs risk confirmation
        st.session_state.messages.append({
            "role":               "assistant",
            "content":            data.get("explanation", ""),
            "sql":                data.get(
                "safe_sql", data.get("sql", ""),
            ),
            "needs_confirmation": True,
            "executed":           False,
            "result":             data,
            "original_question":  question,
        })
    else:
        # Successful — SQL generated and executed
        st.session_state.messages.append({
            "role":              "assistant",
            "content":           data.get("explanation", ""),
            "sql":               data.get(
                "safe_sql", data.get("sql", ""),
            ),
            "executed":          has_results,
            "result":            data,
            "original_question": question,
        })
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
    earned = [round(raw[i] * maxes[i]) for i in range(len(keys))]
    text   = [f"{earned[i]}/{maxes[i]}" for i in range(len(keys))]
    colors = [
        "#22c55e" if r >= 0.70 else "#f59e0b" if r >= 0.40 else "#ef4444"
        for r in raw
    ]

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

    with st.expander(
        f"🔬 Sanity Check Anomalies  —  {len(anomalies)} issue(s)  "
        f"({pass_rate:.0%} pass rate)",
        expanded=True,
    ):
        for a in anomalies:
            sev  = a.get("severity", "WARNING")
            cls  = "anomaly-error" if sev == "ERROR" else "anomaly-warning"
            icon = "❌" if sev == "ERROR" else "⚠️"
            col  = (
                f" · Column: <code>{a['column']}</code>"
                if a.get("column") else ""
            )
            st.markdown(
                f"<div class='{cls}'>"
                f"<strong>{icon} {a.get('check', '')}</strong>{col}<br>"
                f"<span style='color:#cbd5e1'>{a.get('message','')}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )


def render_verification(result: dict):
    """Render back-translation verification."""
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
        st.warning(
            "⚠️ LOW ALIGNMENT — SQL may not correctly answer the intended question."
        )


def render_guardrail_warnings(result: dict):
    """Render guardrail violation banners."""
    warnings = result.get("guardrail_warnings", [])
    limit    = result.get("guardrail_limit_applied", False)
    if limit:
        st.info("🔒 Guardrail: LIMIT clause was automatically appended.")
    if warnings:
        for w in warnings:
            st.warning(f"⚠️ Guardrail: {w}")


# =========================================================
# AUTH PAGE  (shown when not logged in)
# =========================================================

if not st.session_state.logged_in:
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown(
            "<div style='text-align:center; padding:40px 0 20px 0;'>"
            "<span style='font-size:3rem'>🗄️</span>"
            "<h2 style='color:#a5b4fc; font-weight:700; margin-top:8px'>"
            "Text-to-SQL Explorer</h2>"
            "<p style='color:#94a3b8; font-size:0.9rem'>"
            "Sign in or create an account to get started</p>"
            "</div>",
            unsafe_allow_html=True,
        )

        auth_tab_login, auth_tab_signup = st.tabs(["🔑 Login", "📝 Sign Up"])

        with auth_tab_login:
            with st.form("login_form"):
                login_email = st.text_input(
                    "Email", placeholder="you@example.com", key="login_email",
                )
                login_pass = st.text_input(
                    "Password", type="password", key="login_pass",
                )
                login_btn = st.form_submit_button(
                    "Log In", use_container_width=True,
                )

            if login_btn:
                if not login_email or not login_pass:
                    st.error("Please enter both email and password.")
                else:
                    data, err = api_login(login_email, login_pass)
                    if err:
                        st.error(f"❌ {err}")
                    elif data:
                        st.session_state.token = data["token"]
                        st.session_state.user_id = data["user_id"]
                        st.session_state.user_email = data["email"]
                        st.session_state.user_role = data["role"]
                        st.session_state.logged_in = True
                        st.success(f"✅ Welcome back, {data['email']}!")
                        time.sleep(0.5)
                        st.rerun()

        with auth_tab_signup:
            with st.form("signup_form"):
                signup_email = st.text_input(
                    "Email", placeholder="you@example.com", key="signup_email",
                )
                signup_pass = st.text_input(
                    "Password", type="password", key="signup_pass",
                )
                signup_pass2 = st.text_input(
                    "Confirm Password", type="password", key="signup_pass2",
                )
                signup_btn = st.form_submit_button(
                    "Create Account", use_container_width=True,
                )

            if signup_btn:
                if not signup_email or not signup_pass:
                    st.error("Please fill in all fields.")
                elif signup_pass != signup_pass2:
                    st.error("Passwords do not match.")
                elif len(signup_pass) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    data, err = api_signup(signup_email, signup_pass)
                    if err:
                        st.error(f"❌ {err}")
                    elif data:
                        st.session_state.token = data["token"]
                        st.session_state.user_id = data["user_id"]
                        st.session_state.user_email = data["email"]
                        st.session_state.user_role = data["role"]
                        st.session_state.logged_in = True
                        st.success(
                            f"✅ Account created! Welcome, {data['email']}!"
                        )
                        time.sleep(0.5)
                        st.rerun()

    st.stop()


# =========================================================
# SIDEBAR (logged-in users only)
# =========================================================

with st.sidebar:
    # Logo / title
    st.markdown(
        "<h2 style='color:#a5b4fc; font-weight:700; margin-bottom:0'>"
        "🗄️ Text-to-SQL</h2>"
        "<p style='color:#64748b; font-size:0.8rem; margin-top:2px'>"
        "Powered by Groq LLM</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    # User profile
    role_icons = {"viewer": "👁️", "editor": "✏️", "admin": "🛡️"}
    r_icon = role_icons.get(st.session_state.user_role, "👤")
    st.markdown(
        f"<div style='background:rgba(165,180,252,0.1); border:1px solid #334155; "
        f"border-radius:8px; padding:10px 12px; margin-bottom:8px;'>"
        f"<span style='font-size:0.9rem;'>👤 {st.session_state.user_email}</span><br>"
        f"<span style='color:#94a3b8; font-size:0.75rem;'>"
        f"{r_icon} {st.session_state.user_role.capitalize()}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    if st.button("🚪 Logout", use_container_width=True):
        for k in [
            "token", "user_id", "user_email", "logged_in",
            "connections", "active_connection_id", "active_connection_name",
            "messages", "detail_result", "current_page",
        ]:
            if k == "logged_in":
                st.session_state[k] = False
            elif k in ("messages", "connections"):
                st.session_state[k] = []
            elif k == "current_page":
                st.session_state[k] = "chat"
            else:
                st.session_state[k] = None
        st.rerun()

    st.divider()

    # ── Page Navigation ───────────────────────────────────
    st.markdown(
        "<p class='section-label'>Navigation</p>",
        unsafe_allow_html=True,
    )
    nav_col1, nav_col2, nav_col3 = st.columns(3)
    with nav_col1:
        if st.button(
            "💬 Chat",
            use_container_width=True,
            key="nav_chat",
        ):
            st.session_state.current_page = "chat"
            st.rerun()
    with nav_col2:
        if st.button(
            "📊 Details",
            use_container_width=True,
            key="nav_details",
        ):
            st.session_state.current_page = "details"
            st.rerun()
    with nav_col3:
        if st.session_state.user_role == "admin":
            if st.button(
                "📋 Audit",
                use_container_width=True,
                key="nav_audit",
            ):
                st.session_state.current_page = "audit"
                st.rerun()

    st.divider()

    # ── Database Connections ──────────────────────────────
    with st.expander("🔌 Database Connections", expanded=True):
        conns_data, conns_err = api_list_connections()
        connections = (
            conns_data.get("connections", []) if conns_data else []
        )
        st.session_state.connections = connections

        conn_options = [{"id": None, "label": "📦 Default (college_2)"}]
        for c in connections:
            tbl_count = c.get("table_count", 0)
            conn_options.append({
                "id": c["id"],
                "label": f"🗄 {c['connection_name']} ({tbl_count} tables)",
            })

        labels = [o["label"] for o in conn_options]
        current_idx = 0
        for i, o in enumerate(conn_options):
            if o["id"] == st.session_state.active_connection_id:
                current_idx = i
                break

        selected_label = st.selectbox(
            "Active Database",
            options=labels,
            index=current_idx,
            key="db_selector",
        )
        selected_conn = conn_options[labels.index(selected_label)]
        st.session_state.active_connection_id = selected_conn["id"]
        st.session_state.active_connection_name = selected_label

        st.divider()

        # Connect new database form
        st.markdown(
            "<p style='font-size:0.82rem; color:#94a3b8; font-weight:600;'>"
            "Connect New Database</p>",
            unsafe_allow_html=True,
        )
        with st.form("connect_db_form"):
            c_name = st.text_input(
                "Connection Name", placeholder="My Database",
            )
            c_host = st.text_input("Host", value="localhost")
            c_port = st.number_input(
                "Port", value=5432, min_value=1, max_value=65535,
            )
            c_db = st.text_input(
                "Database Name", placeholder="my_database",
            )
            c_user = st.text_input("Username", value="postgres")
            c_pass = st.text_input("Password", type="password")
            connect_btn = st.form_submit_button(
                "🔗 Connect & Extract Schema", use_container_width=True,
            )

        if connect_btn:
            if not all([c_name, c_host, c_db, c_user, c_pass]):
                st.error("All fields are required.")
            else:
                with st.spinner("Connecting and extracting schema..."):
                    data, err = api_connect_db(
                        c_name, c_host, c_port, c_db, c_user, c_pass,
                    )
                if err:
                    st.error(f"❌ {err}")
                elif data:
                    st.success(f"✅ {data.get('message', 'Connected!')}")
                    st.session_state.active_connection_id = data["id"]
                    time.sleep(0.5)
                    st.rerun()

        # Delete connections
        if connections:
            st.divider()
            for c in connections:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.caption(f"🗄 {c['connection_name']}")
                    st.caption(
                        f"{c['host']}:{c['port']}/{c['database_name']}",
                    )
                with col2:
                    if st.button("🗑️", key=f"del_{c['id']}"):
                        api_delete_connection(c["id"])
                        if (
                            st.session_state.active_connection_id == c["id"]
                        ):
                            st.session_state.active_connection_id = None
                        st.rerun()

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
                            f"<span style='color:#6366f1; "
                            f"font-family:monospace; font-size:0.82rem'>"
                            f"{c['name']}</span>"
                            f"<span style='color:#475569; "
                            f"font-size:0.78rem'> · {c['type']}</span>",
                            unsafe_allow_html=True,
                        )


# =========================================================
# PAGE ROUTER
# =========================================================

page = st.session_state.current_page


# =========================================================
# PAGE: CHAT  (main conversational interface)
# =========================================================

if page == "chat":

    # Hero header
    st.markdown(
        "<h1 class='hero-header'>Text-to-SQL Explorer</h1>"
        "<p class='hero-sub'>Ask questions in plain English — "
        "get instant SQL, results, and confidence signals.</p>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    # ── Render chat history ─────────────────────────────────────────
    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                # ─── Assistant message rendering ────────────────────
                # Error message
                if msg.get("error"):
                    st.error(msg["content"])
                    continue

                # Clarification needed
                if msg.get("needs_clarification"):
                    cr = msg.get("clarification_data", {})
                    st.warning(
                        f"🤔 **Clarification Required**\n\n{msg['content']}"
                    )
                    opts = cr.get("interpretations", [])
                    if opts:
                        choices = [
                            o.get("example_query", o.get("label", ""))
                            for o in opts
                        ]
                        chosen = st.radio(
                            "Choose an interpretation:",
                            choices,
                            index=0,
                            key=f"clarify_radio_{i}",
                        )
                        if st.button(
                            "Submit clarification",
                            key=f"clarify_submit_{i}",
                        ):
                            # Re-submit with the chosen interpretation
                            st.session_state.messages.append(
                                {"role": "user", "content": chosen}
                            )
                            conv_history = _build_conv_history()
                            with st.spinner("⏳ Generating SQL..."):
                                data, err = api_query(
                                    chosen,
                                    confirmed=False,
                                    conversation_history=conv_history,
                                )
                            _process_api_response(data, err, chosen)
                            st.rerun()
                    continue

                # Show explanation
                if msg.get("content"):
                    st.markdown(
                        f"💬 {msg['content']}",
                    )

                # Show SQL
                sql = msg.get("sql", "")
                if sql:
                    # Check if we're editing this message
                    if st.session_state.editing_msg_idx == i:
                        _render_edit_mode(i, msg)
                    else:
                        st.code(sql, language="sql")

                # Blocked query
                if msg.get("blocked"):
                    st.markdown(
                        "<div class='risk-banner risk-risky'>"
                        "<span class='risk-badge risk-badge-risky'>"
                        "🚫 Hard Block</span>"
                        "<p class='risk-warning-text'>"
                        "This query was blocked by safety guardrails."
                        "</p></div>",
                        unsafe_allow_html=True,
                    )
                    result_data = msg.get("result", {})
                    for v in result_data.get("guardrail_warnings", []):
                        st.markdown(
                            f"<div class='anomaly-error'>"
                            f"<strong>❌ Violation:</strong> "
                            f"<span style='color:#fca5a5'>{v}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    continue

                # Risk confirmation needed
                if msg.get("needs_confirmation") and not msg.get("executed"):
                    result_data = msg.get("result", {})
                    risk_level = result_data.get("risk_level", "moderate")
                    risk_warning = result_data.get("risk_warning", "")
                    badge_cfg = {
                        "moderate": (
                            "risk-moderate",
                            "risk-badge-moderate",
                            "🟡 Moderate Risk",
                        ),
                        "risky": (
                            "risk-risky",
                            "risk-badge-risky",
                            "🔴 High Risk",
                        ),
                    }
                    banner_cls, badge_cls, badge_label = badge_cfg.get(
                        risk_level,
                        badge_cfg["moderate"],
                    )

                    st.markdown(
                        f"<div class='risk-banner {banner_cls}'>"
                        f"<span class='risk-badge {badge_cls}'>"
                        f"{badge_label}</span>"
                        f"<p class='risk-warning-text'>{risk_warning}</p>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                    col_confirm, col_cancel, _ = st.columns([1.6, 1, 5])
                    if col_confirm.button(
                        "✅ Execute Anyway",
                        type="primary",
                        key=f"risk_confirm_{i}",
                        use_container_width=True,
                    ):
                        with st.spinner("⏳ Executing confirmed query…"):
                            data, err = api_execute_sql(
                                sql=sql,
                                question=msg.get("original_question", ""),
                                confirmed=True,
                            )
                        if err:
                            st.error(f"❌ {err}")
                        elif data:
                            msg["result"] = data
                            msg["executed"] = True
                            msg["needs_confirmation"] = False
                        st.rerun()

                    if col_cancel.button(
                        "❌ Cancel",
                        key=f"risk_cancel_{i}",
                        use_container_width=True,
                    ):
                        msg["cancelled"] = True
                        msg["needs_confirmation"] = False
                        st.rerun()
                    continue

                # ── Executed state ─────────────────────────────────
                if msg.get("executed"):
                    result_data = msg.get("result", {})
                    row_count = result_data.get("row_count", 0)
                    exec_ms = result_data.get("execution_time_ms", 0)
                    exec_err = result_data.get("execution_error")
                    rows_affected = result_data.get("rows_affected", 0)

                    if exec_err:
                        st.error(f"❌ Execution error: {exec_err}")
                    else:
                        # Build summary text
                        summary_parts = [f"{row_count} row(s) returned"]
                        if exec_ms:
                            summary_parts.append(f"{exec_ms:.1f}ms")
                        if rows_affected:
                            summary_parts.append(
                                f"{rows_affected} row(s) affected"
                            )

                        st.markdown(
                            f"<div class='executed-badge'>"
                            f"✅ Executed — {' · '.join(summary_parts)}"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                    col1, col2, _ = st.columns([1, 1, 3])
                    if col1.button(
                        "📊 View Details",
                        key=f"details_{i}",
                    ):
                        st.session_state.detail_result = result_data
                        st.session_state.current_page = "details"
                        st.rerun()

                    if (
                        not st.session_state.editing_msg_idx == i
                        and col2.button(
                            "✏️ Edit SQL", key=f"edit_btn_{i}",
                        )
                    ):
                        st.session_state.editing_msg_idx = i
                        st.session_state.edit_sql = sql
                        st.session_state.validation_result = None
                        st.rerun()

                # ── Not yet executed — show Execute button ─────────
                elif not msg.get("needs_confirmation") and not msg.get(
                    "blocked"
                ) and not msg.get("cancelled") and sql:
                    col1, col2, _ = st.columns([1, 1, 3])
                    if col1.button(
                        "▶ Execute",
                        key=f"exec_{i}",
                        type="primary",
                    ):
                        with st.spinner("⏳ Executing query…"):
                            data, err = api_execute_sql(
                                sql=sql,
                                question=msg.get(
                                    "original_question", ""
                                ),
                                confirmed=False,
                            )
                        if err:
                            st.error(f"❌ {err}")
                        elif data:
                            # Check if needs risk confirmation
                            risk = data.get("risk_level", "safe")
                            has_results = bool(
                                data.get("execution_results")
                                or data.get("dataframe")
                            )
                            if risk in ("moderate", "risky") and not has_results:
                                msg["needs_confirmation"] = True
                                msg["result"] = data
                            else:
                                msg["executed"] = True
                                msg["result"] = data
                        st.rerun()

                    if (
                        not st.session_state.editing_msg_idx == i
                        and col2.button(
                            "✏️ Edit SQL", key=f"edit_btn2_{i}",
                        )
                    ):
                        st.session_state.editing_msg_idx = i
                        st.session_state.edit_sql = sql
                        st.session_state.validation_result = None
                        st.rerun()

    # ── Chat input ──────────────────────────────────────────────────
    if prompt := st.chat_input(
        "Ask about your database…  e.g. 'How many students per department?'"
    ):
        # Add user message
        st.session_state.messages.append(
            {"role": "user", "content": prompt}
        )

        # Build conversation history for context
        conv_history = _build_conv_history()

        # Call API
        with st.spinner("⏳ Running pipeline — generating SQL…"):
            data, err = api_query(
                prompt,
                confirmed=False,
                conversation_history=conv_history,
            )

        _process_api_response(data, err, prompt)
        st.rerun()

    # Empty state
    if not st.session_state.messages:
        st.markdown(
            "<div style='text-align:center; padding: 80px 0; "
            "color:#334155'>"
            "<div style='font-size:3.5rem'>🗄️</div>"
            "<h3 style='color:#475569; font-weight:500; "
            "margin-top:12px'>Ready to query</h3>"
            "<p style='color:#374151; font-size:0.9rem'>"
            "Type a question below to get started</p>"
            "</div>",
            unsafe_allow_html=True,
        )


# =========================================================
# PAGE: DETAILS  (results, confidence, verification, sanity)
# =========================================================

elif page == "details":

    # Back button
    if st.button("← Back to Chat", key="back_to_chat"):
        st.session_state.current_page = "chat"
        st.rerun()

    st.markdown(
        "<h1 class='hero-header'>Results & Analysis</h1>"
        "<p class='hero-sub'>Detailed breakdown of the query execution, "
        "confidence signals, and verification.</p>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    result = st.session_state.detail_result

    if not result:
        st.info(
            "ℹ️ No query results to display. Run a query in the Chat "
            "and click **📊 View Details** to see the full analysis."
        )
    else:
        # ── Tabs layout ────────────────────────────────────────────
        tab_names = [
            "📊 Results",
            "📝 SQL Details",
            "🎯 Confidence",
            "🔁 Verification",
        ]
        tabs = st.tabs(tab_names)

        # ─── TAB 1: RESULTS ────────────────────────────────────────
        with tabs[0]:
            st.markdown("<div class='card'>", unsafe_allow_html=True)

            # Query info
            question = result.get("question", "")
            if question:
                st.markdown(
                    f"<p class='section-label'>Original Question</p>"
                    f"<p style='color:#e2e8f0; font-size:0.95rem; "
                    f"font-style:italic'>\"{question}\"</p>",
                    unsafe_allow_html=True,
                )
                st.divider()

            render_results(result)
            st.markdown("</div>", unsafe_allow_html=True)
            render_sanity(result)

        # ─── TAB 2: SQL DETAILS ────────────────────────────────────
        with tabs[1]:
            st.markdown("<div class='card'>", unsafe_allow_html=True)
            render_guardrail_warnings(result)

            st.markdown(
                "<p class='section-label'>Executed SQL</p>",
                unsafe_allow_html=True,
            )
            st.code(
                result.get("safe_sql", result.get("sql", "")),
                language="sql",
            )

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
                st.markdown(
                    "<p class='section-label'>Tables Accessed</p>",
                    unsafe_allow_html=True,
                )
                for t in result.get("tables_accessed", []):
                    st.markdown(
                        f"<code style='background:#1e3a5f; color:#93c5fd; "
                        f"padding:2px 8px; border-radius:4px; "
                        f"font-size:0.82rem'>{t}</code> ",
                        unsafe_allow_html=True,
                    )
            with col_m2:
                st.markdown(
                    "<p class='section-label'>Columns Accessed</p>",
                    unsafe_allow_html=True,
                )
                cols_str = ", ".join(
                    f"{c['table']}.{c['column']}"
                    for c in result.get("columns_accessed", [])
                )
                st.caption(cols_str or "—")

            st.markdown("</div>", unsafe_allow_html=True)

            # Explanation
            with st.expander("💬 Query Explanation", expanded=True):
                st.markdown(
                    f"<p style='color:#cbd5e1; font-size:0.92rem; "
                    f"line-height:1.6'>"
                    f"{result.get('explanation', '—')}</p>",
                    unsafe_allow_html=True,
                )

            # Risk info
            risk_level = result.get("risk_level", "safe")
            risk_warning = result.get("risk_warning", "")
            if risk_warning:
                badge_cfg = {
                    "safe": (
                        "risk-safe", "risk-badge-safe",
                        "🟢 Safe",
                    ),
                    "moderate": (
                        "risk-moderate", "risk-badge-moderate",
                        "🟡 Moderate Risk",
                    ),
                    "risky": (
                        "risk-risky", "risk-badge-risky",
                        "🔴 High Risk",
                    ),
                }
                banner_cls, badge_cls, badge_label = badge_cfg.get(
                    risk_level, badge_cfg["safe"]
                )
                st.markdown(
                    f"<div class='risk-banner {banner_cls}'>"
                    f"<span class='risk-badge {badge_cls}'>"
                    f"{badge_label}</span>"
                    f"<p class='risk-warning-text'>{risk_warning}</p>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

        # ─── TAB 3: CONFIDENCE ─────────────────────────────────────
        with tabs[2]:
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
                max_pts = {
                    "syntax_validity": 20,
                    "back_translation": 35,
                    "sanity_pass_rate": 30,
                    "schema_coverage": 15,
                }
                labels = {
                    "syntax_validity": "Syntax Validity",
                    "back_translation": "Back-Translation Alignment",
                    "sanity_pass_rate": "Sanity Check Pass Rate",
                    "schema_coverage": "Schema Coverage",
                }

                rows_conf = []
                for k, lbl in labels.items():
                    raw = bd.get(k, 0)
                    mx = max_pts[k]
                    rows_conf.append({
                        "Signal":    lbl,
                        "Raw Score": f"{raw:.0%}",
                        "Weight":    f"{mx}%",
                        "Points":    f"{round(raw * mx)} / {mx}",
                        "Status": (
                            "✅ Good" if raw >= 0.70
                            else "🟡 Fair" if raw >= 0.40
                            else "❌ Poor"
                        ),
                    })
                st.dataframe(
                    pd.DataFrame(rows_conf),
                    use_container_width=True,
                    hide_index=True,
                )

        # ─── TAB 4: VERIFICATION ───────────────────────────────────
        with tabs[3]:
            st.markdown("<div class='card'>", unsafe_allow_html=True)
            st.markdown(
                "<p class='section-label'>"
                "SQL-to-Question Back-Translation</p>",
                unsafe_allow_html=True,
            )
            st.markdown(
                "<p style='color:#94a3b8; font-size:0.82rem; "
                "margin-bottom:12px'>"
                "The generated SQL was sent back to the LLM asking "
                "<em>\"What question does this SQL answer?\"</em> — "
                "the result is compared to your original question.</p>",
                unsafe_allow_html=True,
            )
            render_verification(result)
            st.markdown("</div>", unsafe_allow_html=True)


# =========================================================
# PAGE: AUDIT LOG  (admin only)
# =========================================================

elif page == "audit":

    # Back button
    if st.button("← Back to Chat", key="back_to_chat_audit"):
        st.session_state.current_page = "chat"
        st.rerun()

    st.markdown(
        "<h1 class='hero-header'>Audit Log</h1>"
        "<p class='hero-sub'>Full audit trail of all query executions "
        "across users and roles.</p>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    if st.session_state.user_role != "admin":
        st.error(
            "🚫 Access denied. Only admin users can view the audit log."
        )
    else:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        audit_data, audit_err = api_audit(limit=50)
        if audit_err:
            st.error(f"❌ {audit_err}")
        elif audit_data:
            records = audit_data.get("records", [])
            total = audit_data.get("total_records", 0)

            if records:
                st.caption(
                    f"Showing {len(records)} of {total} records "
                    f"(newest first)"
                )

                display_rows = []
                for rec in records:
                    r_icons = {
                        "viewer": "👁️",
                        "editor": "✏️",
                        "admin": "🛡️",
                    }
                    r = rec.get("role", "")
                    display_rows.append({
                        "Time": (
                            str(rec.get("timestamp", ""))[:19]
                            .replace("T", " ")
                        ),
                        "User": rec.get("user_id", ""),
                        "Role": f"{r_icons.get(r, '')} {r}",
                        "Question": (
                            (rec.get("question", "") or "")[:60]
                        ),
                        "SQL": (
                            (
                                rec.get("safe_sql", "")
                                or rec.get("generated_sql", "")
                                or ""
                            )[:60]
                        ),
                        "Time (ms)": round(
                            rec.get("execution_time_ms", 0), 1,
                        ),
                        "Rows": rec.get("row_count", 0),
                        "Affected": rec.get("rows_affected", 0),
                        "Success": (
                            "✅" if rec.get("success") else "❌"
                        ),
                        "Risk": rec.get("risk_level", ""),
                        "Permitted": (
                            "✅" if rec.get("permission_granted")
                            else "🚫"
                        ),
                    })

                st.dataframe(
                    pd.DataFrame(display_rows),
                    use_container_width=True,
                    hide_index=True,
                    height=400,
                )
            else:
                st.info("No audit records found.")
        st.markdown("</div>", unsafe_allow_html=True)

