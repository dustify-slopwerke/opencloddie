/* opencloddie web UI - no framework, no build step. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  me: null,
  sessions: [],
  system: null,
  activeId: null,
  view: 'sessions',
  layout: 'single',
  terminals: new Map(),
  poll: null,
  booted: false,
};

const TMUX_BUFFER_FRESH_SECONDS = 120;

const TERM_THEME = {
  background: '#07090c',
  foreground: '#d7dfe9',
  cursor: '#7ee787',
  cursorAccent: '#07090c',
  selectionBackground: '#1f4d34',
  black: '#0b0f14',
  red: '#ff7b72',
  green: '#7ee787',
  yellow: '#d29922',
  blue: '#58a6ff',
  magenta: '#bc8cff',
  cyan: '#39c5cf',
  white: '#b1bac4',
  brightBlack: '#6e7681',
  brightRed: '#ffa198',
  brightGreen: '#aff5b4',
  brightYellow: '#e3b341',
  brightBlue: '#79c0ff',
  brightMagenta: '#d2a8ff',
  brightCyan: '#56d4dd',
  brightWhite: '#f0f6fc',
};

/* ---------------------------------------------------------------- helpers */
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function quoteArg(value) {
  if (/^[A-Za-z0-9._\/:-]+$/.test(value)) return value;
  return "'" + String(value).replace(/'/g, "'\\''") + "'";
}

async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  if (method !== 'GET' && method !== 'HEAD') headers['X-OpenCloddie-Client'] = 'web';
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const contentType = response.headers.get('content-type') || '';
  let data = {};
  if (contentType.includes('application/json')) {
    try { data = await response.json(); } catch (_) { data = {}; }
  } else if (response.ok) {
    // Authelia redirects an expired browser session to its portal; fetch
    // follows it and returns HTML with status 200.
    throw new Error('Authentication expired — reload the page to sign in again.');
  }
  if (!response.ok) {
    const message = (data && data.error && data.error.message) || data.message || `${response.status} ${response.statusText}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function copyText(text, { manualAction = false, message = 'Copied to clipboard' } = {}) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) {
    ok = false;
  }
  if (!ok) {
    try {
      const area = h('textarea', { style: 'position:fixed;left:-9999px;top:0' });
      area.value = text;
      document.body.append(area);
      area.select();
      ok = document.execCommand('copy');
      area.remove();
    } catch (_) {
      ok = false;
    }
  }
  if (ok) {
    toast(
      message,
      'info',
      manualAction ? { label: 'Show text', onClick: () => showCopyFallback(text) } : null,
    );
    return true;
  }
  showCopyFallback(text);
  return false;
}

function getScreenText(entry) {
  const term = entry.term;
  const buffer = term.buffer.active;
  const start = buffer.viewportY;
  const end = Math.min(buffer.length, start + term.rows);
  const lines = [];
  for (let i = start; i < end; i += 1) {
    const line = buffer.getLine(i);
    if (line) lines.push(line.translateToString(true));
  }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return lines.join('\n');
}

function copyFromTerminal(entry) {
  if (!entry) return;
  const selection = entry.term.getSelection() || entry.selection || '';
  if (selection) {
    copyText(selection, { manualAction: true });
    return;
  }
  requestTmuxBuffer(entry, 'auto');
}

function copyScreen(entry) {
  const screen = getScreenText(entry);
  if (!screen.trim()) {
    toast('Terminal is empty', 'warn');
    return;
  }
  copyText(screen, {
    manualAction: true,
    message: 'No tmux selection — copied the visible screen',
  });
}

function requestTmuxBuffer(entry, mode) {
  if (!entry || entry.disposed) return;
  if (!entry.ws || entry.ws.readyState !== WebSocket.OPEN) {
    copyScreen(entry);
    return;
  }
  if (entry.pendingBuffer && mode === 'auto') return;
  if (entry.bufferTimer) clearTimeout(entry.bufferTimer);
  entry.pendingBuffer = { mode };
  entry.ws.send(JSON.stringify({ t: 'get_tmux_buffer' }));
  entry.bufferTimer = setTimeout(() => {
    const pending = entry.pendingBuffer;
    entry.pendingBuffer = null;
    entry.bufferTimer = null;
    if (!pending) return;
    copyScreen(entry);
  }, 4000);
}

function handleTmuxBuffer(entry, message) {
  const pending = entry.pendingBuffer;
  entry.pendingBuffer = null;
  if (entry.bufferTimer) {
    clearTimeout(entry.bufferTimer);
    entry.bufferTimer = null;
  }
  if (!pending) return;
  const mode = pending.mode;
  if (message.error) {
    copyScreen(entry);
    return;
  }
  let text = '';
  try {
    const raw = atob(message.data || '');
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    text = new TextDecoder().decode(bytes);
  } catch (_) {
    text = '';
  }
  if (!text) {
    copyScreen(entry);
    return;
  }
  if (mode === 'auto') {
    const created = Number(message.created || 0);
    const fresh = created > 0 && (Date.now() / 1000 - created) <= TMUX_BUFFER_FRESH_SECONDS;
    if (!fresh) {
      copyScreen(entry);
      return;
    }
    copyText(text, { manualAction: true, message: 'Copied recent tmux selection' });
    return;
  }
  copyText(text, { manualAction: true, message: 'Copied tmux buffer' });
}

function showCopyFallback(text, title = 'Copy manually') {
  const area = h('textarea', {
    class: 'input',
    style: 'width:100%;height:180px;font-family:var(--mono);resize:vertical',
  });
  area.value = text;
  area.readOnly = true;
  openModal({
    title,
    body: h('div', {},
      h('p', { class: 'muted', text: 'Select the text below and copy it, or download it.' }),
      area),
    actions: [
      {
        label: 'Download',
        class: 'btn',
        onClick: () => {
          const blob = new Blob([text], { type: 'text/plain' });
          const link = document.createElement('a');
          link.href = URL.createObjectURL(blob);
          link.download = 'opencloddie-copy.txt';
          link.click();
          setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        },
      },
      { label: 'Close', class: 'btn-primary' },
    ],
  });
  setTimeout(() => { area.focus(); area.select(); }, 50);
}

function toast(message, kind = 'info', action = null) {
  const node = h('div', { class: `toast ${kind === 'info' ? '' : kind}`.trim() });
  if (action) {
    node.style.display = 'flex';
    node.style.alignItems = 'center';
    node.style.gap = '10px';
  }
  node.append(h('span', { text: message }));
  if (action) {
    node.append(h('button', {
      class: 'btn btn-sm',
      text: action.label,
      onclick: () => { node.remove(); action.onClick(); },
    }));
  }
  $('#toasts').append(node);
  const ttl = action ? 9000 : (kind === 'error' ? 6000 : 3200);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transform = 'translateY(6px)';
    node.style.transition = 'opacity .2s, transform .2s';
    setTimeout(() => node.remove(), 220);
  }, ttl);
}

function fmtAge(iso) {
  if (!iso) return 'never';
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return '—';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/* ---------------------------------------------------------------- modal */
let modalResolver = null;

function closeModal() {
  $('#modal-root').hidden = true;
  clear($('#modal-body'));
  clear($('#modal-actions'));
  modalResolver = null;
}

function openModal({ title, body, actions = [], dismissible = true }) {
  $('#modal-title').textContent = title;
  const bodyNode = $('#modal-body');
  clear(bodyNode);
  if (body instanceof Node) bodyNode.append(body);
  else if (typeof body === 'string') bodyNode.innerHTML = body;

  const actionNode = $('#modal-actions');
  clear(actionNode);
  for (const action of actions) {
    const button = h('button', {
      class: `btn ${action.class || ''}`.trim(),
      text: action.label,
      onclick: async () => {
        if (action.onClick) {
          const keep = await action.onClick();
          if (keep === false) return;
        }
        if (action.close !== false) closeModal();
      },
    });
    actionNode.append(button);
  }
  $('#modal-root').hidden = false;
  $('#modal-x').style.visibility = dismissible ? 'visible' : 'hidden';
  const focusable = bodyNode.querySelector('input, select, textarea, button');
  if (focusable) setTimeout(() => focusable.focus(), 30);
}

function confirmDialog(title, message, { confirmLabel = 'Confirm', danger = false } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (!settled) { settled = true; resolve(value); } };
    openModal({
      title,
      body: h('p', { class: 'muted', text: message }),
      actions: [
        { label: 'Cancel', class: 'btn-ghost', onClick: () => finish(false) },
        {
          label: confirmLabel,
          class: danger ? 'btn-danger' : 'btn-primary',
          onClick: () => { finish(true); },
        },
      ],
    });
    const observer = new MutationObserver(() => {
      if ($('#modal-root').hidden) { observer.disconnect(); finish(false); }
    });
    observer.observe($('#modal-root'), { attributes: true, attributeFilter: ['hidden'] });
  });
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  try {
    state.me = await api('/api/me');
  } catch (err) {
    const boot = $('#booting');
    clear(boot);
    boot.append(
      h('div', { class: 'empty-card', style: 'max-width:520px' },
        h('h2', { text: 'Cannot load opencloddie' }),
        h('p', { class: 'muted', text: err.status === 401
          ? 'You are not authenticated. Access this page through nginx + Authelia.'
          : err.message }),
        h('div', { class: 'empty-actions' },
          h('button', { class: 'btn btn-primary', text: 'Retry', onclick: () => location.reload() }))),
    );
    return;
  }
  renderIdentity();
  renderModelNotes();
  renderInstall();
  state.layout = safeStorageGet('opencloddie.layout') === 'tile' ? 'tile' : 'single';
  // Phones keep a single terminal; the toggle is hidden at this width.
  if (window.matchMedia('(max-width: 520px)').matches) state.layout = 'single';
  applyLayoutChrome();
  await loadSessions();
  await loadSystem();
  setView('sessions');
  $('#booting').hidden = true;
  $('#app').hidden = false;
  state.booted = true;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  $('#status-hint').textContent = isMac ? '⌘C copy · ⌘V paste' : 'Ctrl+Insert copy · Shift+Insert paste';
  setTimeout(() => {
    if (state.layout === 'tile') {
      layoutTiles();
    } else {
      const entry = state.terminals.get(state.activeId);
      if (entry) fitTerminal(entry);
    }
  }, 60);
  state.poll = setInterval(() => {
    if (!document.hidden) loadSessions({ quiet: true });
  }, 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadSessions({ quiet: true });
  });
}

function renderIdentity() {
  const user = state.me.user || {};
  const name = user.name || user.username || 'user';
  $('#user-name').textContent = name;
  $('#user-sub').textContent = user.email || 'signed in via Authelia';
  $('#avatar').textContent = (name[0] || '?').toUpperCase();
  $('#fact-user').textContent = user.username || '—';
  $('#fact-email').textContent = user.email || '—';
  $('#fact-web').textContent = (state.me.urls || {}).web || '—';
  $('#fact-api').textContent = (state.me.urls || {}).api || '—';
}

function renderModelNotes() {
  const root = $('#model-notes');
  clear(root);
  for (const model of state.me.models || []) {
    const detail = model.context_tokens
      ? `${fmtContext(model.context_tokens)} context`
      : 'context window not published';
    root.append(
      h('div', { class: 'model-note' },
        h('code', { text: model.id }),
        h('span', { text: `${model.label} · ${detail}${model.note ? ' · ' + model.note : ''}` })),
    );
  }
}

function fmtContext(tokens) {
  return tokens >= 1000000 ? `${(tokens / 1000000).toLocaleString()}M` : `${Math.round(tokens / 1000)}K`;
}

function renderInstall() {
  const install = (state.me.install || {}).command || '';
  const login = (state.me.install || {}).login || '';
  $('#install-command').textContent = install;
  $('#login-command').textContent = login;
  const installEl = $('[data-copy="install"]');
  const loginEl = $('[data-copy="login"]');
  if (installEl) installEl.textContent = install;
  if (loginEl) loginEl.textContent = login;
}

async function loadSystem() {
  try {
    state.system = await api('/api/system');
    const sys = state.system;
    $('#fact-version').textContent = sys.version || '—';
    $('#fact-uptime').textContent = formatUptime(sys.uptime_seconds);
    $('#fact-upstream').textContent = (sys.gateway || {}).upstream || '—';
    $('#fact-agents').textContent = `${(sys.hub || {}).agents || 0} connected`;
    $('#status-server').textContent = `api ${sys.version} · ${(sys.hub || {}).agents || 0} agents`;
  } catch (_) { /* non-fatal */ }
}

function formatUptime(seconds) {
  seconds = Number(seconds || 0);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

/* ---------------------------------------------------------------- views */
const VIEWS = {
  sessions: { title: 'Sessions', subtitle: () => `${state.sessions.filter((s) => s.online).length} online · ${state.sessions.length} total` },
  keys: { title: 'API keys', subtitle: () => 'one model per key' },
  settings: { title: 'Settings', subtitle: () => 'account and gateway' },
};

function setView(view) {
  state.view = view;
  $$('.nav-item').forEach((button) => button.classList.toggle('is-active', button.dataset.view === view));
  $('#layout-toggle').hidden = view !== 'sessions';
  for (const name of Object.keys(VIEWS)) {
    const section = $(`#view-${name}`);
    if (section) section.hidden = name !== view;
  }
  $('#view-title').textContent = VIEWS[view].title;
  $('#view-subtitle').textContent = VIEWS[view].subtitle();
  renderTopbar();
  if (view === 'keys') loadKeys();
  if (view === 'settings') {
    renderSettings();
    loadSystem();
  }
  if (view === 'sessions') {
    loadSessions({ quiet: true });
    if (state.layout === 'tile') {
      requestAnimationFrame(() => layoutTiles());
    } else {
      const entry = state.terminals.get(state.activeId);
      if (entry) setTimeout(() => { fitTerminal(entry); entry.term.focus(); }, 30);
    }
  }
}

