/**
 * store.js — Zustand state management
 * Manages auth, chat messages, connections, settings, and theme.
 */
import { create } from 'zustand';

function getInitialTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) return saved;
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
}

// Apply theme on load
const initialTheme = getInitialTheme();
applyTheme(initialTheme);

const useStore = create((set, get) => ({
  // ── Theme ────────────────────────────────────────────────
  theme: initialTheme,
  toggleTheme: () => {
    const next = get().theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    set({ theme: next });
  },

  // ── Auth ─────────────────────────────────────────────────
  token: localStorage.getItem('token') || null,
  userId: localStorage.getItem('userId') || null,
  userEmail: localStorage.getItem('userEmail') || null,
  userRole: localStorage.getItem('userRole') || 'viewer',
  loggedIn: !!localStorage.getItem('token'),

  setAuth: (data) => {
    localStorage.setItem('token', data.token);
    localStorage.setItem('userId', data.user_id);
    localStorage.setItem('userEmail', data.email);
    localStorage.setItem('userRole', data.role);
    set({
      token: data.token,
      userId: data.user_id,
      userEmail: data.email,
      userRole: data.role,
      loggedIn: true,
    });
  },

  logout: () => {
    localStorage.removeItem('token');
    localStorage.removeItem('userId');
    localStorage.removeItem('userEmail');
    localStorage.removeItem('userRole');
    set({
      token: null,
      userId: null,
      userEmail: null,
      userRole: 'viewer',
      loggedIn: false,
      messages: [],
      connections: [],
      activeConnectionId: null,
      detailResult: null,
    });
  },

  // ── Settings ─────────────────────────────────────────────
  apiBase: localStorage.getItem('api_base_url') || 'http://localhost:8000',
  sessionId: localStorage.getItem('session_id') || 'default',

  setApiBase: (url) => {
    localStorage.setItem('api_base_url', url);
    set({ apiBase: url });
  },

  setSessionId: (id) => {
    localStorage.setItem('session_id', id);
    set({ sessionId: id });
  },

  // ── Connections ──────────────────────────────────────────
  connections: [],
  activeConnectionId: null,

  setConnections: (conns) => set({ connections: conns }),
  setActiveConnectionId: (id) => set({ activeConnectionId: id }),

  // ── Chat Messages ────────────────────────────────────────
  messages: [],

  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),

  updateMessage: (idx, updates) =>
    set((s) => {
      const msgs = [...s.messages];
      msgs[idx] = { ...msgs[idx], ...updates };
      return { messages: msgs };
    }),

  clearMessages: () => set({ messages: [] }),

  // ── Detail result (for Details page) ─────────────────────
  detailResult: null,
  setDetailResult: (r) => set({ detailResult: r }),

  // ── Build conversation history for API context ───────────
  buildConvHistory: () => {
    const msgs = get().messages;
    const recent = msgs.slice(-20);
    return recent
      .filter((m) => m.role === 'user' || (m.role === 'assistant' && !m.error))
      .map((m) => {
        if (m.role === 'user') {
          return { role: 'user', content: m.content };
        }
        return {
          role: 'assistant',
          content: m.content || '',
          sql: m.sql || '',
          explanation: m.content || '',
        };
      });
  },
}));

export default useStore;
