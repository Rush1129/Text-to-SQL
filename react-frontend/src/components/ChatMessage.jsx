import { useState } from 'react';
import {
  User, Bot, CheckCircle, AlertTriangle, ShieldX,
  BarChart3, Pencil, Play, Copy,
} from 'lucide-react';
import SqlEditor from './SqlEditor';
import RiskBanner from './RiskBanner';
import useStore from '../store';
import { apiExecuteSql, apiQuery } from '../api';

export default function ChatMessage({ msg, idx }) {
  const [editing, setEditing] = useState(false);
  const [clarifyChoice, setClarifyChoice] = useState(0);

  const updateMessage = useStore((s) => s.updateMessage);
  const addMessage = useStore((s) => s.addMessage);
  const setDetailResult = useStore((s) => s.setDetailResult);
  const sessionId = useStore((s) => s.sessionId);
  const activeConnectionId = useStore((s) => s.activeConnectionId);
  const buildConvHistory = useStore((s) => s.buildConvHistory);

  // ── User message ─────────────────────────────────────────
  if (msg.role === 'user') {
    return (
      <div className="chat-message user-msg">
        <div className="chat-avatar user-avatar">
          <User style={{ width: 14, height: 14 }} />
        </div>
        <div className="chat-bubble user-bubble">
          <div className="chat-text">{msg.content}</div>
        </div>
      </div>
    );
  }

  // ── Assistant message ────────────────────────────────────
  const handleExecute = async (sql, confirmed = false) => {
    const [data, err] = await apiExecuteSql(
      sql, msg.original_question || '', sessionId, confirmed, activeConnectionId
    );
    if (err) {
      // show inline error
      return;
    }
    if (data) {
      const risk = data.risk_level || 'safe';
      const hasResults = !!(data.execution_results?.length || data.dataframe?.length);
      if (risk !== 'safe' && !hasResults && !confirmed) {
        updateMessage(idx, { needs_confirmation: true, result: data, executed: false });
      } else {
        updateMessage(idx, { executed: true, result: data, needs_confirmation: false });
      }
    }
    setEditing(false);
  };

  const handleConfirm = async () => {
    await handleExecute(msg.sql, true);
  };

  const handleCancel = () => {
    updateMessage(idx, { cancelled: true, needs_confirmation: false });
  };

  const handleEditExecute = async (editedSql) => {
    const [data, err] = await apiExecuteSql(
      editedSql, msg.original_question || '', sessionId, false, activeConnectionId
    );
    if (err) return;
    if (data) {
      const risk = data.risk_level || 'safe';
      const hasResults = !!(data.execution_results?.length || data.dataframe?.length);
      if (risk !== 'safe' && !hasResults) {
        updateMessage(idx, { sql: editedSql, needs_confirmation: true, result: data, executed: false });
      } else {
        updateMessage(idx, { sql: editedSql, executed: true, result: data, needs_confirmation: false });
      }
    }
    setEditing(false);
  };

  const handleClarify = async (chosen) => {
    addMessage({ role: 'user', content: chosen });
    const convHistory = buildConvHistory();
    const [data, err] = await apiQuery(chosen, sessionId, false, activeConnectionId, convHistory);
    if (err) {
      addMessage({ role: 'assistant', content: `Error: ${err}`, error: true });
    } else if (data) {
      processApiResponse(data, chosen);
    }
  };

  const processApiResponse = (data, question) => {
    if (data.needs_clarification) {
      addMessage({
        role: 'assistant',
        content: data.clarification_request?.message || 'Please clarify.',
        needs_clarification: true,
        clarification_data: data.clarification_request,
      });
      return;
    }
    if (!data.guardrail_allowed) {
      addMessage({
        role: 'assistant', content: data.explanation || '',
        sql: data.sql || '', blocked: true, result: data,
        original_question: question,
      });
      return;
    }
    const risk = data.risk_level || 'safe';
    const hasResults = !!(data.execution_results?.length || data.dataframe?.length);
    if (risk !== 'safe' && !hasResults) {
      addMessage({
        role: 'assistant', content: data.explanation || '',
        sql: data.safe_sql || data.sql || '',
        needs_confirmation: true, executed: false, result: data,
        original_question: question,
      });
    } else {
      addMessage({
        role: 'assistant', content: data.explanation || '',
        sql: data.safe_sql || data.sql || '',
        executed: hasResults, result: data,
        original_question: question,
      });
    }
  };

  return (
    <div className="chat-message">
      <div className="chat-avatar assistant-avatar">
        <Bot style={{ width: 14, height: 14 }} />
      </div>
      <div className="chat-bubble">
        {/* Error */}
        {msg.error && (
          <div className="validation-fail">{msg.content}</div>
        )}

        {/* Clarification */}
        {msg.needs_clarification && !msg.error && (
          <div>
            <div className="risk-banner risk-moderate" style={{ borderColor: 'var(--warning)' }}>
              <div className="risk-banner-header">
                <AlertTriangle style={{ width: 14, height: 14 }} />
                <strong>Clarification Required</strong>
              </div>
              <div className="risk-banner-text">{msg.content}</div>
            </div>
            {msg.clarification_data?.interpretations?.length > 0 && (
              <div className="clarification-options">
                {msg.clarification_data.interpretations.map((opt, i) => {
                  const label = opt.example_query || opt.label || '';
                  return (
                    <label
                      className={`clarification-option ${clarifyChoice === i ? 'selected' : ''}`}
                      key={i}
                    >
                      <input
                        type="radio"
                        name={`clarify-${idx}`}
                        checked={clarifyChoice === i}
                        onChange={() => setClarifyChoice(i)}
                      />
                      {label}
                    </label>
                  );
                })}
                <button
                  className="btn btn-primary btn-sm mt-2"
                  onClick={() => {
                    const opts = msg.clarification_data.interpretations;
                    const chosen = opts[clarifyChoice]?.example_query || opts[clarifyChoice]?.label || '';
                    handleClarify(chosen);
                  }}
                >
                  Submit clarification
                </button>
              </div>
            )}
          </div>
        )}

        {/* Normal content */}
        {!msg.error && !msg.needs_clarification && msg.content && (
          <div className="chat-text">{msg.content}</div>
        )}

        {/* SQL display */}
        {msg.sql && !msg.error && !msg.needs_clarification && (
          editing ? (
            <SqlEditor
              sql={msg.sql}
              onExecute={handleEditExecute}
              onCancel={() => setEditing(false)}
              msgIdx={idx}
            />
          ) : (
            <div className="chat-sql">
              <div className="chat-sql-header">
                <span>SQL</span>
                <button
                  className="btn btn-ghost btn-sm"
                  style={{ padding: '2px 6px' }}
                  onClick={() => navigator.clipboard.writeText(msg.sql)}
                  title="Copy SQL"
                >
                  <Copy style={{ width: 12, height: 12 }} />
                </button>
              </div>
              <pre>{msg.sql}</pre>
            </div>
          )
        )}

        {/* Blocked */}
        {msg.blocked && (
          <div>
            <RiskBanner riskLevel="risky" riskWarning="This query was blocked by safety guardrails." />
            {(msg.result?.guardrail_warnings || []).map((v, i) => (
              <div className="anomaly-item anomaly-error" key={i}>
                <strong>Violation:</strong> {v}
              </div>
            ))}
          </div>
        )}

        {/* Risk confirmation */}
        {msg.needs_confirmation && !msg.executed && !msg.error && !msg.needs_clarification && (
          <RiskBanner
            riskLevel={msg.result?.risk_level || 'moderate'}
            riskWarning={msg.result?.risk_warning || ''}
            onConfirm={handleConfirm}
            onCancel={handleCancel}
          />
        )}

        {/* Executed state */}
        {msg.executed && !msg.error && (
          <div>
            {msg.result?.execution_error ? (
              <div className="validation-fail">
                Execution error: {msg.result.execution_error}
              </div>
            ) : (
              <div className="executed-badge">
                <CheckCircle style={{ width: 14, height: 14 }} />
                Executed — {msg.result?.row_count || 0} row(s)
                {msg.result?.execution_time_ms ? ` · ${msg.result.execution_time_ms.toFixed(1)}ms` : ''}
                {msg.result?.rows_affected ? ` · ${msg.result.rows_affected} affected` : ''}
              </div>
            )}
            <div className="chat-actions">
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => {
                  setDetailResult(msg.result);
                  window.location.hash = '#/details';
                }}
              >
                <BarChart3 style={{ width: 12, height: 12 }} />
                View Details
              </button>
              {!editing && (
                <button className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>
                  <Pencil style={{ width: 12, height: 12 }} />
                  Edit SQL
                </button>
              )}
            </div>
          </div>
        )}

        {/* Not yet executed, not blocked, not cancelled, has SQL */}
        {!msg.executed && !msg.needs_confirmation && !msg.blocked && !msg.cancelled
          && msg.sql && !msg.error && !msg.needs_clarification && (
          <div className="chat-actions">
            <button className="btn btn-primary btn-sm" onClick={() => handleExecute(msg.sql)}>
              <Play style={{ width: 12, height: 12 }} />
              Execute
            </button>
            {!editing && (
              <button className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>
                <Pencil style={{ width: 12, height: 12 }} />
                Edit SQL
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