function renderTopbar() {
  const root = $('#topbar-actions');
  clear(root);
  if (state.view === 'sessions') {
    const active = activeSession();
    if (active) {
      root.append(
        h('button', { class: 'btn btn-sm', text: 'Copy', title: 'Copy selection, recent tmux buffer, or visible screen', onclick: copySelection }),
        h('button', { class: 'btn btn-sm', text: 'tmux', title: 'Copy the tmux selection buffer (any age)', onclick: () => requestTmuxBuffer(state.terminals.get(state.activeId), 'manual') }),
        h('button', { class: 'btn btn-ghost btn-sm', text: 'Copy attach', onclick: () => copyText(`opencloddie attach ${quoteArg(active.name)}`) }),
        h('button', { class: 'btn btn-danger btn-sm', text: 'Kill session', onclick: () => killSession(active) }),
      );
    }
    root.append(
      h('button', { class: 'btn btn-ghost btn-sm', text: 'Refresh', onclick: () => loadSessions() }),
      h('button', { class: 'btn btn-primary btn-sm', text: 'Connect session', onclick: connectModal }),
    );
  } else if (state.view === 'keys') {
    root.append(h('button', { class: 'btn btn-primary btn-sm', text: 'Create key', onclick: createKeyModal }));
  }
}

/* ---------------------------------------------------------------- layout */
function applyLayoutChrome() {
  const terminals = $('#terminals');
  terminals.classList.toggle('tiled', state.layout === 'tile');
  terminals.classList.toggle('single', state.layout === 'single');
  $('#tabs').hidden = state.layout === 'tile';
  renderLayoutToggle();
  updateTilesActive();
}

