export default function ConfidenceChart({ confidence }) {
  if (!confidence) return null;

  const grade = confidence.grade || '?';
  const composite = confidence.composite_score || 0;
  const verdict = confidence.verdict || '';
  const signals = confidence.signal_breakdown || {};

  const items = [
    { key: 'syntax_validity', label: 'Syntax Validity', max: 20 },
    { key: 'back_translation', label: 'Back-Translation', max: 35 },
    { key: 'sanity_pass_rate', label: 'Sanity Checks', max: 30 },
    { key: 'schema_coverage', label: 'Schema Coverage', max: 15 },
  ];

  const gradeColors = {
    A: 'var(--success)',
    B: 'var(--warning)',
    C: 'var(--warning)',
    D: 'var(--error)',
  };

  return (
    <div>
      <div className="confidence-overview">
        <div className="confidence-score">{(composite * 100).toFixed(0)}%</div>
        <div>
          <span
            className="badge"
            style={{
              background: gradeColors[grade] || 'var(--text-tertiary)',
              color: 'white',
              fontSize: '0.82rem',
              padding: '4px 12px',
            }}
          >
            Grade {grade}
          </span>
          {verdict && <div className="confidence-verdict" style={{ marginTop: 4 }}>{verdict}</div>}
        </div>
      </div>

      <div className="confidence-bar-group">
        {items.map(({ key, label, max }) => {
          const raw = signals[key] || 0;
          const earned = Math.round(raw * max);
          const pct = Math.round(raw * 100);
          const barClass = raw >= 0.7 ? 'bar-good' : raw >= 0.4 ? 'bar-fair' : 'bar-poor';

          return (
            <div className="confidence-bar-item" key={key}>
              <div className="confidence-bar-label">{label}</div>
              <div className="confidence-bar-track">
                <div
                  className={`confidence-bar-fill ${barClass}`}
                  style={{ width: `${Math.max(pct, 12)}%` }}
                >
                  {earned}/{max}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Signal detail table */}
      <div style={{ marginTop: 16 }}>
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Signal</th>
                <th>Raw Score</th>
                <th>Weight</th>
                <th>Points</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map(({ key, label, max }) => {
                const raw = signals[key] || 0;
                const earned = Math.round(raw * max);
                let status, statusCls;
                if (raw >= 0.7) { status = 'Good'; statusCls = 'badge-success'; }
                else if (raw >= 0.4) { status = 'Fair'; statusCls = 'badge-warning'; }
                else { status = 'Poor'; statusCls = 'badge-error'; }

                return (
                  <tr key={key}>
                    <td>{label}</td>
                    <td>{(raw * 100).toFixed(0)}%</td>
                    <td>{max}%</td>
                    <td>{earned} / {max}</td>
                    <td><span className={`badge ${statusCls}`}>{status}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
