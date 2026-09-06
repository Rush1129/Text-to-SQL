import { useState, useEffect } from 'react';
import { ArrowLeft, CheckCircle, XCircle } from 'lucide-react';
import { apiAudit } from '../api';
import useStore from '../store';

export default function AuditPage() {
  const [records, setRecords] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const userRole = useStore((s) => s.userRole);

  useEffect(() => {
    if (userRole !== 'admin') return;
    setLoading(true);
    apiAudit(50, 0).then(([data, err]) => {
      setLoading(false);
      if (err) setError(err);
      else if (data) {
        setRecords(data.records || []);
        setTotal(data.total_records || 0);
      }
    });
  }, [userRole]);

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
        <h1>Audit Log</h1>
        <p>Full audit trail of all query executions across users and roles.</p>
      </div>

      <div className="tab-content">
        {userRole !== 'admin' ? (
          <div className="validation-fail">
            Access denied. Only admin users can view the audit log.
          </div>
        ) : loading ? (
          <div className="spinner">
            <span className="spinner-dot" />
            <span className="spinner-dot" />
            <span className="spinner-dot" />
            <span>Loading audit records...</span>
          </div>
        ) : error ? (
          <div className="validation-fail">{error}</div>
        ) : records.length === 0 ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: '0.85rem' }}>No audit records found.</div>
        ) : (
          <div className="card">
            <div className="data-table-info">
              <span>Showing {records.length} of {total} records (newest first)</span>
            </div>
            <div className="data-table-wrap" style={{ maxHeight: 500, overflowY: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>User</th>
                    <th>Role</th>
                    <th>Question</th>
                    <th>SQL</th>
                    <th>Time (ms)</th>
                    <th>Rows</th>
                    <th>Success</th>
                    <th>Risk</th>
                    <th>Permitted</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((rec, i) => (
                    <tr key={i}>
                      <td>{(rec.timestamp || '').slice(0, 19).replace('T', ' ')}</td>
                      <td style={{ maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {rec.user_id || ''}
                      </td>
                      <td>{rec.role || ''}</td>
                      <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {(rec.question || '').slice(0, 60)}
                      </td>
                      <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', fontFamily: 'JetBrains Mono, monospace', fontSize: '0.75rem' }}>
                        {(rec.safe_sql || rec.generated_sql || '').slice(0, 60)}
                      </td>
                      <td>{(rec.execution_time_ms || 0).toFixed(1)}</td>
                      <td>{rec.row_count || 0}</td>
                      <td>
                        {rec.success ? (
                          <CheckCircle style={{ width: 14, height: 14, color: 'var(--success)' }} />
                        ) : (
                          <XCircle style={{ width: 14, height: 14, color: 'var(--error)' }} />
                        )}
                      </td>
                      <td>
                        <span className={`badge ${
                          rec.risk_level === 'safe' ? 'badge-success' :
                          rec.risk_level === 'moderate' ? 'badge-warning' : 'badge-error'
                        }`}>
                          {rec.risk_level || ''}
                        </span>
                      </td>
                      <td>
                        {rec.permission_granted ? (
                          <CheckCircle style={{ width: 14, height: 14, color: 'var(--success)' }} />
                        ) : (
                          <XCircle style={{ width: 14, height: 14, color: 'var(--error)' }} />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