function renderLayoutToggle() {
  const single = $('#layout-single');
  const tile = $('#layout-tile');
  if (!single || !tile) return;
  single.classList.toggle('is-active', state.layout === 'single');
  tile.classList.toggle('is-active', state.layout === 'tile');
  single.setAttribute('aria-pressed', String(state.layout === 'single'));
  tile.setAttribute('aria-pressed', String(state.layout === 'tile'));
}

function setLayout(mode, { persist = true } = {}) {
  if (mode !== 'single' && mode !== 'tile') return;
  state.layout = mode;
  if (persist) safeStorageSet('opencloddie.layout', mode);
  applyLayoutChrome();
  if (!state.activeId && state.sessions.length) {
    const preferred = state.sessions.find((s) => s.online) || state.sessions[0];
    state.activeId = preferred.id;
  }
  renderTopbar();
  if (mode === 'tile') {
    layoutTiles();
    return;
  }
  if (state.activeId) {
    activate(state.activeId, { quiet: true });
  } else {
    $('#empty').hidden = false;
    $('#session-overlay').hidden = true;
    $('#status-active').textContent = 'No session selected';
  }
}

// Choose the column/row split that gives the most usable terminal area: prefer
// cells with a ~1.55 aspect ratio, penalize empty cells, and fall back to a
// square-ish grid when the container cannot satisfy the minimum size.
function computeGrid(count, width, height) {
  if (count <= 1) return { cols: 1, rows: 1 };
  const gap = 6;
  const pad = 6;
  const header = 28;
  const usableW = Math.max(1, width - pad * 2);
  const usableH = Math.max(1, height - pad * 2);
  let best = null;
  for (let cols = 1; cols <= count; cols += 1) {
    const rows = Math.ceil(count / cols);
    const cellW = (usableW - gap * (cols - 1)) / cols;
    const cellH = (usableH - gap * (rows - 1)) / rows;
    const termH = cellH - header;
    if (cellW < 200 || termH < 100) continue;
    const empty = cols * rows - count;
    const aspect = cellW / termH;
    const aspectPenalty = Math.abs(Math.log(aspect / 1.55));
    const score = Math.log(cellW * termH) - empty * 0.45 - aspectPenalty * 0.8;
    if (!best || score > best.score) best = { cols, rows, score };
  }
  if (!best) {
    const cols = Math.max(1, Math.min(count, Math.round(Math.sqrt(count * (usableW / usableH)))));
    best = { cols, rows: Math.ceil(count / cols) };
  }
  return best;
}

