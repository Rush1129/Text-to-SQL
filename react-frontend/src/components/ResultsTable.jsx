import { Download } from 'lucide-react';

function toCsv(rows) {
  if (!rows || rows.length === 0) return '';
  const headers = Object.keys(rows[0]);
  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(
      headers.map((h) => {
        const val = row[h] ?? '';
        const str = String(val);
        return str.includes(',') || str.includes('"') || str.includes('\n')
          ? `"${str.replace(/"/g, '""')}"`
          : str;
      }).join(',')
    );
  }
  return lines.join('\n');
}

export default function ResultsTable({ result }) {
  const rows = result.dataframe || result.execution_results || [];
  const rowCount = result.row_count || 0;
  const execMs = result.execution_time_ms || 0;
  const execErr = result.execution_error;

  if (execErr) {
    return (
      <div className="validation-fail">
        <strong>Execution error:</strong> {execErr}
      </div>
    );
  }

  if (!rows || rows.length === 0) {
    return (
      <div style={{ color: 'var(--text-tertiary)', fontSize: '0.85rem', padding: 12 }}>
        Query returned 0 rows.
      </div>
    );
  }

  const headers = Object.keys(rows[0]);

  const handleDownload = () => {
    const csv = toCsv(rows);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `query_results_${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="data-table-info">
        <span>
          <strong>{rowCount}</strong> row(s) returned in <strong>{execMs.toFixed(1)} ms</strong>
          {rowCount > 500 && ' (display capped at 500)'}
        </span>
        <button className="btn btn-secondary btn-sm" onClick={handleDownload}>
          <Download style={{ width: 12, height: 12 }} />
          Download CSV
        </button>
      </div>
      <div className="data-table-wrap" style={{ maxHeight: 380, overflowY: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              {headers.map((h) => (
                <th key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {headers.map((h) => (
                  <td key={h}>{row[h] != null ? String(row[h]) : ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
