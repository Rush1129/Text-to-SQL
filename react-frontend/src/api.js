/**
 * api.js — API service layer
 * Mirrors all Streamlit _api helpers for the FastAPI backend.
 */

const DEFAULT_BASE = import.meta.env.VITE_tts || 'http://localhost:8000';

function getBaseUrl() {
  return localStorage.getItem('api_base_url') || DEFAULT_BASE;
}

function getAuthHeaders() {
  const token = localStorage.getItem('token');
  if (token) return { Authorization: `Bearer ${token}` };
  return {};
}

async function request(method, path, { json, params, headers = {} } = {}) {
  const base = getBaseUrl().replace(/\/+$/, '');
  let url = `${base}${path}`;

  if (params) {
    const qs = new URLSearchParams(params).toString();
    if (qs) url += `?${qs}`;
  }

  const opts = {
    method,
    headers: {
      ...getAuthHeaders(),
      ...headers,
    },
  };

  if (json !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(json);
  }

  try {
    const res = await fetch(url, opts);

    if (res.status === 401) {
      localStorage.removeItem('token');
      return [null, 'Session expired. Please log in again.'];
    }

    if (!res.ok) {
      const text = await res.text();
      return [null, `API error ${res.status}: ${text.slice(0, 200)}`];
    }

    const data = await res.json();
    return [data, null];
  } catch (err) {
    if (err.name === 'TypeError' && err.message.includes('fetch')) {
      return [null, `Cannot connect to API at ${base}. Is the backend running?`];
    }
    return [null, err.message];
  }
}

// ── Auth ─────────────────────────────────────────────────
export function apiSignup(email, password) {
  return request('POST', '/v1/auth/signup', { json: { email, password } });
}

export function apiLogin(email, password) {
  return request('POST', '/v1/auth/login', { json: { email, password } });
}

// ── Query ────────────────────────────────────────────────
export function apiQuery(question, sessionId, confirmed, connectionId, conversationHistory) {
  const payload = { question, session_id: sessionId, confirmed };
  if (connectionId) payload.connection_id = connectionId;
  if (conversationHistory) payload.conversation_history = conversationHistory;
  return request('POST', '/v1/query', { json: payload });
}

// ── SQL Validation & Execution ───────────────────────────
export function apiValidateSql(sql, connectionId) {
  const payload = { sql };
  if (connectionId) payload.connection_id = connectionId;
  return request('POST', '/v1/validate-sql', { json: payload });
}

export function apiExecuteSql(sql, question, sessionId, confirmed, connectionId) {
  const payload = { sql, question, session_id: sessionId, confirmed };
  if (connectionId) payload.connection_id = connectionId;
  return request('POST', '/v1/execute-sql', { json: payload });
}

// ── History ──────────────────────────────────────────────
export function apiHistory(sessionId, limit = 50) {
  return request('GET', '/v1/history', { params: { session_id: sessionId, limit } });
}

// ── Schema ───────────────────────────────────────────────
export function apiSchema(connectionId) {
  const params = {};
  if (connectionId) params.connection_id = connectionId;
  return request('GET', '/v1/schema', { params });
}

// ── Audit ────────────────────────────────────────────────
export function apiAudit(limit = 50, offset = 0) {
  return request('GET', '/v1/audit', { params: { limit, offset } });
}

// ── Connections ──────────────────────────────────────────
export function apiConnectDb(name, host, port, db, user, password) {
  return request('POST', '/v1/connections', {
    json: {
      connection_name: name,
      host, port,
      database_name: db,
      username: user,
      password,
    },
  });
}

export function apiListConnections() {
  return request('GET', '/v1/connections');
}

export function apiDeleteConnection(connId) {
  return request('DELETE', `/v1/connections/${connId}`);
}