function layoutTiles() {
  if (state.layout !== 'tile') return;
  const root = $('#terminals');
  const sessions = state.sessions;
  if (!sessions.length) {
    $('#empty').hidden = false;
    $('#session-overlay').hidden = true;
    $('#status-active').textContent = 'No session selected';
    return;
  }
  $('#empty').hidden = true;
  $('#session-overlay').hidden = true;
  const grid = computeGrid(sessions.length, root.clientWidth, root.clientHeight);
  root.style.setProperty('--tile-cols', String(grid.cols));
  root.style.setProperty('--tile-rows', String(grid.rows));
  for (const session of sessions) {
    const entry = ensureTerminal(session);
    entry.el.classList.remove('hidden');
    updateTileHeader(entry, session);
  }
  updateTilesActive();
  const active = activeSession();
  $('#status-active').textContent = active
    ? `${active.name} · ${active.hostname || 'host'} · ${active.cwd || '—'} · ${active.model || ''}`
    : 'No session selected';
  requestAnimationFrame(() => {
    for (const session of sessions) {
      const entry = state.terminals.get(session.id);
      if (entry) fitTerminal(entry);
    }
  });
}

function updateTilesActive() {
  for (const [id, entry] of state.terminals) {
    entry.el.classList.toggle('is-active', state.layout === 'tile' && id === state.activeId);
  }
}

function updateTileHeader(entry, session) {
  if (!entry.headName) return;
  entry.headDot.className = session.online ? 'dot online' : 'dot';
  entry.headName.textContent = session.name;
  entry.headModel.textContent = session.model || '';
  entry.el.title = `${session.name}\n${session.hostname || ''} · ${session.cwd || ''}\n${session.model || ''}`;
  entry.offlineEl.hidden = Boolean(session.online);
  if (!session.online) {
    entry.offlineEl.textContent = session.last_seen
      ? `Offline · last seen ${session.last_seen}`
      : 'Offline';
  }
  entry.el.classList.toggle('is-offline', !session.online);
}

/* ---------------------------------------------------------------- sessions */
async function loadSessions({ quiet = false } = {}) {
  let data;
  try {
    data = await api('/api/sessions');
  } catch (err) {
    if (!quiet) toast(err.message, 'error');
    return;
  }
  const previous = new Map(state.sessions.map((s) => [s.id, s]));
  const previousActive = state.activeId;
  state.sessions = data.sessions || [];
  for (const session of state.sessions) {
    const entry = state.terminals.get(session.id);
    if (entry) {
      const wasOnline = entry.session && entry.session.online;
      entry.session = session;
      updateTileHeader(entry, session);
      if (wasOnline && !session.online) disconnectTerminal(entry);
      if (!wasOnline && session.online) connectTerminal(entry, session);
    }
  }
  // Drop terminals for sessions removed elsewhere.
  for (const id of Array.from(state.terminals.keys())) {
    if (!state.sessions.some((s) => s.id === id)) destroyTerminal(id);
  }
  if (state.sessions.length && !state.sessions.some((s) => s.id === state.activeId)) {
    const saved = safeStorageGet('opencloddie.active');
    const candidate = state.sessions.find((s) => s.id === saved && s.online)
      || state.sessions.find((s) => s.online)
      || state.sessions[0];
    state.activeId = candidate.id;
  }
  if (!state.sessions.length) state.activeId = null;
  renderTabs();
  if (state.layout === 'tile') {
    if (state.sessions.length) {
      layoutTiles();
    } else {
      $('#empty').hidden = false;
      $('#session-overlay').hidden = true;
      $('#status-active').textContent = 'No session selected';
    }
  } else if (state.activeId) {
    activate(state.activeId, { quiet: true });
  } else {
    $('#empty').hidden = false;
    $('#session-overlay').hidden = true;
    $('#status-active').textContent = 'No session selected';
  }
  updateSubtitle();
  // A new session can become active without a tab click (the first session,
  // or the active one disappearing).  Rebuild the action bar in that case.
  if (!quiet || previousActive !== state.activeId) renderTopbar();
}

function activeSession() {
  return state.sessions.find((session) => session.id === state.activeId) || null;
}

function updateSubtitle() {
  if (state.view === 'sessions') $('#view-subtitle').textContent = VIEWS.sessions.subtitle();
  if (state.view === 'keys') $('#view-subtitle').textContent = VIEWS.keys.subtitle();
}

function renderTabs() {
  const root = $('#tabs');
  clear(root);
  if (!state.sessions.length) {
    root.append(h('span', { class: 'tab-empty', text: 'No sessions connected' }));
  }
  for (const session of state.sessions) {
    const tab = h('button', {
      class: `tab ${session.id === state.activeId ? 'is-active' : ''}`.trim(),
      role: 'tab',
      title: `${session.name}\n${session.hostname || 'unknown host'} · ${session.cwd || '—'}\n${session.model || ''}`,
      onclick: () => activate(session.id),
    },
      h('span', { class: `dot ${session.online ? 'online' : ''}`.trim() }),
      h('span', { class: 'tab-name', text: session.name }),
    );
    root.append(tab);
  }
  root.append(h('button', { class: 'tab-add', text: '+', title: 'Connect a session', onclick: connectModal }));
}

