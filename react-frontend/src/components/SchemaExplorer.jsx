import { useState, useEffect } from 'react';
import { ChevronRight, Columns3 } from 'lucide-react';
import { apiSchema } from '../api';
import useStore from '../store';

export default function SchemaExplorer() {
  const [schema, setSchema] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState({});
  const activeConnectionId = useStore((s) => s.activeConnectionId);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    apiSchema(activeConnectionId).then(([data, err]) => {
      if (cancelled) return;
      setLoading(false);
      if (err) setError(err);
      else setSchema(data);
    });
    return () => { cancelled = true; };
  }, [activeConnectionId]);

  if (loading) return <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>Loading schema...</div>;
  if (error) return <div style={{ fontSize: '0.78rem', color: 'var(--error-text)' }}>{error}</div>;
  if (!schema) return null;

  const tables = schema.tables || {};
  const tableNames = Object.keys(tables).slice(0, 30);

  return (
    <div>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)', marginBottom: 8 }}>
        {Object.keys(tables).length} tables
      </div>
      {tableNames.map((tname) => {
        const tinfo = tables[tname];
        const cols = tinfo.columns || [];
        const isOpen = expanded[tname];

        return (
          <div className="collapsible" key={tname}>
            <button
              className={`collapsible-trigger ${isOpen ? 'open' : ''}`}
              onClick={() => setExpanded((prev) => ({ ...prev, [tname]: !prev[tname] }))}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <Columns3 style={{ width: 12, height: 12, color: 'var(--accent)' }} />
                {tname}
              </span>
              <ChevronRight />
            </button>
            {isOpen && (
              <div className="collapsible-body">
                {cols.map((c, i) => (
                  <div className="schema-column" key={i}>
                    <span className="schema-col-name">{c.name}</span>
                    <span className="schema-col-type">{c.type}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
