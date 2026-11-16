import React, { useState, useEffect, useCallback, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  Send,
  RefreshCw,
  Lock,
  LogOut,
  Terminal,
  Server,
  ListTodo,
  Trash2,
  Plus,
  MessageCircle,
  Copy,
  Check,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react';
import './style.css';

const UI_VERSION =
  typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '0.2.0';
const UI_BUILD_TIME =
  typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : 'dev';

const DEFAULT_PROVIDERS = [
  {
    id: 'codex',
    display_name: 'Codex CLI',
    default_model_alias: 'codex-default',
    capabilities: {
      modes: ['chat', 'agent'],
      default_mode: 'chat',
      supports_system_prompt: true,
      supports_sessions: true,
      supports_session_history: true,
      supports_session_delete: true,
      supports_usage_quota: true,
      supports_model_discovery: true,
    },
  },
  {
    id: 'agy',
    display_name: 'Antigravity CLI (agy)',
    default_model_alias: 'agy-default',
    capabilities: {
      modes: ['chat', 'agent'],
      default_mode: 'chat',
      supports_system_prompt: true,
      supports_sessions: true,
      supports_session_history: true,
      supports_session_delete: true,
      supports_usage_quota: true,
      supports_model_discovery: true,
    },
  },
];

function formatModeLabel(providerId, mode) {
  if (providerId === 'agy') {
    if (mode === 'chat') return 'Chat · slash & skill expansion off';
    if (mode === 'agent') return 'Normal · full CLI behavior';
  }
  if (mode === 'chat') return 'Chat · lightweight';
  if (mode === 'agent') return 'Agent · full config';
  return mode;
}

const TABS = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'tester', label: 'Chat Tester', icon: Send },
  { id: 'sessions', label: 'Sessions', icon: Server },
  { id: 'jobs', label: 'Jobs', icon: ListTodo },
];

const HASH_TO_TAB = {
  '#dashboard': 'overview',
  '#overview': 'overview',
  '#tester': 'tester',
  '#sessions': 'sessions',
  '#jobs': 'jobs',
};

function tabFromHash() {
  return HASH_TO_TAB[window.location.hash] || 'overview';
}

function formatDate(value) {
  if (!value) return '—';
  const ts = typeof value === 'number' && value < 1e12 ? value * 1000 : value;
  return new Date(ts).toLocaleString();
}

function UsageWindow({ title, windowData }) {
  if (!windowData) return null;
  const used = windowData.used_percent;
  const pct = used == null ? 0 : Math.min(100, Math.max(0, used));
  const barClass = pct >= 90 ? 'high' : pct >= 75 ? 'warn' : '';

  return (
    <article className="usage-window">
      <div>
        <span>{title}</span>
        <strong>
          {used == null ? '—' : `${Math.max(0, Math.round(100 - used))}% left`}
        </strong>
      </div>
      <div className="usage-track">
        <i className={barClass} style={{ width: `${pct}%` }} />
      </div>
      <small>
        {used == null ? 'Usage unavailable' : `${Math.round(used)}% used`}
        {windowData.window_label
          ? ` · ${windowData.window_label}`
          : windowData.window_duration_mins
            ? ` · ${Math.round(windowData.window_duration_mins / 60)}h window`
            : ''}
        {windowData.resets_at ? ` · resets ${formatDate(windowData.resets_at)}` : ''}
      </small>
    </article>
  );
}