function activate(id, { quiet = false } = {}) {
  const session = state.sessions.find((s) => s.id === id);
  if (!session) return;
  const changed = state.activeId !== id;
  state.activeId = id;
  safeStorageSet('opencloddie.active', id);
  renderTabs();
  updateSubtitle();
  if (!quiet || changed) renderTopbar();

  if (state.layout === 'tile') {
    $('#empty').hidden = true;
    $('#session-overlay').hidden = true;
    const entry = ensureTerminal(session);
    entry.el.classList.remove('hidden');
    updateTileHeader(entry, session);
    if (!entry.ws && !entry.retryTimer && session.online) connectTerminal(entry, session);
    updateTilesActive();
    requestAnimationFrame(() => {
      fitTerminal(entry);
      if (session.online) entry.term.focus();
    });
    $('#status-active').textContent = session.online
      ? `${session.name} · ${session.hostname || 'host'} · ${session.cwd || '—'} · ${session.model || ''}`
      : `${session.name} · offline · last seen ${session.last_seen || 'never'}`;
    return;
  }

  $('#empty').hidden = true;
  for (const [otherId, entry] of state.terminals) {
    entry.el.classList.toggle('hidden', otherId !== id);
  }
  if (!session.online) {
    $('#session-overlay').hidden = false;
    $('#overlay-title').textContent = 'Session offline';
    $('#overlay-text').textContent = session.last_seen
      ? `Last seen ${session.last_seen}. Start it again on ${session.hostname || 'the host'} with opencloddie.`
      : 'Start it again on the host with opencloddie.';
    $('#overlay-reconnect').hidden = true;
    $('#status-active').textContent = `${session.name} · offline · last seen ${session.last_seen || 'never'}`;
    return;
  }
  $('#session-overlay').hidden = true;
  const entry = ensureTerminal(session);
  if (!entry.ws && !entry.retryTimer && session.online) connectTerminal(entry, session);
  entry.el.classList.remove('hidden');
  requestAnimationFrame(() => { fitTerminal(entry); entry.term.focus(); });
  $('#status-active').textContent = `${session.name} · ${session.hostname || 'host'} · ${session.cwd || '—'} · ${session.model || ''}`;
}

function ensureTerminal(session) {
  let entry = state.terminals.get(session.id);
  if (entry) {
    entry.session = session;
    return entry;
  }
  const el = h('div', { class: 'term-wrap hidden' });
  const headDot = h('span', { class: 'dot' });
  const headName = h('span', { class: 'tile-name', text: session.name });
  const headModel = h('span', { class: 'tile-model', text: session.model || '' });
  const focusBtn = h('button', {
    class: 'tile-focus',
    type: 'button',
    title: 'Focus this session',
    'aria-label': 'Focus this session',
    text: '⤢',
  });
  const head = h('div', { class: 'tile-head' }, headDot, headName, headModel, focusBtn);
  const termEl = h('div', { class: 'tile-term' });
  const offlineEl = h('div', {
    class: 'tile-offline',
    hidden: Boolean(session.online),
    text: 'Offline',
  });
  el.append(head, termEl, offlineEl);
  $('#terminals').append(el);
  const term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
    fontSize: 13,
    lineHeight: 1.15,
    scrollback: 8000,
    allowProposedApi: true,
    theme: TERM_THEME,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(termEl);
  entry = {
    id: session.id,
    session,
    el,
    headDot,
    headName,
    headModel,
    termEl,
    offlineEl,
    term,
    fit,
    ws: null,
    connected: false,
    disposed: false,
    retries: 0,
    retryTimer: null,
    generation: 0,
    observer: null,
    selection: '',
    pendingBuffer: null,
    bufferTimer: null,
  };
  state.terminals.set(session.id, entry);
  setupTerminalClipboard(entry);

  head.addEventListener('click', (event) => {
    if (event.target.closest('.tile-focus')) return;
    if (state.layout === 'tile') activate(session.id, { quiet: true });
  });
  head.addEventListener('dblclick', (event) => {
    if (event.target.closest('.tile-focus')) return;
    setLayout('single');
    activate(session.id);
  });
  focusBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    setLayout('single');
    activate(session.id);
  });
  el.addEventListener('mousedown', (event) => {
    if (state.layout !== 'tile') return;
    if (event.target.closest('.tile-focus')) return;
    if (state.activeId !== session.id) activate(session.id, { quiet: true });
  });

  term.onData((data) => {
    entry.selection = '';
    if (entry.ws && entry.ws.readyState === WebSocket.OPEN) {
      entry.ws.send(new TextEncoder().encode(data));
    }
  });
  term.onResize(({ cols, rows }) => {
    sendControl(entry, { t: 'resize', cols, rows });
  });
  entry.observer = new ResizeObserver(() => {
    if (!entry.el.classList.contains('hidden')) fitTerminal(entry);
  });
  entry.observer.observe(termEl);
  updateTileHeader(entry, session);
  connectTerminal(entry, session);
  return entry;
}

function fitTerminal(entry) {
  try {
    entry.fit.fit();
    sendControl(entry, { t: 'resize', cols: entry.term.cols, rows: entry.term.rows });
  } catch (_) { /* hidden or zero-sized */ }
}

function sendControl(entry, message) {
  if (entry.ws && entry.ws.readyState === WebSocket.OPEN) {
    entry.ws.send(JSON.stringify(message));
  }
}

function copySelection() {
  copyFromTerminal(state.terminals.get(state.activeId));
}

function setupTerminalClipboard(entry) {
  const term = entry.term;
  const currentSelection = () => term.getSelection() || entry.selection || '';
  term.onSelectionChange(() => {
    const selection = term.getSelection();
    if (selection) entry.selection = selection;
  });
  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== 'keydown') return true;
    const key = (event.key || '').toLowerCase();
    if (event.ctrlKey && !event.shiftKey && key === 'insert') {
      event.preventDefault();
      copyFromTerminal(entry);
      return false;
    }
    if (event.shiftKey && !event.ctrlKey && !event.metaKey && key === 'insert') {
      event.preventDefault();
      pasteClipboard(term);
      return false;
    }
    // macOS: Cmd+C is copy; Ctrl+C must remain SIGINT.  Ctrl+Shift+C is
    // reserved by Chrome/Edge for DevTools, so it cannot be used.
    if (event.metaKey && !event.shiftKey && key === 'c') {
      event.preventDefault();
      copyFromTerminal(entry);
      return false;
    }
    return true;
  });
  entry.el.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    const selection = currentSelection();
    if (selection) {
      copyText(selection, { manualAction: true });
      return;
    }
    requestTmuxBuffer(entry, 'manual');
  });
}

async function pasteClipboard(term) {
  try {
    if (!navigator.clipboard || !window.isSecureContext) throw new Error('clipboard unavailable');
    const text = await navigator.clipboard.readText();
    if (text) term.paste(text);
  } catch (_) {
    toast('Clipboard read is blocked; use your browser paste shortcut', 'error');
  }
}

