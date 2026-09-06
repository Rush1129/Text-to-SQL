import { useState, useRef, useEffect } from 'react';
import { Database, SendHorizontal } from 'lucide-react';
import ChatMessage from '../components/ChatMessage';
import useStore from '../store';
import { apiQuery } from '../api';

export default function ChatPage() {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const chatEndRef = useRef(null);
  const inputRef = useRef(null);

  const messages = useStore((s) => s.messages);
  const addMessage = useStore((s) => s.addMessage);
  const sessionId = useStore((s) => s.sessionId);
  const activeConnectionId = useStore((s) => s.activeConnectionId);
  const buildConvHistory = useStore((s) => s.buildConvHistory);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const processApiResponse = (data, question) => {
    if (!data) {
      addMessage({ role: 'assistant', content: 'No response from the API.', error: true });
      return;
    }

    if (data.needs_clarification) {
      const cr = data.clarification_request || {};
      addMessage({
        role: 'assistant',
        content: cr.message || 'Please clarify.',
        needs_clarification: true,
        clarification_data: cr,
      });
      return;
    }

    if (!data.guardrail_allowed) {
      addMessage({
        role: 'assistant',
        content: data.explanation || '',
        sql: data.sql || '',
        blocked: true,
        result: data,
        original_question: question,
      });
      return;
    }

    const risk = data.risk_level || 'safe';
    const hasResults = !!(data.execution_results?.length || data.dataframe?.length);

    if (risk !== 'safe' && !hasResults) {
      addMessage({
        role: 'assistant',
        content: data.explanation || '',
        sql: data.safe_sql || data.sql || '',
        needs_confirmation: true,
        executed: false,
        result: data,
        original_question: question,
      });
    } else {
      addMessage({
        role: 'assistant',
        content: data.explanation || '',
        sql: data.safe_sql || data.sql || '',
        executed: hasResults,
        result: data,
        original_question: question,
      });
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const question = input.trim();
    if (!question) return;

    addMessage({ role: 'user', content: question });
    setInput('');
    setLoading(true);

    const convHistory = buildConvHistory();
    const [data, err] = await apiQuery(question, sessionId, false, activeConnectionId, convHistory);

    setLoading(false);

    if (err) {
      addMessage({ role: 'assistant', content: `Error: ${err}`, error: true });
    } else {
      processApiResponse(data, question);
    }
  };

  return (
    <div className="main-content">
      {/* Header */}
      <div className="page-header">
        <h1>Text-to-SQL Explorer</h1>
        <p>Ask questions in plain English — get instant SQL, results, and confidence signals.</p>
      </div>

      {/* Chat area */}
      <div className="chat-container">
        {messages.length === 0 ? (
          <div className="chat-empty">
            <Database />
            <h3>Ready to query</h3>
            <p>Type a question below to get started</p>
          </div>
        ) : (
          <>
            {messages.map((msg, i) => (
              <ChatMessage key={i} msg={msg} idx={i} />
            ))}
            {loading && (
              <div className="chat-message">
                <div className="chat-avatar assistant-avatar">
                  <div className="spinner">
                    <span className="spinner-dot" />
                    <span className="spinner-dot" />
                    <span className="spinner-dot" />
                  </div>
                </div>
                <div className="chat-bubble">
                  <div className="chat-text" style={{ color: 'var(--text-tertiary)' }}>
                    Running pipeline — generating SQL...
                  </div>
                </div>
              </div>
            )}
            <div ref={chatEndRef} />
          </>
        )}
      </div>

      {/* Input bar */}
      <form className="chat-input-bar" onSubmit={handleSubmit}>
        <div className="chat-input-wrap">
          <input
            ref={inputRef}
            className="chat-input"
            type="text"
            placeholder="Ask about your database... e.g. 'How many students per department?'"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={loading}
          />
          <button className="chat-send-btn" type="submit" disabled={loading || !input.trim()}>
            <SendHorizontal />
          </button>
        </div>
      </form>
    </div>
  );
}
