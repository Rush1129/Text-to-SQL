import { useState } from 'react';
import {
  ArrowLeft, Table2, Code2, Target, RotateCcw,
  CheckCircle, XCircle, ShieldAlert, AlertTriangle,
} from 'lucide-react';
import ResultsTable from '../components/ResultsTable';
import ConfidenceChart from '../components/ConfidenceChart';
import RiskBanner from '../components/RiskBanner';
import useStore from '../store';

export default function DetailsPage() {
  const [activeTab, setActiveTab] = useState(0);
  const result = useStore((s) => s.detailResult);

  const tabs = [
    { icon: Table2, label: 'Results' },
    { icon: Code2, label: 'SQL Details' },
    { icon: Target, label: 'Confidence' },
    { icon: RotateCcw, label: 'Verification' },
  ];

  if (!result) {
    return (
      <div className="main-content">
        <div className="page-header">
          <button
            className="btn btn-ghost btn-sm mb-3"
            onClick={() => { window.location.hash = '#/'; }}
          >
            <ArrowLeft style={{ width: 14, height: 14 }} />
            Back to Chat
          </button>
          <h1>Results & Analysis</h1>
          <p>No query results to display. Run a query in the Chat and click View Details.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="main-content" style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
      <div className="page-header">
        <button
          className="btn btn-ghost btn-sm mb-3"
          onClick={() => { window.location.hash = '#/'; }}
        >
          <ArrowLeft style={{ width: 14, height: 14 }} />
          Back to Chat
        </button>
        <h1>Results & Analysis</h1>
        <p>Detailed breakdown of the query execution, confidence signals, and verification.</p>
      </div>

      {/* Tabs */}
      <div className="tabs">
        {tabs.map((t, i) => (
          <button
            key={i}
            className={`tab-btn ${activeTab === i ? 'active' : ''}`}
            onClick={() => setActiveTab(i)}
          >
            <t.icon />
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="tab-content">
        {/* Results */}
        {activeTab === 0 && (
          <div className="card">
            {result.question && (
              <div style={{ marginBottom: 12 }}>
                <div className="sidebar-section-label">Original Question</div>
                <div style={{ fontStyle: 'italic', color: 'var(--text-primary)', fontSize: '0.88rem' }}>
                  "{result.question}"
                </div>
                <hr style={{ border: 'none', borderTop: '1px solid var(--border-primary)', margin: '12px 0' }} />
              </div>
            )}
            <ResultsTable result={result} />

            {/* Sanity anomalies */}
            {(result.sanity_anomalies || []).length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <div className="sidebar-section-label">
                  Sanity Check Anomalies — {result.sanity_anomalies.length} issue(s) ({((result.sanity_pass_rate || 1) * 100).toFixed(0)}% pass rate)
                </div>
                {result.sanity_anomalies.map((a, i) => (
                  <div
                    key={i}
                    className={`anomaly-item ${a.severity === 'ERROR' ? 'anomaly-error' : 'anomaly-warning'}`}
                  >
                    <strong>{a.check || ''}</strong>
                    {a.column && <span style={{ color: 'var(--text-tertiary)' }}> · Column: <code>{a.column}</code></span>}
                    <br />
                    <span>{a.message || ''}</span>
                  </div>
                ))}
              </div>
            ) : result.sanity_summary ? (
              <div className="validation-pass" style={{ marginTop: 12 }}>
                <CheckCircle style={{ width: 14, height: 14 }} />
                {result.sanity_summary}
              </div>
            ) : null}
          </div>
        )}

        {/* SQL Details */}
        {activeTab === 1 && (
          <div className="card">
            {/* Guardrail warnings */}
            {result.guardrail_limit_applied && (
              <div className="validation-pass" style={{ marginBottom: 8 }}>
                <ShieldAlert style={{ width: 14, height: 14 }} />
                Guardrail: LIMIT clause was automatically appended.
              </div>
            )}
            {(result.guardrail_warnings || []).map((w, i) => (
              <div key={i} className="anomaly-item anomaly-warning" style={{ marginBottom: 6 }}>
                <AlertTriangle style={{ width: 12, height: 12, display: 'inline', verticalAlign: 'middle' }} /> Guardrail: {w}
              </div>
            ))}

            <div className="sidebar-section-label">Executed SQL</div>
            <div className="chat-sql">
              <pre>{result.safe_sql || result.sql || ''}</pre>
            </div>

            {/* Validation status */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
              {result.sql_valid ? (
                <span className="badge badge-success">
                  <CheckCircle style={{ width: 10, height: 10 }} /> Valid SQL
                </span>
              ) : (
                <span className="badge badge-error">
                  <XCircle style={{ width: 10, height: 10 }} /> Invalid SQL
                </span>
              )}
              <span style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
                {result.validation_message || ''}
              </span>
            </div>

            {/* Tables & Columns */}
            <div style={{ display: 'flex', gap: 24, marginTop: 16, flexWrap: 'wrap' }}>
              <div style={{ flex: 1, minWidth: 150 }}>
                <div className="sidebar-section-label">Tables Accessed</div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {(result.tables_accessed || []).map((t, i) => (
                    <span key={i} className="badge badge-neutral" style={{ fontFamily: 'JetBrains Mono, monospace' }}>{t}</span>
                  ))}
                </div>
              </div>
              <div style={{ flex: 1, minWidth: 150 }}>
                <div className="sidebar-section-label">Columns Accessed</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
                  {(result.columns_accessed || []).map((c) => `${c.table}.${c.column}`).join(', ') || '—'}
                </div>
              </div>
            </div>

            {/* Explanation */}
            {result.explanation && (
              <div style={{ marginTop: 16 }}>
                <div className="sidebar-section-label">Query Explanation</div>
                <div style={{ fontSize: '0.85rem', lineHeight: 1.6, color: 'var(--text-secondary)' }}>
                  {result.explanation}
                </div>
              </div>
            )}

            {/* Risk */}
            {result.risk_warning && (
              <div style={{ marginTop: 12 }}>
                <RiskBanner riskLevel={result.risk_level || 'safe'} riskWarning={result.risk_warning} />
              </div>
            )}
          </div>
        )}

        {/* Confidence */}
        {activeTab === 2 && (
          <div className="card">
            {result.confidence && Object.keys(result.confidence).length > 0 ? (
              <ConfidenceChart confidence={result.confidence} />
            ) : (
              <div style={{ color: 'var(--text-tertiary)', fontSize: '0.85rem' }}>
                No confidence data available.
              </div>
            )}
          </div>
        )}

        {/* Verification */}
        {activeTab === 3 && (
          <div className="card">
            <div className="sidebar-section-label">SQL-to-Question Back-Translation</div>
            <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginBottom: 12 }}>
              The generated SQL was sent back to the LLM asking "What question does this SQL answer?" — the result is compared to your original question.
            </div>

            <div className="verification-block">
              <div className="verification-question">
                <div className="sidebar-section-label">Back-Translated Question</div>
                <div style={{ fontStyle: 'italic', color: 'var(--text-primary)', fontSize: '0.88rem' }}>
                  "{result.back_translated_question || ''}"
                </div>
              </div>
              <div className="verification-score">
                <div className="verification-score-value">
                  {((result.alignment_score || 0) * 100).toFixed(0)}%
                </div>
                <div className="verification-score-label">
                  {result.alignment_label || ''}
                </div>
              </div>
            </div>

            {result.judge_reason && (
              <div style={{ fontSize: '0.82rem', color: 'var(--text-tertiary)', marginTop: 8 }}>
                {result.judge_reason}
              </div>
            )}

            {result.alignment_flagged && (
              <div className="anomaly-item anomaly-warning" style={{ marginTop: 8 }}>
                <AlertTriangle style={{ width: 12, height: 12, display: 'inline', verticalAlign: 'middle' }} />
                {' '}LOW ALIGNMENT — SQL may not correctly answer the intended question.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