function connectTerminal(entry, session) {
  if (entry.disposed) return;
  entry.session = session;
  if (entry.retryTimer) { clearTimeout(entry.retryTimer); entry.retryTimer = null; }
  if (!session.online) return;
  if (entry.ws && (entry.ws.readyState === WebSocket.OPEN || entry.ws.readyState === WebSocket.CONNECTING)) return;

  const generation = ++entry.generation;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const cols = Math.max(2, entry.term.cols || 120);
  const rows = Math.max(2, entry.term.rows || 32);
  const url = `${proto}//${location.host}/api/sessions/${encodeURIComponent(session.id)}/ws?cols=${cols}&rows=${rows}`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (err) {
    scheduleReconnect(entry);
    return;
  }
  ws.binaryType = 'arraybuffer';
  entry.ws = ws;
  entry.connected = false;

  ws.onopen = () => {
    if (generation !== entry.generation) return;
    entry.connected = true;
    entry.retries = 0;
    sendControl(entry, { t: 'resize', cols: entry.term.cols, rows: entry.term.rows });
  };
  ws.onmessage = async (event) => {
    if (generation !== entry.generation) return;
    if (typeof event.data === 'string') {
      let message = null;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (!message) return;
      if (message.t === 'hello' && message.session) {
        Object.assign(entry.session, message.session);
        updateStatusline(entry);
      } else if (message.t === 'error') {
        toast(message.message || 'terminal error', 'error');
        updateStatusline(entry, message.message);
      } else if (message.t === 'tmux_buffer') {
        handleTmuxBuffer(entry, message);
      } else if (message.t === 'exit') {
        updateStatusline(entry, 'session ended');
        entry.term.write('\r\n\x1b[2m[session ended]\x1b[0m\r\n');
        loadSessions({ quiet: true });
      }
      return;
    }
    const buffer = event.data instanceof ArrayBuffer ? new Uint8Array(event.data) : event.data;
    entry.term.write(buffer);
  };
  ws.onerror = () => { /* onclose handles it */ };
  ws.onclose = () => {
    if (generation !== entry.generation) return;
    entry.connected = false;
    entry.ws = null;
    if (entry.disposed) return;
    if (entry.session && entry.session.online) {
      scheduleReconnect(entry);
    }
  };
}

function scheduleReconnect(entry) {
  if (entry.disposed || entry.retryTimer) return;
  entry.retries += 1;
  const delay = Math.min(10000, 500 * Math.pow(1.7, entry.retries - 1));
  entry.retryTimer = setTimeout(() => {
    entry.retryTimer = null;
    if (entry.session && entry.session.online && !entry.disposed) {
      connectTerminal(entry, entry.session);
    }
  }, delay);
}

function disconnectTerminal(entry) {
  entry.generation += 1;
  if (entry.retryTimer) { clearTimeout(entry.retryTimer); entry.retryTimer = null; }
  if (entry.bufferTimer) { clearTimeout(entry.bufferTimer); entry.bufferTimer = null; }
  entry.pendingBuffer = null;
  if (entry.ws) {
    try { entry.ws.close(); } catch (_) { /* ignore */ }
    entry.ws = null;
  }
  entry.connected = false;
}

function destroyTerminal(id) {
  const entry = state.terminals.get(id);
  if (!entry) return;
  entry.disposed = true;
  disconnectTerminal(entry);
  if (entry.observer) entry.observer.disconnect();
  try { entry.term.dispose(); } catch (_) { /* ignore */ }
  entry.el.remove();
  state.terminals.delete(id);
}

function updateStatusline(entry, override) {
  if (entry.id !== state.activeId) return;
  if (override) {
    $('#status-active').textContent = `${entry.session.name} · ${override}`;
    return;
  }
  $('#status-active').textContent = `${entry.session.name} · ${entry.session.hostname || 'host'} · ${entry.session.cwd || '—'} · ${entry.session.model || ''}`;
}

async function killSession(session) {
  const ok = await confirmDialog(
    'Kill session?',
    `This stops tmux and unregisters “${session.name}”. Claude Code running in it will exit.`,
    { confirmLabel: 'Kill session', danger: true },
  );
  if (!ok) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' });
    toast(`Removed ${session.name}`);
    destroyTerminal(session.id);
    state.sessions = state.sessions.filter((s) => s.id !== session.id);
    if (state.activeId === session.id) {
      state.activeId = state.sessions[0] ? state.sessions[0].id : null;
      if (state.activeId) activate(state.activeId);
    }
    await loadSessions();
  } catch (err) {
    toast(err.message, 'error');
  }
}

function connectModal() {
  const install = (state.me.install || {}).command || '';
  const login = (state.me.install || {}).login || '';
  const nameInput = h('input', { class: 'input', id: 'connect-name', placeholder: 'work', spellcheck: 'false' });
  const command = h('code', { style: 'display:block;padding:10px;white-space:pre-wrap;word-break:break-all' });
  const update = () => {
    const name = nameInput.value.trim();
    command.textContent = name ? `opencloddie --name ${quoteArg(name)}` : 'opencloddie';
  };
  nameInput.addEventListener('input', update);
  update();
  const body = h('div', {},
    h('p', { text: 'Run this in any directory where you want Claude to work. It starts a tmux session, points Claude at this gateway, and ships the terminal here.' }),
    h('div', { class: 'field' }, h('label', { text: 'Session name (optional)' }), nameInput),
    h('div', { class: 'field' }, h('label', { text: 'Command' }), command),
    h('p', { class: 'hint', text: 'Need the client first?' }),
    h('pre', { class: 'code', style: 'margin-top:6px', text: `${install}\n${login}` }),
  );
  openModal({
    title: 'Connect a session',
    body,
    actions: [
      { label: 'Copy command', onClick: () => { copyText(command.textContent); return false; } },
      { label: 'Done', class: 'btn-primary' },
    ],
  });
}