function App() {
  const [token, setToken] = useState(sessionStorage.getItem('gatewayToken') || '');
  const [draft, setDraft] = useState('');
  const [activeTab, setActiveTab] = useState(tabFromHash);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem('codexGateway.sidebarCollapsed') === '1';
    } catch {
      return false;
    }
  });

  const [overview, setOverview] = useState(null);
  const [providers, setProviders] = useState(DEFAULT_PROVIDERS);
  const [usageByProvider, setUsageByProvider] = useState({});
  const [usageLoading, setUsageLoading] = useState({});
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [refreshingAll, setRefreshingAll] = useState(false);

  const [error, setError] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [prompt, setPrompt] = useState('');
  const [sending, setSending] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const [modelCatalog, setModelCatalog] = useState([]);
  const [provider, setProvider] = useState('codex');
  const [selectedModel, setSelectedModel] = useState('codex-default');
  const [modeByProvider, setModeByProvider] = useState({});
  const [lastUsage, setLastUsage] = useState(null);

  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionFilter, setSessionFilter] = useState('all');
  const [sessionQuery, setSessionQuery] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [saveSession, setSaveSession] = useState(false);
  const [copiedSessionId, setCopiedSessionId] = useState(false);

  const [savedSystemPrompts, setSavedSystemPrompts] = useState(() => {
    try {
      const items = JSON.parse(
        localStorage.getItem('codexGateway.savedSystemPrompts') || '[]'
      );
      return Array.isArray(items) ? items : [];
    } catch {
      return [];
    }
  });
  const [selectedSystemPromptId, setSelectedSystemPromptId] = useState('');
  const [systemPromptName, setSystemPromptName] = useState('');

  const chatTranscriptRef = useRef(null);
  const providersRef = useRef(providers);
  providersRef.current = providers;

  const providerMap = Object.fromEntries(providers.map((p) => [p.id, p]));
  const getProviderLabel = (id) => providerMap[id]?.display_name || id;
  const currentProvider = providerMap[provider] || providers[0];
  const currentCaps = currentProvider?.capabilities || {};
  const currentModes = currentCaps.modes?.length ? currentCaps.modes : ['chat'];
  const currentMode =
    modeByProvider[provider] ||
    (currentModes.includes(currentCaps.default_mode) ? currentCaps.default_mode : currentModes[0]);
  const usageProviders = providers.filter((p) => p.capabilities?.supports_usage_quota !== false);

  const navigate = useCallback((tab) => {
    setActiveTab(tab);
    setError('');
    const targetHash = tab === 'overview' ? '#dashboard' : `#${tab}`;
    if (window.location.hash !== targetHash) {
      window.history.replaceState(null, '', targetHash);
    }
  }, []);

  useEffect(() => {
    const onHashChange = () => setActiveTab(tabFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const request = useCallback(
    async (path, options = {}) => {
      const response = await fetch(path, {
        ...options,
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
          ...(options.headers || {}),
        },
      });
      const body = await response
        .json()
        .catch(() => ({ detail: response.statusText }));
      if (!response.ok) {
        throw new Error(
          typeof body.detail === 'string'
            ? body.detail
            : JSON.stringify(body.detail || body)
        );
      }
      return body;
    },
    [token]
  );

  const refreshOverview = useCallback(async () => {
    if (!token) return;
    setOverviewLoading(true);
    try {
      const data = await request('/admin/overview');
      setOverview(data);
      setError('');
    } catch (e) {
      setError(e.message);
    } finally {
      setOverviewLoading(false);
    }
  }, [token, request]);

  const refreshProviders = useCallback(async () => {
    if (!token) return DEFAULT_PROVIDERS;
    try {
      const result = await request('/v1/providers');
      const items = Array.isArray(result.data) && result.data.length > 0 ? result.data : DEFAULT_PROVIDERS;
      setProviders(items);
      setProvider((current) => {
        if (items.some((item) => item.id === current)) return current;
        const next = items[0].id;
        setSelectedModel(`${next}-default`);
        return next;
      });
      return items;
    } catch {
      return DEFAULT_PROVIDERS;
    }
  }, [token, request]);

  const refreshUsage = useCallback(
    async (target = 'all', providerList) => {
      if (!token) return;
      const activeProviders = providerList || providersRef.current;
      const targets =
        target === 'all'
          ? activeProviders
              .filter((p) => p.capabilities?.supports_usage_quota !== false)
              .map((p) => p.id)
          : [target];
      if (!targets.length) return;
      setUsageLoading((prev) => {
        const next = { ...prev };
        for (const cli of targets) next[cli] = true;
        return next;
      });

      await Promise.all(
        targets.map(async (cli) => {
          try {
            const data = await request(`/admin/usage?provider=${encodeURIComponent(cli)}`);
            setUsageByProvider((prev) => ({ ...prev, [cli]: data }));
          } catch (e) {
            setUsageByProvider((prev) => ({ ...prev, [cli]: { error: e.message } }));
          } finally {
            setUsageLoading((prev) => ({ ...prev, [cli]: false }));
          }
        })
      );
    },
    [token, request]
  );

  const refreshModels = useCallback(
    async (targetProvider = provider) => {
      if (!token) return;
      try {
        const models = await request(`/v1/models?provider=${targetProvider}`);
        setModelCatalog(models.data || []);
      } catch (e) {
        setModelCatalog([]);
        setError((prev) => prev || `Could not load ${targetProvider} models: ${e.message}`);
      }
    },
    [token, request, provider]
  );

  const refreshSessions = useCallback(async () => {
    if (!token) return;
    setSessionsLoading(true);
    try {
      const result = await request('/admin/sessions');
      setSessions(result.sessions || []);
    } catch (e) {
      setError((prev) => prev || `Could not load sessions: ${e.message}`);
    } finally {
      setSessionsLoading(false);
    }
  }, [token, request]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    refreshProviders().then((items) => {
      if (!cancelled) refreshUsage('all', items);
    });
    refreshOverview();
    refreshSessions();

    const overviewTimer = setInterval(refreshOverview, 15000);
    const usageTimer = setInterval(() => refreshUsage('all'), 60000);
    const sessionTimer = setInterval(refreshSessions, 60000);
    return () => {
      cancelled = true;
      clearInterval(overviewTimer);
      clearInterval(usageTimer);
      clearInterval(sessionTimer);
    };
  }, [token, refreshProviders, refreshOverview, refreshUsage, refreshSessions]);

  useEffect(() => {
    if (!token) return;
    if (!sessionId) {
      setSelectedModel(`${provider}-default`);
    }
    setModelCatalog([]);
    refreshModels(provider);
  }, [token, provider, sessionId, refreshModels]);

  useEffect(() => {
    const panel = chatTranscriptRef.current;
    if (panel) {
      panel.scrollTop = panel.scrollHeight;
    }
  }, [chatMessages, sending, historyLoading, activeTab]);

  const login = () => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    sessionStorage.setItem('gatewayToken', trimmed);
    setToken(trimmed);
    setDraft('');
  };

  const logout = () => {
    sessionStorage.removeItem('gatewayToken');
    setToken('');
    setOverview(null);
    setUsageByProvider({});
    setSessions([]);
    setChatMessages([]);
    setSessionId('');
    setSaveSession(false);
    setLastUsage(null);
    setError('');
  };

  const startSession = () => {
    setSessionId('');
    setSaveSession(true);
    setChatMessages([]);
    setPrompt('');
    setLastUsage(null);
    setError('');
  };

  const newChat = () => {
    setSessionId('');
    setSaveSession(false);
    setChatMessages([]);
    setPrompt('');
    setLastUsage(null);
    setError('');
  };

  const persistSystemPrompts = (items) => {
    setSavedSystemPrompts(items);
    localStorage.setItem('codexGateway.savedSystemPrompts', JSON.stringify(items));
  };

  const saveSystemPrompt = (asNew = false) => {
    const name = systemPromptName.trim();
    const content = systemPrompt.trim();
    if (!name || !content) {
      setError('Enter a name and system prompt before saving.');
      return;
    }
    const id =
      !asNew && selectedSystemPromptId
        ? selectedSystemPromptId
        : String(Date.now());
    const item = { id, name, content };
    const items =
      !asNew && selectedSystemPromptId
        ? savedSystemPrompts.map((saved) => (saved.id === id ? item : saved))
        : [...savedSystemPrompts, item];
    persistSystemPrompts(items);
    setSelectedSystemPromptId(id);
    setError('');
  };

  const loadSystemPrompt = (id) => {
    setSelectedSystemPromptId(id);
    const item = savedSystemPrompts.find((saved) => saved.id === id);
    if (item) {
      setSystemPromptName(item.name);
      setSystemPrompt(item.content);
    } else {
      setSystemPromptName('');
      setSystemPrompt('');
    }
  };

  const deleteSystemPrompt = () => {
    if (!selectedSystemPromptId) return;
    persistSystemPrompts(
      savedSystemPrompts.filter((item) => item.id !== selectedSystemPromptId)
    );
    loadSystemPrompt('');
  };

  const sendMessage = async () => {
    const userMessage = prompt.trim();
    if (!userMessage || sending) return;

    setSending(true);
    setLastUsage(null);
    setError('');

    const updatedMessages = [...chatMessages, { role: 'user', content: userMessage }];
    const displayMessages = sessionId
      ? updatedMessages
      : updatedMessages.slice(-10);
    setChatMessages(displayMessages);
    setPrompt('');

    try {
      const result = await request('/v1/chat/completions', {
        method: 'POST',
        body: JSON.stringify({
          provider,
          model: selectedModel,
          messages: sessionId
            ? [{ role: 'user', content: userMessage }]
            : updatedMessages.slice(-10),
          stream: false,
          session_id: currentCaps.supports_sessions !== false && saveSession ? sessionId || null : null,
          store_session: currentCaps.supports_sessions !== false && saveSession && !sessionId,
          system_prompt: currentCaps.supports_system_prompt !== false ? systemPrompt.trim() || null : null,
          gateway_options: {
            mode: currentMode,
            max_history_messages: 10,
          },
        }),
      });
      const reply = result.choices?.[0]?.message?.content || '(empty)';
      setLastUsage(result.usage || null);
      setChatMessages((current) => {
        const next = [...current, { role: 'assistant', content: reply }];
        return sessionId || result.session_id ? next : next.slice(-10);
      });
      if (result.session_id) {
        setSessionId(result.session_id);
        setSaveSession(true);
      }
      await Promise.all([refreshOverview(), refreshSessions()]);
    } catch (e) {
      setError(e.message);
      setPrompt(userMessage);
      setChatMessages((current) => current.slice(0, -1));
    } finally {
      setSending(false);
    }
  };

  const deleteSession = async (session) => {
    const label = getProviderLabel(session.provider || 'codex');
    if (
      !window.confirm(
        `ลบ session ${label} นี้พร้อมประวัติข้อความถาวรใช่ไหมคะ?`
      )
    ) {
      return;
    }
    try {
      const { id } = session;
      await request(
        `/admin/sessions/${encodeURIComponent(id)}?mode=${session.mode || 'agent'}&provider=${session.provider || 'codex'}`,
        { method: 'DELETE' }
      );
      if (sessionId === id) {
        setSessionId('');
        setSaveSession(false);
        setChatMessages([]);
      }
      await Promise.all([refreshOverview(), refreshSessions()]);
    } catch (e) {
      setError(e.message);
    }
  };

  const continueSession = async (session) => {
    const { id, provider: sessionProvider = 'codex' } = session;
    const targetDescriptor = providerMap[sessionProvider];
    const mode =
      session.mode ||
      targetDescriptor?.capabilities?.default_mode ||
      'chat';
    const rawModel = session.model;
    const nextModel =
      !rawModel ||
      rawModel === 'default' ||
      rawModel === `${sessionProvider}-default`
        ? `${sessionProvider}-default`
        : rawModel.startsWith(`${sessionProvider}:`)
          ? rawModel
          : `${sessionProvider}:${rawModel}`;

    setProvider(sessionProvider);
    setSessionId(id);
    setSaveSession(true);
    setModeByProvider((prev) => ({ ...prev, [sessionProvider]: mode }));
    setSelectedModel(nextModel);
    setLastUsage(null);
    setChatMessages([]);
    setError('');
    setHistoryLoading(true);
    navigate('tester');

    try {
      const history = await request(
        `/admin/sessions/${encodeURIComponent(id)}?mode=${mode}&provider=${sessionProvider}`
      );
      setChatMessages(history.messages || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setHistoryLoading(false);
    }
  };

  const copySessionId = async () => {
    if (!sessionId || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(sessionId);
      setCopiedSessionId(true);
      setTimeout(() => setCopiedSessionId(false), 1800);
    } catch {
      // Ignore clipboard failures in restricted browser contexts.
    }
  };

  const toggleSidebar = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem('codexGateway.sidebarCollapsed', next ? '1' : '0');
      } catch {
        // Ignore storage failures.
      }
      return next;
    });
  };

  if (!token) {
    return (
      <main className="login">
        <div className="login-card">
          <div className="brand">
            <Terminal size={28} /> AI CLI Gateway
          </div>
          <p>Personal Management Console</p>
          <label htmlFor="gateway-token-input">Gateway API Key</label>
          <input
            id="gateway-token-input"
            type="password"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && login()}
            placeholder="Paste GATEWAY_API_KEY"
          />
          <button disabled={!draft.trim()} onClick={login}>
            <Lock size={16} /> Unlock Console
          </button>
          <small>
            Key is held only in this browser tab session. Access from localhost
            only.
          </small>
        </div>
      </main>
    );
  }

  const counts = overview?.counts || {};
  const runningCount = counts.running ?? 0;
  const anyUsageLoading = Object.values(usageLoading).some(Boolean);
  const hasSelectedInCatalog = modelCatalog.some((m) => m.id === selectedModel);

  const filteredSessions = sessions.filter((s) => {
    const itemProvider = s.provider || 'codex';
    if (sessionFilter !== 'all' && itemProvider !== sessionFilter) return false;
    const q = sessionQuery.trim().toLowerCase();
    if (!q) return true;
    return (
      s.id.toLowerCase().includes(q) ||
      (s.model || '').toLowerCase().includes(q) ||
      (s.title || '').toLowerCase().includes(q)
    );
  });

  const pageMeta = {
    overview: {
      title: 'Gateway Overview',
      subtitle: 'System health, queue metrics, and usage limits for all CLIs',
    },
    tester: {
      title: 'Chat Tester',
      subtitle: `Interactive prompt testing for ${providers.map((p) => p.display_name).join(', ')}`,
    },
    sessions: {
      title: 'Saved CLI Sessions',
      subtitle: 'Resume or delete persistent conversations stored on disk',
    },
    jobs: {
      title: 'Recent Jobs',
      subtitle: 'Recent gateway executions and Redis TTL status',
    },
  }[activeTab];

  return (
    <div className={`layout ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <aside>
        <div className="aside-head">
          <div className="brand" title="AI CLI Gateway">
            <Terminal size={24} />
            {!sidebarCollapsed && <span>AI CLI Gateway</span>}
          </div>
          <button
            type="button"
            className="sidebar-toggle"
            onClick={toggleSidebar}
            title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={!sidebarCollapsed}
          >
            {sidebarCollapsed ? (
              <PanelLeftOpen size={17} />
            ) : (
              <PanelLeftClose size={17} />
            )}
          </button>
        </div>
        {!sidebarCollapsed && <p className="aside-sub">Personal AI CLI Router</p>}
        <nav aria-label="Main navigation">
          {TABS.map(({ id, label, icon: Icon }) => {
            const badge =
              id === 'sessions' && sessions.length > 0
                ? sessions.length
                : id === 'jobs' && runningCount > 0
                  ? runningCount
                  : null;
            return (
              <button
                key={id}
                type="button"
                className={`nav-item ${activeTab === id ? 'active' : ''}`}
                onClick={() => navigate(id)}
                title={sidebarCollapsed ? label : undefined}
                aria-label={label}
              >
                <Icon size={17} />
                {!sidebarCollapsed && <span>{label}</span>}
                {badge != null && (
                  <span className="nav-badge">{badge}</span>
                )}
              </button>
            );
          })}
        </nav>
        <div className="aside-foot">
          {!sidebarCollapsed && (
            <small className="muted" title={`UI build: ${UI_BUILD_TIME}`}>
              UI v{UI_VERSION} · {UI_BUILD_TIME.slice(0, 16).replace('T', ' ')}
              {overview?.gateway_version ? ` · API v${overview.gateway_version}` : ''}
            </small>
          )}
          <button
            type="button"
            className="ghost"
            onClick={logout}
            title={sidebarCollapsed ? 'Lock Console' : undefined}
            aria-label="Lock Console"
          >
            <LogOut size={16} />
            {!sidebarCollapsed && <span>Lock Console</span>}
          </button>
        </div>
      </aside>

      <main className="content">
        <div className="content-inner">
          <header>
            <div>
              <h1>{pageMeta.title}</h1>
              <p>{pageMeta.subtitle}</p>
            </div>
            <div className="header-actions">
              {activeTab === 'overview' && (
                <>
                  <button
                    type="button"
                    className="secondary"
                    disabled={anyUsageLoading}
                    onClick={() => refreshUsage('all')}
                  >
                    <RefreshCw
                      size={15}
                      className={anyUsageLoading ? 'spin' : ''}
                    />
                    {anyUsageLoading ? 'Refreshing Usage…' : 'Refresh Usage'}
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={refreshingAll}
                    onClick={async () => {
                      setRefreshingAll(true);
                      try {
                        const items = await refreshProviders();
                        await Promise.all([
                          refreshOverview(),
                          refreshUsage('all', items),
                          refreshSessions(),
                        ]);
                      } finally {
                        setRefreshingAll(false);
                      }
                    }}
                  >
                    <RefreshCw
                      size={15}
                      className={refreshingAll ? 'spin' : ''}
                    />
                    {refreshingAll ? 'Refreshing…' : 'Refresh All'}
                  </button>
                </>
              )}
              {activeTab === 'sessions' && (
                <button
                  type="button"
                  className="secondary"
                  disabled={sessionsLoading}
                  onClick={refreshSessions}
                >
                  <RefreshCw
                    size={15}
                    className={sessionsLoading ? 'spin' : ''}
                  />
                  Refresh Sessions
                </button>
              )}
              {activeTab === 'jobs' && (
                <button
                  type="button"
                  className="secondary"
                  disabled={overviewLoading}
                  onClick={refreshOverview}
                >
                  <RefreshCw
                    size={15}
                    className={overviewLoading ? 'spin' : ''}
                  />
                  Refresh Jobs
                </button>
              )}
              <button
                type="button"
                className="secondary mobile-logout"
                onClick={logout}
                title="Lock Console"
              >
                <LogOut size={15} /> Lock
              </button>
            </div>
          </header>

          {error && (
            <div className="alert" role="alert">
              <span>{error}</span>
              <button type="button" onClick={() => setError('')}>
                Dismiss
              </button>
            </div>
          )}

          {activeTab === 'overview' && (
            <>
              <section className="stats">
                <article>
                  <span>Gateway</span>
                  <strong>{overview ? 'Connected' : 'Checking...'}</strong>
                  <small>Bearer-authenticated API</small>
                </article>
                <article
                  className="clickable"
                  onClick={() => navigate('jobs')}
                  title="View recent jobs"
                >
                  <span>Queued</span>
                  <strong>{overview?.queue_depth ?? '—'}</strong>
                  <small>Redis pending queue</small>
                </article>
                <article
                  className="clickable"
                  onClick={() => navigate('jobs')}
                  title="View active jobs"
                >
                  <span>Running</span>
                  <strong>{runningCount}</strong>
                  <small>Active CLI requests</small>
                </article>
                <article
                  className="clickable"
                  onClick={() => navigate('sessions')}
                  title="Manage saved sessions"
                >
                  <span>Saved CLI Sessions</span>
                  <strong>{sessions.length}</strong>
                  <small>{providers.map((p) => p.display_name).join(', ')}</small>
                </article>
              </section>

              {usageProviders.map((item) => {
                const usageData = usageByProvider[item.id];
                const isLoading = !!usageLoading[item.id];
                return (
                  <section className="panel usage-panel" key={item.id}>
                    <div className="section-title">
                      <h2>{item.display_name} Usage Limits</h2>
                      <div className="section-actions">
                        <span className="muted">
                          {usageData?.fetched_at
                            ? `Updated ${formatDate(usageData.fetched_at)}`
                            : 'Account status'}
                        </span>
                        <button
                          type="button"
                          className="secondary compact-btn"
                          disabled={isLoading}
                          onClick={() => refreshUsage(item.id)}
                        >
                          <RefreshCw
                            size={14}
                            className={isLoading ? 'spin' : ''}
                          />
                          Refresh
                        </button>
                      </div>
                    </div>
                    {usageData?.error ? (
                      <p className="usage-note">
                        Could not read usage limits ({usageData.error}). Check{' '}
                        {item.display_name} sign-in and refresh.
                      </p>
                    ) : usageData?.groups?.length ? (
                      <div className="usage-grid agy-usage-grid">
                        {usageData.groups.map((group) => (
                          <article className="agy-usage-group" key={group.name}>
                            <div className="agy-usage-group-title">
                              <h3>{group.name}</h3>
                              {group.description && <p>{group.description}</p>}
                            </div>
                            <div className="agy-usage-buckets">
                              {group.buckets.map((bucket) => (
                                <UsageWindow
                                  key={bucket.id || bucket.name}
                                  title={bucket.name}
                                  windowData={{
                                    used_percent:
                                      bucket.remaining_fraction == null
                                        ? null
                                        : (1 - bucket.remaining_fraction) * 100,
                                    window_label: bucket.window,
                                    resets_at: bucket.reset_time,
                                  }}
                                />
                              ))}
                            </div>
                          </article>
                        ))}
                      </div>
                    ) : usageData?.primary || usageData?.secondary ? (
                      <div className="usage-grid">
                        <UsageWindow
                          title="Current limit"
                          windowData={usageData.primary}
                        />
                        <UsageWindow
                          title="Weekly limit"
                          windowData={usageData.secondary}
                        />
                      </div>
                    ) : (
                      <p className="usage-note">
                        Loading usage limits from {item.display_name}…
                      </p>
                    )}
                  </section>
                );
              })}
            </>
          )}

          {activeTab === 'tester' && (
            <section className="panel" id="tester">
              <div className="section-title">
                <h2>Test AI CLI</h2>
                <span className="pill">{modelCatalog.length} models</span>
              </div>

              <div className="chat-toolbar">
                <div className="chat-selectors">
                  <label>
                    Provider
                    <select
                      value={provider}
                      disabled={!!sessionId}
                      onChange={(e) => {
                        const next = e.target.value;
                        setProvider(next);
                        setSelectedModel(`${next}-default`);
                        setLastUsage(null);
                      }}
                    >
                      {providers.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.display_name}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    Mode
                    <select
                      value={currentMode}
                      disabled={!!sessionId}
                      onChange={(e) =>
                        setModeByProvider((prev) => ({
                          ...prev,
                          [provider]: e.target.value,
                        }))
                      }
                    >
                      {currentModes.map((m) => (
                        <option key={m} value={m}>
                          {formatModeLabel(provider, m)}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    Model
                    <select
                      value={selectedModel}
                      onChange={(e) => setSelectedModel(e.target.value)}
                    >
                      {!hasSelectedInCatalog && selectedModel && (
                        <option value={selectedModel}>{selectedModel}</option>
                      )}
                      {modelCatalog.map((model) => (
                        <option key={model.id} value={model.id}>
                          {model.name || model.id}
                          {model.is_default ? ' (default)' : ''}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                <div className="chat-actions">
                  {currentCaps.supports_sessions !== false && (
                    <label className="session-toggle">
                      <input
                        type="checkbox"
                        checked={saveSession}
                        onChange={(e) => {
                          setSaveSession(e.target.checked);
                          if (!e.target.checked) setSessionId('');
                        }}
                      />
                      Save conversation
                    </label>
                  )}
                  <button type="button" className="secondary" onClick={newChat}>
                    <Plus size={15} /> New chat
                  </button>
                </div>
              </div>

              {provider === 'agy' && (
                <small className="composer-hint">
                  {currentMode === 'chat'
                    ? 'Chat mode disables slash-command and skill expansion. Core instructions and global/plugin rules may still add context.'
                    : 'Normal mode keeps agy slash commands and skills enabled.'}
                </small>
              )}

              {sessionId && (
                <div className="session-current">
                  <span>Continuing session</span>
                  <code>{sessionId}</code>
                  <button
                    type="button"
                    className="secondary compact-btn"
                    onClick={copySessionId}
                    title="Copy session ID"
                  >
                    {copiedSessionId ? <Check size={14} /> : <Copy size={14} />}
                    {copiedSessionId ? 'Copied' : 'Copy ID'}
                  </button>
                  <button
                    type="button"
                    className="secondary compact-btn"
                    onClick={startSession}
                  >
                    <Plus size={15} /> New saved session
                  </button>
                </div>
              )}

              {currentCaps.supports_system_prompt !== false && (
                <details
                  className="system-prompt-settings"
                  open={!!systemPrompt}
                >
                <summary>
                  System prompt{' '}
                  <span>{systemPrompt ? 'Configured' : 'Optional'}</span>
                </summary>
                <p>
                  Guide the selected CLI&apos;s role and response style. Saved
                  prompt templates stay in this browser.
                </p>
                <div className="saved-prompt-controls">
                  <select
                    aria-label="Saved system prompts"
                    value={selectedSystemPromptId}
                    onChange={(e) => loadSystemPrompt(e.target.value)}
                  >
                    <option value="">Choose a saved prompt</option>
                    {savedSystemPrompts.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                  <input
                    aria-label="Prompt name"
                    value={systemPromptName}
                    onChange={(e) => setSystemPromptName(e.target.value)}
                    placeholder="Name this prompt"
                  />
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => saveSystemPrompt(false)}
                  >
                    {selectedSystemPromptId ? 'Update prompt' : 'Save prompt'}
                  </button>
                  {selectedSystemPromptId && (
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => saveSystemPrompt(true)}
                    >
                      Save as new
                    </button>
                  )}
                  <button
                    type="button"
                    className="ghost danger"
                    disabled={!selectedSystemPromptId}
                    onClick={deleteSystemPrompt}
                    title="Delete saved prompt"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
                <textarea
                  rows="3"
                  value={systemPrompt}
                  onChange={(e) => setSystemPrompt(e.target.value)}
                  placeholder="Set the assistant's role and response style"
                />
                </details>
              )}

              <div
                className="chat-transcript"
                aria-live="polite"
                ref={chatTranscriptRef}
              >
                {historyLoading ? (
                  <p className="empty-chat">Loading conversation…</p>
                ) : chatMessages.length ? (
                  chatMessages.map((message, index) => (
                    <div
                      className={`chat-bubble ${message.role}`}
                      key={`${index}-${message.role}`}
                    >
                      <small>
                        {message.role === 'assistant'
                          ? provider.toUpperCase()
                          : 'You'}
                      </small>
                      <div>{message.content}</div>
                    </div>
                  ))
                ) : (
                  <div className="empty-chat">
                    <MessageCircle size={25} />
                    <strong>Start a conversation</strong>
                    <span>
                      Send a message to begin. Unsaved chats stay in this tab.
                    </span>
                  </div>
                )}
                {sending && (
                  <div className="chat-bubble assistant typing">
                    <small>{provider.toUpperCase()}</small>
                    <div>Thinking…</div>
                  </div>
                )}
              </div>

              <div className="chat-composer">
                <textarea
                  rows="2"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    if (
                      e.key === 'Enter' &&
                      !e.shiftKey &&
                      !e.nativeEvent.isComposing &&
                      !sending &&
                      prompt.trim()
                    ) {
                      e.preventDefault();
                      sendMessage();
                    }
                  }}
                  placeholder={`Message ${getProviderLabel(provider)}… (Enter to send, Shift+Enter for a new line)`}
                />
                <button
                  type="button"
                  disabled={sending || !prompt.trim()}
                  onClick={sendMessage}
                  aria-label="Send message"
                >
                  <Send size={17} />
                  {sending ? 'Thinking' : 'Send'}
                </button>
              </div>

              {lastUsage && (
                <small
                  className="composer-hint"
                  title="usage counters reported by the CLI"
                >
                  {provider} usage · input {lastUsage.input_tokens ?? '—'} · output{' '}
                  {lastUsage.output_tokens ?? '—'} · thinking{' '}
                  {lastUsage.thinking_tokens ?? '—'} · cache{' '}
                  {lastUsage.cache_read_tokens ?? '—'}
                </small>
              )}
              <small className="composer-hint">
                {sessionId
                  ? 'Continuing this saved session.'
                  : currentCaps.supports_sessions !== false
                    ? 'Save conversation to resume it later from Sessions.'
                    : 'This provider runs statelessly; chat context is kept in this tab.'}
              </small>
            </section>
          )}

          {activeTab === 'sessions' && (
            <section className="panel" id="sessions">
              <div className="section-title">
                <h2>Saved CLI Sessions</h2>
                <span className="muted">{filteredSessions.length} shown</span>
              </div>
              <p>
                เลือก Continue เพื่อกลับไปคุยต่อใน CLI เดิม · Antigravity
                transcript ที่ Gateway เก็บไว้แสดงได้เฉพาะข้อความที่ส่งผ่าน
                Gateway
              </p>

              <div className="filter-bar">
                <div className="filter-pills">
                  {[
                    { id: 'all', label: `All (${sessions.length})` },
                    ...Array.from(
                      new Set([
                        ...providers.map((p) => p.id),
                        ...sessions.map((s) => s.provider || 'codex'),
                      ])
                    ).map((pid) => ({
                      id: pid,
                      label: `${getProviderLabel(pid)} (${sessions.filter((s) => (s.provider || 'codex') === pid).length})`,
                    })),
                  ].map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={`filter-pill ${sessionFilter === item.id ? 'active' : ''}`}
                      onClick={() => setSessionFilter(item.id)}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                <input
                  className="search-input"
                  type="search"
                  value={sessionQuery}
                  onChange={(e) => setSessionQuery(e.target.value)}
                  placeholder="Filter by ID, model, or title…"
                />
              </div>

              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Provider</th>
                      <th>Session ID</th>
                      <th>Title</th>
                      <th>Last Updated</th>
                      <th>Model</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSessions.map((s) => (
                      <tr
                        key={`${s.provider || 'codex'}:${s.mode || ''}:${s.id}`}
                      >
                        <td>
                          {getProviderLabel(s.provider || 'codex')}
                          {s.mode ? ` · ${s.mode}` : ''}
                        </td>
                        <td title={s.id}>
                          <code>{s.id.slice(0, 8)}</code>
                        </td>
                        <td title={s.title || ''}>
                          {s.title ? s.title.slice(0, 60) : '—'}
                        </td>
                        <td>{formatDate(s.updated_at)}</td>
                        <td>{s.model || '—'}</td>
                        <td>
                          <button
                            type="button"
                            className="secondary session-action"
                            onClick={() => continueSession(s)}
                          >
                            <MessageCircle size={15} /> Continue
                          </button>{' '}
                          <button
                            type="button"
                            className="ghost danger session-action"
                            onClick={() => deleteSession(s)}
                            title="Delete session"
                          >
                            <Trash2 size={16} /> Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                    {!filteredSessions.length && (
                      <tr>
                        <td colSpan="6" className="empty">
                          {sessions.length
                            ? 'No sessions match this filter'
                            : 'No saved sessions'}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {activeTab === 'jobs' && (
            <section className="panel" id="jobs">
              <div className="section-title">
                <h2>Recent Jobs</h2>
                <span className="muted">Expires automatically</span>
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Job ID</th>
                      <th>Provider</th>
                      <th>Model</th>
                      <th>Status</th>
                      <th>Duration</th>
                      <th>Created</th>
                      <th>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(overview?.jobs || []).map((j) => (
                      <tr key={j.id}>
                        <td title={j.id}>
                          <code>{j.id.slice(0, 8)}</code>
                        </td>
                        <td>{getProviderLabel(j.provider || 'codex')}</td>
                        <td>{j.model || 'default'}</td>
                        <td>
                          <span className={`status ${j.status}`}>
                            {j.status}
                          </span>
                        </td>
                        <td>
                          {j.duration_ms != null
                            ? `${Math.round(j.duration_ms / 1000)} s`
                            : '—'}
                        </td>
                        <td>{formatDate(j.created_at)}</td>
                        <td className="error-cell" title={j.error || ''}>
                          {j.error || '—'}
                        </td>
                      </tr>
                    ))}
                    {!overview?.jobs?.length && (
                      <tr>
                        <td colSpan="7" className="empty">
                          No jobs yet
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          <footer>
            <Server size={14} /> Local-first MVP · UI v{UI_VERSION} (build{' '}
            {UI_BUILD_TIME.slice(0, 19).replace('T', ' ')}Z)
            {overview?.gateway_version
              ? ` · API v${overview.gateway_version}`
              : ''}{' '}
            · Do not expose publicly
          </footer>
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
