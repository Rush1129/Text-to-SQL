import { useState, useEffect } from 'react';
import {
  Database, MessageSquare, BarChart3, ClipboardList,
  LogOut, Sun, Moon, Settings, Plug, ChevronDown, ChevronUp,
  Trash2, Link, Layers,
} from 'lucide-react';
import SchemaExplorer from './SchemaExplorer';
import useStore from '../store';
import { apiListConnections, apiConnectDb, apiDeleteConnection } from '../api';

export default function Sidebar() {
  const [connectionsOpen, setConnectionsOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [addDbOpen, setAddDbOpen] = useState(false);

  // Connection form state
  const [cName, setCName] = useState('');
  const [cHost, setCHost] = useState('localhost');
  const [cPort, setCPort] = useState('5432');
  const [cDb, setCDb] = useState('');
  const [cUser, setCUser] = useState('postgres');
  const [cPass, setCPass] = useState('');
  const [connectErr, setConnectErr] = useState('');
  const [connecting, setConnecting] = useState(false);

  const theme = useStore((s) => s.theme);
  const toggleTheme = useStore((s) => s.toggleTheme);
  const userEmail = useStore((s) => s.userEmail);
  const userRole = useStore((s) => s.userRole);
  const logout = useStore((s) => s.logout);
  const connections = useStore((s) => s.connections);
  const setConnections = useStore((s) => s.setConnections);
  const activeConnectionId = useStore((s) => s.activeConnectionId);
  const setActiveConnectionId = useStore((s) => s.setActiveConnectionId);
  const apiBase = useStore((s) => s.apiBase);
  const setApiBase = useStore((s) => s.setApiBase);
  const sessionId = useStore((s) => s.sessionId);
  const setSessionId = useStore((s) => s.setSessionId);

  // Fetch connections on mount
  useEffect(() => {
    apiListConnections().then(([data]) => {
      if (data) setConnections(data.connections || []);
    });
  }, [setConnections]);

  const currentPage = window.location.hash.replace('#/', '') || '';

  const navItems = [
    { key: '', icon: MessageSquare, label: 'Chat' },
    { key: 'details', icon: BarChart3, label: 'Details' },
  ];

  if (userRole === 'admin') {
    navItems.push({ key: 'audit', icon: ClipboardList, label: 'Audit' });
  }

  const handleConnect = async () => {
    setConnectErr('');
    if (!cName || !cHost || !cDb || !cUser || !cPass) {
      setConnectErr('All fields are required.');
      return;
    }
    setConnecting(true);
    const [data, err] = await apiConnectDb(cName, cHost, parseInt(cPort), cDb, cUser, cPass);
    setConnecting(false);
    if (err) {
      setConnectErr(err);
    } else if (data) {
      setActiveConnectionId(data.id);
      setAddDbOpen(false);
      setCName(''); setCDb(''); setCPass('');
      // Refresh connections
      const [conns] = await apiListConnections();
      if (conns) setConnections(conns.connections || []);
    }
  };

  const handleDelete = async (connId) => {
    await apiDeleteConnection(connId);
    if (activeConnectionId === connId) setActiveConnectionId(null);
    const [conns] = await apiListConnections();
    if (conns) setConnections(conns.connections || []);
  };

  const roleIcons = { viewer: 'V', editor: 'E', admin: 'A' };

  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="sidebar-header">
        <div className="sidebar-brand">
          <Database className="sidebar-brand-icon" />
          <h2>Text-to-SQL</h2>
        </div>
        <div className="sidebar-brand-sub">Powered by Groq LLM</div>
      </div>

      {/* User card */}
      <div className="sidebar-section">
        <div className="user-card">
          <div className="user-card-avatar">
            {(userEmail || '?')[0].toUpperCase()}
          </div>
          <div className="user-card-info">
            <div className="user-card-email">{userEmail}</div>
            <div className="user-card-role">
              {roleIcons[userRole] || 'U'} · {(userRole || 'viewer').charAt(0).toUpperCase() + (userRole || 'viewer').slice(1)}
            </div>
          </div>
        </div>
      </div>

      <div className="sidebar-divider" />

      {/* Navigation */}
      <div className="sidebar-section">
        <div className="sidebar-section-label">Navigation</div>
        <div className="nav-list">
          {navItems.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${currentPage === item.key ? 'active' : ''}`}
              onClick={() => { window.location.hash = `#/${item.key}`; }}
            >
              <item.icon />
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="sidebar-divider" />

      {/* Database Connections */}
      <div className="sidebar-section">
        <button
          className="nav-item"
          style={{ justifyContent: 'space-between', marginBottom: 4 }}
          onClick={() => setConnectionsOpen(!connectionsOpen)}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Plug style={{ width: 14, height: 14 }} />
            Connections
          </span>
          {connectionsOpen ? <ChevronUp style={{ width: 12, height: 12 }} /> : <ChevronDown style={{ width: 12, height: 12 }} />}
        </button>

        {connectionsOpen && (
          <div style={{ paddingLeft: 4 }}>
            {/* Active DB selector */}
            <div className="input-group" style={{ marginBottom: 8 }}>
              <label>Active Database</label>
              <select
                className="input-field"
                value={activeConnectionId || ''}
                onChange={(e) => setActiveConnectionId(e.target.value || null)}
                style={{ fontSize: '0.78rem' }}
              >
                <option value="">Default (college_2)</option>
                {connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.connection_name} ({c.table_count || 0} tables)
                  </option>
                ))}
              </select>
            </div>

            {/* Existing connections */}
            {connections.map((c) => (
              <div className="connection-item" key={c.id}>
                <div className="connection-item-info">
                  <div className="connection-item-name">{c.connection_name}</div>
                  <div className="connection-item-host">{c.host}:{c.port}/{c.database_name}</div>
                </div>
                <button
                  className="btn btn-danger btn-sm"
                  style={{ padding: '3px 6px' }}
                  onClick={() => handleDelete(c.id)}
                  title="Delete connection"
                >
                  <Trash2 style={{ width: 12, height: 12 }} />
                </button>
              </div>
            ))}

            {/* Add new */}
            <button
              className="btn btn-secondary btn-sm btn-block mt-2"
              onClick={() => setAddDbOpen(!addDbOpen)}
            >
              <Link style={{ width: 12, height: 12 }} />
              {addDbOpen ? 'Cancel' : 'Connect New Database'}
            </button>

            {addDbOpen && (
              <div className="connection-form mt-2">
                <div className="input-group">
                  <label>Connection Name</label>
                  <input className="input-field" placeholder="My Database" value={cName} onChange={(e) => setCName(e.target.value)} />
                </div>
                <div className="input-group">
                  <label>Host</label>
                  <input className="input-field" value={cHost} onChange={(e) => setCHost(e.target.value)} />
                </div>
                <div className="input-group">
                  <label>Port</label>
                  <input className="input-field" type="number" value={cPort} onChange={(e) => setCPort(e.target.value)} />
                </div>
                <div className="input-group">
                  <label>Database Name</label>
                  <input className="input-field" placeholder="my_database" value={cDb} onChange={(e) => setCDb(e.target.value)} />
                </div>
                <div className="input-group">
                  <label>Username</label>
                  <input className="input-field" value={cUser} onChange={(e) => setCUser(e.target.value)} />
                </div>
                <div className="input-group">
                  <label>Password</label>
                  <input className="input-field" type="password" value={cPass} onChange={(e) => setCPass(e.target.value)} />
                </div>
                {connectErr && <div className="auth-error" style={{ fontSize: '0.75rem' }}>{connectErr}</div>}
                <button className="btn btn-primary btn-sm btn-block" onClick={handleConnect} disabled={connecting}>
                  {connecting ? 'Connecting...' : 'Connect & Extract Schema'}
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="sidebar-divider" />

      {/* Schema Explorer */}
      <div className="sidebar-section">
        <button
          className="nav-item"
          style={{ justifyContent: 'space-between', marginBottom: 4 }}
          onClick={() => setSchemaOpen(!schemaOpen)}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Layers style={{ width: 14, height: 14 }} />
            Schema Explorer
          </span>
          {schemaOpen ? <ChevronUp style={{ width: 12, height: 12 }} /> : <ChevronDown style={{ width: 12, height: 12 }} />}
        </button>
        {schemaOpen && (
          <div style={{ paddingLeft: 4 }}>
            <SchemaExplorer />
          </div>
        )}
      </div>

      <div className="sidebar-divider" />

      {/* Settings */}
      <div className="sidebar-section">
        <button
          className="nav-item"
          style={{ justifyContent: 'space-between', marginBottom: 4 }}
          onClick={() => setSettingsOpen(!settingsOpen)}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Settings style={{ width: 14, height: 14 }} />
            Settings
          </span>
          {settingsOpen ? <ChevronUp style={{ width: 12, height: 12 }} /> : <ChevronDown style={{ width: 12, height: 12 }} />}
        </button>
        {settingsOpen && (
          <div style={{ paddingLeft: 4 }}>
            <div className="input-group" style={{ marginBottom: 8 }}>
              <label>API Base URL</label>
              <input
                className="input-field"
                value={apiBase}
                onChange={(e) => setApiBase(e.target.value)}
                placeholder="http://localhost:8000"
                style={{ fontSize: '0.78rem' }}
              />
            </div>
            <div className="input-group">
              <label>Session ID</label>
              <input
                className="input-field"
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
                placeholder="default"
                style={{ fontSize: '0.78rem' }}
              />
            </div>
          </div>
        )}
      </div>

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Bottom section */}
      <div className="sidebar-divider" />
      <div className="sidebar-section">
        {/* Theme toggle */}
        <div className="theme-toggle">
          <span className="theme-toggle-label" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {theme === 'dark' ? <Moon style={{ width: 14, height: 14 }} /> : <Sun style={{ width: 14, height: 14 }} />}
            {theme === 'dark' ? 'Dark mode' : 'Light mode'}
          </span>
          <button
            className={`theme-toggle-switch ${theme === 'dark' ? 'on' : ''}`}
            onClick={toggleTheme}
            aria-label="Toggle theme"
          />
        </div>

        {/* Logout */}
        <button
          className="btn btn-ghost btn-sm btn-block mt-2"
          onClick={logout}
          style={{ justifyContent: 'flex-start' }}
        >
          <LogOut style={{ width: 14, height: 14 }} />
          Logout
        </button>
      </div>
    </aside>
  );
}