/* ---------------------------------------------------------------- keys */
async function loadKeys() {
  const root = $('#keys-list');
  clear(root);
  let data;
  try {
    data = await api('/api/keys');
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  const keys = data.keys || [];
  $('#keys-callout').hidden = keys.length > 0;
  const models = state.me.models || [];
  for (const key of keys) {
    const select = h('select', {
      class: 'input select key-model',
      disabled: key.revoked ? true : undefined,
      onchange: async (event) => {
        const model = event.target.value;
        try {
          await api(`/api/keys/${encodeURIComponent(key.id)}`, { method: 'PATCH', body: { model } });
          key.model = model;
          toast(`Model for “${key.name}” is now ${model}`);
        } catch (err) {
          toast(err.message, 'error');
          event.target.value = key.model;
        }
      },
    });
    for (const model of models) {
      select.append(h('option', { value: model.id, selected: model.id === key.model, text: `${model.label}` }));
    }
    if (!models.some((m) => m.id === key.model)) {
      select.append(h('option', { value: key.model, selected: true, text: key.model }));
    }

    const row = h('div', { class: `key-row ${key.revoked ? 'revoked' : ''}`.trim() },
      h('div', {},
        h('div', { class: 'key-name', text: key.name }),
        h('div', { class: 'key-prefix', text: key.prefix }),
        h('div', { class: 'key-meta', text: `created ${fmtAge(key.created_at)} · last used ${fmtAge(key.last_used_at)}` }),
      ),
      h('div', { class: 'key-meta', text: key.revoked ? `revoked ${fmtAge(key.revoked_at)}` : 'active' }),
      select,
      h('div', { class: 'key-actions' },
        key.revoked
          ? h('button', {
              class: 'btn btn-ghost btn-sm btn-danger-text', text: 'Delete',
              onclick: () => deleteKey(key),
            })
          : h('button', {
              class: 'btn btn-ghost btn-sm btn-danger-text', text: 'Revoke',
              onclick: () => revokeKey(key),
            }),
      ),
    );
    root.append(row);
  }
}

async function revokeKey(key) {
  const ok = await confirmDialog(
    'Revoke API key?',
    `“${key.name}” will immediately stop working for Claude Code. This cannot be undone.`,
    { confirmLabel: 'Revoke', danger: true },
  );
  if (!ok) return;
  try {
    await api(`/api/keys/${encodeURIComponent(key.id)}`, { method: 'PATCH', body: { revoked: true } });
    toast('Key revoked');
    loadKeys();
  } catch (err) { toast(err.message, 'error'); }
}

async function deleteKey(key) {
  const ok = await confirmDialog('Delete key?', `Permanently delete “${key.name}”?`, { confirmLabel: 'Delete', danger: true });
  if (!ok) return;
  try {
    await api(`/api/keys/${encodeURIComponent(key.id)}`, { method: 'DELETE' });
    toast('Key deleted');
    loadKeys();
  } catch (err) { toast(err.message, 'error'); }
}

function createKeyModal() {
  const nameInput = h('input', { class: 'input', placeholder: 'laptop', spellcheck: 'false' });
  const modelSelect = h('select', { class: 'input select' });
  const defaultModel = state.me.default_model || (state.me.models[0] || {}).id;
  for (const model of state.me.models || []) {
    modelSelect.append(h('option', { value: model.id, selected: model.id === defaultModel, text: `${model.label} (${model.id})` }));
  }
  const body = h('div', {},
    h('p', { text: 'The key is shown once. Claude Code uses it with the selected model; change it later from this page.' }),
    h('div', { class: 'field' }, h('label', { text: 'Name' }), nameInput),
    h('div', { class: 'field' }, h('label', { text: 'Model' }), modelSelect),
  );
  openModal({
    title: 'Create API key',
    body,
    actions: [
      { label: 'Cancel', class: 'btn-ghost' },
      {
        label: 'Create key',
        class: 'btn-primary',
        close: false,
        onClick: async () => {
          const name = nameInput.value.trim() || 'CLI key';
          try {
            const result = await api('/api/keys', { method: 'POST', body: { name, model: modelSelect.value } });
            showToken(result.key);
            loadKeys();
          } catch (err) {
            toast(err.message, 'error');
          }
          return false;
        },
      },
    ],
  });
}

function showToken(key) {
  const token = key.token;
  const body = h('div', {},
    h('p', { text: `“${key.name}” is ready. Copy the token now — it is not stored in plaintext.` }),
    h('div', { class: 'token-box' }, h('code', { text: token })),
    h('p', { class: 'hint', text: 'Run on the machine with Claude Code:' }),
    h('pre', { class: 'code', style: 'margin-top:6px', text: `opencloddie login --server ${(state.me.urls || {}).api || ''} --api-key ${token}` }),
  );
  openModal({
    title: 'API key created',
    body,
    actions: [
      { label: 'Copy token', onClick: () => { copyText(token); return false; } },
      { label: 'Done', class: 'btn-primary' },
    ],
  });
}

/* ---------------------------------------------------------------- settings */
function renderSettings() {
  const me = state.me;
  const badge = $('#opencode-badge');
  badge.textContent = me.opencode_key_set ? `set · ${me.opencode_key_hint}` : 'not set';
  badge.className = `badge ${me.opencode_key_set ? 'ok' : 'bad'}`;
  $('#opencode-hint').textContent = me.opencode_key_set
    ? 'Replace it by entering a new key. Requests use the key of the owning account.'
    : 'Paste your OpenCode Go key (oc_sk_…). Without it, gateway requests return an error.';

  const select = $('#default-model');
  clear(select);
  for (const model of me.models || []) {
    select.append(h('option', { value: model.id, selected: model.id === me.default_model, text: `${model.label} (${model.id})` }));
  }
  renderInstall();
}

async function saveOpencodeKey() {
  const input = $('#opencode-input');
  const key = input.value.trim();
  if (!key) { toast('Paste an OpenCode key first', 'warn'); return; }
  const button = $('#opencode-save');
  button.disabled = true;
  try {
    await api('/api/opencode-key', { method: 'PUT', body: { key } });
    input.value = '';
    toast('OpenCode key saved');
    state.me = await api('/api/me');
    renderSettings();
  } catch (err) { toast(err.message, 'error'); }
  finally { button.disabled = false; }
}

async function testOpencodeKey() {
  const button = $('#opencode-test');
  const key = $('#opencode-input').value.trim();
  button.disabled = true;
  button.textContent = 'Testing…';
  try {
    const result = await api('/api/opencode-key/test', { method: 'POST', body: key ? { key } : {} });
    const detail = result.result || {};
    if (result.ok) toast(`OpenCode key works (HTTP ${detail.status})`);
    else toast(`Test failed (HTTP ${detail.status}): ${detail.message || 'unknown error'}`, 'error');
  } catch (err) { toast(err.message, 'error'); }
  finally { button.disabled = false; button.textContent = 'Test'; }
}

async function removeOpencodeKey() {
  const ok = await confirmDialog('Remove OpenCode key?', 'Gateway requests will fail until you add another key.', { confirmLabel: 'Remove', danger: true });
  if (!ok) return;
  try {
    await api('/api/opencode-key', { method: 'DELETE' });
    toast('OpenCode key removed');
    state.me = await api('/api/me');
    renderSettings();
  } catch (err) { toast(err.message, 'error'); }
}

async function saveDefaultModel() {
  const model = $('#default-model').value;
  try {
    await api('/api/settings', { method: 'PUT', body: { default_model: model } });
    state.me.default_model = model;
    toast('Default model updated (new keys use it)');
  } catch (err) { toast(err.message, 'error'); }
}

/* ---------------------------------------------------------------- storage */
function safeStorageGet(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function safeStorageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* ignore */ }
}

