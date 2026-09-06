import { useState } from 'react';
import { CheckCircle, AlertCircle, ShieldAlert, Copy } from 'lucide-react';
import { apiValidateSql } from '../api';
import useStore from '../store';

export default function SqlEditor({ sql, onExecute, onCancel, msgIdx }) {
  const [editedSql, setEditedSql] = useState(sql);
  const [validating, setValidating] = useState(false);
  const [validationResult, setValidationResult] = useState(null);
  const activeConnectionId = useStore((s) => s.activeConnectionId);

  const handleValidate = async () => {
    setValidating(true);
    const [data, err] = await apiValidateSql(editedSql, activeConnectionId);
    setValidating(false);
    if (err) {
      setValidationResult({
        is_valid: false,
        issues: [err],
        suggestions: '',
        corrected_sql: null,
        risk_assessment: 'safe',
      });
    } else {
      setValidationResult(data);
    }
  };

  const handleApplyCorrection = (corrected) => {
    setEditedSql(corrected);
    setValidationResult(null);
  };

  return (
    <div>
      <div className="sidebar-section-label" style={{ marginBottom: 6 }}>
        Edit SQL
      </div>
      <textarea
        className="input-field"
        value={editedSql}
        onChange={(e) => setEditedSql(e.target.value)}
        rows={8}
        style={{ width: '100%', marginBottom: 8 }}
      />

      <div className="chat-actions">
        <button className="btn btn-secondary btn-sm" onClick={handleValidate} disabled={validating}>
          {validating ? 'Validating...' : 'Validate'}
        </button>
        <button
          className="btn btn-primary btn-sm"
          onClick={() => onExecute(editedSql)}
        >
          Execute
        </button>
        <button className="btn btn-ghost btn-sm" onClick={onCancel}>
          Cancel
        </button>
      </div>

      {validationResult && (
        <div style={{ marginTop: 8 }}>
          {validationResult.is_valid ? (
            <div className="validation-pass">
              <CheckCircle style={{ width: 14, height: 14 }} />
              <span><strong>SQL is valid</strong> — All checks passed.</span>
            </div>
          ) : (
            <div className="validation-fail">
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <AlertCircle style={{ width: 14, height: 14 }} />
                <strong>Issues found:</strong>
              </div>
              <ul>
                {(validationResult.issues || []).map((issue, i) => (
                  <li key={i}>{issue}</li>
                ))}
              </ul>
            </div>
          )}

          {validationResult.suggestions && (
            <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: 4 }}>
              {validationResult.suggestions}
            </div>
          )}

          {validationResult.corrected_sql && (
            <div className="card" style={{ marginTop: 8 }}>
              <div className="sidebar-section-label">Suggested correction</div>
              <div className="chat-sql">
                <pre>{validationResult.corrected_sql}</pre>
              </div>
              <button
                className="btn btn-secondary btn-sm mt-2"
                onClick={() => handleApplyCorrection(validationResult.corrected_sql)}
              >
                <Copy style={{ width: 12, height: 12 }} />
                Apply correction
              </button>
            </div>
          )}

          {validationResult.risk_assessment && validationResult.risk_assessment !== 'safe' && (
            <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.78rem' }}>
              <ShieldAlert style={{ width: 12, height: 12, color: 'var(--warning)' }} />
              <span style={{ color: 'var(--text-tertiary)' }}>
                Risk: <strong>{validationResult.risk_assessment}</strong>
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