/* ---------------------------------------------------------------- drawer */
function openDrawer() {
  const rail = $('#rail');
  if (!rail || rail.classList.contains('is-open')) return;
  rail.inert = false;
  rail.classList.add('is-open');
  $('#drawer-backdrop').hidden = false;
  $('#menu-btn').setAttribute('aria-expanded', 'true');
  const first = rail.querySelector('.nav-item');
  if (first) setTimeout(() => {
    if (rail.classList.contains('is-open')) first.focus();
  }, 30);
}

function closeDrawer({ restoreFocus = false } = {}) {
  const rail = $('#rail');
  if (!rail || !rail.classList.contains('is-open')) return;
  const focusInside = rail.contains(document.activeElement);
  rail.classList.remove('is-open');
  $('#drawer-backdrop').hidden = true;
  $('#menu-btn').setAttribute('aria-expanded', 'false');
  if (restoreFocus || focusInside) $('#menu-btn').focus();
  rail.inert = true;
}

function toggleDrawer() {
  const rail = $('#rail');
  if (rail && rail.classList.contains('is-open')) closeDrawer({ restoreFocus: true });
  else openDrawer();
}

/* ---------------------------------------------------------------- events */
function bind() {
  $$('.nav-item').forEach((button) => button.addEventListener('click', () => {
    setView(button.dataset.view);
    closeDrawer();
  }));
  $('#menu-btn').addEventListener('click', toggleDrawer);
  $('#drawer-close').addEventListener('click', () => closeDrawer({ restoreFocus: true }));
  $('#drawer-backdrop').addEventListener('click', () => closeDrawer({ restoreFocus: true }));
  $('#layout-single').addEventListener('click', () => setLayout('single'));
  $('#layout-tile').addEventListener('click', () => setLayout('tile'));
  $('#modal-x').addEventListener('click', closeModal);
  $('#modal-root').addEventListener('click', (event) => {
    if (event.target.dataset.close) closeModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('#modal-root').hidden) { closeModal(); return; }
    closeDrawer({ restoreFocus: true });
  });
  $('#new-key').addEventListener('click', createKeyModal);
  $('#empty-new').addEventListener('click', connectModal);
  $('#empty-refresh').addEventListener('click', () => loadSessions());
  $('#overlay-reconnect').addEventListener('click', () => {
    const session = activeSession();
    if (session) { const entry = ensureTerminal(session); connectTerminal(entry, session); }
  });
  $('#overlay-kill').addEventListener('click', () => {
    const session = activeSession();
    if (session) killSession(session);
  });
  $('#opencode-save').addEventListener('click', saveOpencodeKey);
  $('#opencode-test').addEventListener('click', testOpencodeKey);
  $('#opencode-remove').addEventListener('click', removeOpencodeKey);
  $('#save-default-model').addEventListener('click', saveDefaultModel);
  document.addEventListener('click', (event) => {
    const target = event.target.closest('[data-copy]');
    if (!target) return;
    const which = target.dataset.copy;
    if (which === 'install') copyText((state.me.install || {}).command || '');
    if (which === 'login') copyText((state.me.install || {}).login || '');
  });
  $$('[data-copy-command]').forEach((button) => {
    button.addEventListener('click', async () => {
      const which = button.dataset.copyCommand;
      const source = which === 'login' ? $('#login-command') : $('#install-command');
      const text = (source ? source.textContent : '').trim();
      if (!text || text === '—') {
        toast('The command is still loading', 'warn');
        return;
      }
      const original = button.textContent;
      const ok = await copyText(text);
      if (ok) {
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = original; }, 1400);
      }
    });
  });
  let tileResizeTimer = null;
  new ResizeObserver(() => {
    if (state.layout !== 'tile' || !state.booted) return;
    clearTimeout(tileResizeTimer);
    tileResizeTimer = setTimeout(() => layoutTiles(), 80);
  }).observe($('#terminals'));
  window.addEventListener('resize', () => {
    if (window.matchMedia('(max-width: 520px)').matches && state.layout === 'tile') {
      setLayout('single', { persist: false });
    }
    if (state.layout === 'tile') {
      layoutTiles();
      return;
    }
    const entry = state.terminals.get(state.activeId);
    if (entry) fitTerminal(entry);
  });
  window.addEventListener('beforeunload', () => {
    for (const entry of state.terminals.values()) disconnectTerminal(entry);
  });
}

bind();
boot();
