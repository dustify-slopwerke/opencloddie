/* Structured chat controller (window.ChatView).
 *
 * One WebSocket per visible chat session.  Events are rendered incrementally,
 * deduplicated by seq/event_id, and replayed from the last seen seq after a
 * reconnect.  Model output is only ever rendered through ChatRender.
 */
(() => {
  'use strict';

  const state = {
    inited: false,
    deps: null,
    sessionId: null,
    session: null,
    chat: null,
    pending: new Map(),
    permissionCards: new Map(),
    groups: new Map(),
    streams: new Map(),
    toolCards: new Map(),
    nestedContainers: new Map(),
    applied: new Set(),
    appliedIds: new Set(),
    optimistic: [],
    loading: false,
    loaded: false,
    visible: false,
    ws: null,
    wsGen: 0,
    connecting: false,
    connected: false,
    retries: 0,
    retryTimer: null,
    pingTimer: null,
    resyncSentAt: 0,
    readTimer: null,
    notifyTimer: null,
    tickTimer: null,
    lastSeq: 0,
    lastMarkedRead: 0,
    streaming: false,
    pinned: true,
    scrollScheduled: false,
    composerScheduled: false,
    bound: false,
    lastErrorToast: 0,
    toolSeq: 0,
    attachments: [],
    attachSeq: 0,
    dragDepth: 0,
    suggestion: '',
    limits: { max_image_bytes: 5 * 1024 * 1024, max_input_images: 4 },
    search: {
      open: false, query: '', lastQuery: '', loading: false, results: null,
      error: '', hasMore: false, timer: null, token: 0,
      wrap: null, input: null, panel: null, head: null, note: null, list: null,
    },
    checkpoints: new Map(),
    lastUser: null,
    seqEls: new Map(),
    rewind: { busy: false, pending: false, timer: null, refreshTimer: null },
  };

  const BUSY_STATUSES = new Set([
    'thinking', 'working', 'running', 'streaming', 'tool', 'tool_use',
    'compacting', 'retrying', 'busy', 'waiting', 'waiting_permission', 'starting',
  ]);
  const PERMISSION_MODES = ['default', 'acceptEdits', 'plan', 'auto', 'dontAsk'];
  const CHAT_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

  /* ---------------------------------------------------------------- shell */
  function init(deps) {
    state.deps = deps;
    if (window.ChatRender) window.ChatRender.init(deps);
    if (deps && deps.chatConfig) applyChatLimits(deps.chatConfig);
    loadChatLimits();
    buildComposerExtras();
    buildSearchUI();
    bindOnce();
    state.inited = true;
  }

  function applyChatLimits(config) {
    if (!config || typeof config !== 'object') return;
    const perImage = Number(config.max_image_bytes);
    const count = Number(config.max_input_images);
    if (Number.isFinite(perImage) && perImage > 0) state.limits.max_image_bytes = perImage;
    if (Number.isFinite(count) && count >= 0) state.limits.max_input_images = Math.floor(count);
  }

  function loadChatLimits() {
    if (!state.deps) return;
    const configured = typeof state.deps.chatConfig === 'function'
      ? state.deps.chatConfig()
      : state.deps.chatConfig;
    if (configured) {
      applyChatLimits(configured);
      return;
    }
    if (typeof state.deps.api !== 'function') return;
    // The web listener's /api/me carries the configured image limits.
    state.deps.api('/api/me').then((data) => {
      applyChatLimits(data && data.chat);
    }).catch(() => { /* keep the documented defaults */ });
  }

  function node(id) {
    return document.getElementById(id);
  }

  function timeline() {
    return node('chat-timeline');
  }

  function isBusy() {
    if (state.streaming) return true;
    const status = (state.chat && state.chat.status) || '';
    return BUSY_STATUSES.has(status);
  }

  function uid() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
    } catch (_) { /* fall through */ }
    return `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
  }

  function modeOf(chat, session) {
    const value = chat || {};
    if (value.mode) return String(value.mode);
    if (session && session.chat && session.chat.mode) return String(session.chat.mode);
    return 'tui';
  }

  function normalizeChat(chat) {
    return Object.assign({}, chat && typeof chat === 'object' ? chat : {});
  }

  function setChat(next) {
    const previous = state.chat ? String(state.chat.mode || '') : '';
    state.chat = normalizeChat(next);
    const mode = String(state.chat.mode || '');
    if (previous && mode && previous !== mode && (mode === 'chat' || mode === 'tui')
      && state.sessionId && state.deps && typeof state.deps.onModeChange === 'function') {
      state.deps.onModeChange(state.sessionId, mode);
    }
  }

  /* --------------------------------------------------------------- public */
  function show(session) {
    if (!state.inited || !session || !session.id) return;
    const pane = node('chat-pane');
    if (pane) pane.hidden = false;
    state.visible = true;
    state.session = session;
    const same = state.sessionId === session.id;
    if (same) {
      renderHeader();
      syncComposer();
      if (!state.loaded && !state.loading) {
        loadSnapshot(session);
        return;
      }
      if (!state.loading && !state.ws && !state.connecting) connect(state.lastSeq);
      startTick();
      markReadSoon();
      return;
    }
    closeSocket();
    stopTimers();
    cancelRewind();
    resetAttachments();
    resetSearch();
    setSuggestion('');
    state.sessionId = session.id;
    state.loading = false;
    state.chat = session.chat ? normalizeChat(session.chat) : null;
    resetTimeline();
    renderHeader();
    syncComposer();
    loadSnapshot(session);
  }

  function hide() {
    state.visible = false;
    closeSocket();
    stopTick();
    if (state.readTimer) { clearTimeout(state.readTimer); state.readTimer = null; }
    if (state.notifyTimer) { clearTimeout(state.notifyTimer); state.notifyTimer = null; }
  }

  function destroy(sessionId) {
    if (state.sessionId !== sessionId) return;
    hide();
    cancelRewind();
    resetAttachments();
    resetSearch();
    setSuggestion('');
    resetTimeline();
    const emptyTimeline = timeline();
    if (emptyTimeline) state.deps.clear(emptyTimeline);
    state.sessionId = null;
    state.session = null;
    state.chat = null;
    state.lastSeq = 0;
    state.lastMarkedRead = 0;
    state.optimistic = [];
    state.loading = false;
    const header = node('chat-status');
    if (header) state.deps.clear(header);
    syncComposer();
  }

  function clearHistory() {
    if (!state.sessionId || !state.deps) return;
    state.deps.confirmDialog(
      'Delete chat history?',
      'Removes opencloddie’s stored transcript for this session. Claude’s own transcript on the host is untouched.',
      { confirmLabel: 'Delete', danger: true },
    ).then((ok) => {
      if (!ok) return;
      state.deps.api(
        `/api/sessions/${encodeURIComponent(state.sessionId)}/chat/history`,
        { method: 'DELETE' },
      ).then(() => {
        resetTimeline();
        state.lastSeq = 0;
        state.applied.clear();
        state.appliedIds.clear();
        state.checkpoints.clear();
        loadSnapshot(state.session);
      }).catch((error) => {
        if (state.deps.toast) {
          state.deps.toast(error && error.message ? error.message : 'Could not delete history', 'error');
        }
      });
    });
  }

  function setPermissionMode(mode) {
    if (!state.sessionId || !mode) return;
    const previous = (state.chat && state.chat.permission_mode) || 'default';
    if (state.deps && state.deps.api) {
      state.deps.api(
        `/api/sessions/${encodeURIComponent(state.sessionId)}/chat/config`,
        { method: 'POST', body: { permission_mode: mode } },
      ).then(() => {
        if (state.chat) state.chat.permission_mode = mode;
        renderHeader();
      }).catch((error) => {
        if (state.deps.toast) {
          state.deps.toast(error && error.message ? error.message : 'Could not change permission mode', 'error');
        }
        if (state.chat) state.chat.permission_mode = previous;
        renderHeader();
      });
    }
  }

  function handoff(session, mode) {
    if (!session || !session.id) return Promise.resolve();
    const to = mode === 'chat' ? 'chat' : 'tui';
    const useSocket = state.sessionId === session.id
      && state.ws && state.ws.readyState === WebSocket.OPEN;
    if (useSocket) {
      state.ws.send(JSON.stringify({ t: 'handoff', to, force: true }));
      state.chat = Object.assign({}, state.chat || {}, { mode: 'handoff', status: 'handoff' });
      renderHeader();
      syncComposer();
      scheduleNotify();
      return Promise.resolve();
    }
    return state.deps.api(`/api/sessions/${encodeURIComponent(session.id)}/chat/mode`, {
      method: 'POST',
      body: { mode: to, force: true },
    }).then(() => {
      if (state.sessionId === session.id) {
        state.chat = Object.assign({}, state.chat || {}, { mode: 'handoff', status: 'handoff' });
        renderHeader();
        syncComposer();
        scheduleNotify();
      }
    });
  }

  function focusPending() {
    for (const [id, card] of state.permissionCards) {
      if (!state.pending.has(id)) continue;
      try { card.el.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { /* ignore */ }
      card.el.classList.add('is-flash');
      setTimeout(() => card.el.classList.remove('is-flash'), 1400);
      return true;
    }
    return false;
  }

  /* -------------------------------------------------------------- loading */
  function resetTimeline() {
    for (const card of state.permissionCards.values()) {
      try { card.dispose(); } catch (_) { /* ignore */ }
    }
    state.permissionCards.clear();
    state.pending.clear();
    state.groups.clear();
    state.streams.clear();
    state.toolCards.clear();
    state.nestedContainers.clear();
    state.applied.clear();
    state.appliedIds.clear();
    state.optimistic = [];
    state.checkpoints.clear();
    state.lastUser = null;
    state.seqEls.clear();
    state.lastSeq = 0;
    state.lastMarkedRead = 0;
    state.streaming = false;
    state.pinned = true;
    state.loaded = false;
    const tl = timeline();
    if (!tl) return;
    state.deps.clear(tl);
    const row = window.ChatRender.statusRow('Loading chat…', { kind: 'loading' });
    row.classList.add('chat-loading');
    tl.append(row);
  }

  async function loadSnapshot(session) {
    if (!session || !session.id) return;
    state.loading = true;
    syncComposer();
    let data;
    try {
      data = await state.deps.api(`/api/sessions/${encodeURIComponent(session.id)}/chat?limit=200`);
    } catch (error) {
      if (state.sessionId !== session.id) return;
      state.loading = false;
      removeLoading();
      const tl = timeline();
      if (tl) {
        tl.append(window.ChatRender.errorRow(
          { message: error && error.message ? error.message : 'Could not load chat.' },
          {
            title: 'Chat unavailable',
            retry: () => { resetTimeline(); loadSnapshot(session); },
          },
        ));
      }
      renderHeader();
      syncComposer();
      if (state.deps.toast) state.deps.toast(error && error.message ? error.message : 'Could not load chat.', 'error');
      return;
    }
    if (state.sessionId !== session.id) return;
    state.loading = false;
    state.loaded = true;
    setChat(data.chat);
    for (const event of Array.isArray(data.events) ? data.events : []) applyEvent(event);
    for (const permission of Array.isArray(data.pending) ? data.pending : []) addPermission(permission);
    state.lastSeq = Math.max(state.lastSeq, Number(data.last_seq) || 0);
    finalizeSnapshot();
    renderHeader();
    syncComposer();
    if (state.visible) {
      connect(state.lastSeq);
      startTick();
      markReadSoon();
    }
    scheduleNotify();
  }

  function finalizeSnapshot() {
    removeLoading();
    const tl = timeline();
    if (!tl) return;
    if (!tl.querySelector('.chat-msg, .chat-permission, .chat-row, .chat-tool')) {
      tl.append(window.ChatRender.emptyState(emptyMessage()));
    }
    scrollToBottom(true);
  }

  function emptyMessage() {
    const mode = modeOf(state.chat, state.session);
    if (mode === 'tui') return 'This session is in terminal mode. Switch to chat to send messages.';
    if (state.session && !state.session.online) return 'No chat history yet. Start the session on its host to chat.';
    return 'No messages yet. Send a message below to start.';
  }

  /* ------------------------------------------------------------ websocket */
  function stopPing() {
    if (state.pingTimer) {
      clearInterval(state.pingTimer);
      state.pingTimer = null;
    }
  }

  function startPing() {
    stopPing();
    state.pingTimer = setInterval(() => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      try { state.ws.send(JSON.stringify({ t: 'ping' })); } catch (_) { /* ignore */ }
    }, 25000);
  }

  function requestResync(afterSeq) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - state.resyncSentAt < 2000) return;
    state.resyncSentAt = Date.now();
    try { state.ws.send(JSON.stringify({ t: 'resync', after_seq: Math.max(0, Number(afterSeq) || 0) })); } catch (_) { /* ignore */ }
  }

  function closeSocket() {
    state.wsGen += 1;
    state.connecting = false;
    state.connected = false;
    stopPing();
    if (state.retryTimer) { clearTimeout(state.retryTimer); state.retryTimer = null; }
    const socket = state.ws;
    state.ws = null;
    if (socket) {
      try { socket.close(); } catch (_) { /* ignore */ }
    }
  }

  function connect(afterSeq) {
    if (!state.visible || !state.sessionId || !state.inited) return;
    closeSocket();
    const generation = ++state.wsGen;
    state.connecting = true;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = Math.max(0, Number(afterSeq) || 0);
    const url = `${protocol}//${window.location.host}/api/sessions/${encodeURIComponent(state.sessionId)}/chat/ws?after_seq=${query}`;
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (_) {
      state.connecting = false;
      scheduleReconnect();
      return;
    }
    state.ws = socket;
    socket.onopen = () => {
      if (generation !== state.wsGen) return;
      state.connecting = false;
      state.connected = true;
      state.retries = 0;
      startPing();
      renderHeader();
      syncComposer();
      markReadSoon();
    };
    socket.onmessage = (event) => {
      if (generation !== state.wsGen) return;
      let frame;
      try { frame = JSON.parse(event.data); } catch (_) { return; }
      if (frame && typeof frame === 'object') handleFrame(frame);
    };
    socket.onerror = () => { /* onclose handles it */ };
    socket.onclose = () => {
      if (generation !== state.wsGen) return;
      state.ws = null;
      state.connected = false;
      state.connecting = false;
      stopPing();
      renderHeader();
      syncComposer();
      if (state.visible) scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (!state.visible || !state.sessionId || state.retryTimer) return;
    state.retries += 1;
    const delay = Math.min(15000, 500 * Math.pow(1.7, state.retries - 1));
    state.retryTimer = setTimeout(() => {
      state.retryTimer = null;
      if (state.visible && state.sessionId) connect(state.lastSeq);
    }, delay);
  }

  /* --------------------------------------------------------------- frames */
  function handleFrame(frame) {
    switch (frame.t) {
      case 'hello': handleHello(frame); break;
      case 'event': applyEvent(frame); markReadSoon(); scheduleNotify(); break;
      case 'delta': applyDelta(frame); break;
      case 'state': {
        const next = normalizeChat(frame.chat);
        setChat(next);
        if (next.status === 'idle' || next.status === 'offline' || next.status === 'error') state.streaming = false;
        renderHeader();
        syncComposer();
        scheduleNotify();
        if (frame.rewind && typeof frame.rewind === 'object') handleRewind(frame.rewind);
        break;
      }
      case 'permission': addPermission(frame.permission); break;
      case 'permission_resolved': resolvePermission(frame.id, frame.status, frame.decision); break;
      case 'mode': handleMode(frame); break;
      case 'input': handleInputEcho(frame); break;
      case 'error': handleError(frame); break;
      case 'pong': break;
      default: break;
    }
  }

  function handleHello(frame) {
    if (frame.chat) setChat(frame.chat);
    if (frame.session && state.session) {
      Object.assign(state.session, frame.session);
      if (frame.session.chat) {
        state.session.chat = Object.assign({}, state.session.chat || {}, frame.session.chat);
      }
    }
    for (const event of Array.isArray(frame.events) ? frame.events : []) applyEvent(event);
    for (const permission of Array.isArray(frame.pending) ? frame.pending : []) addPermission(permission);
    state.lastSeq = Math.max(state.lastSeq, Number(frame.last_seq) || 0);
    finalizeSnapshot();
    renderHeader();
    syncComposer();
    scheduleNotify();
    markReadSoon();
  }

  function handleMode(frame) {
    const previous = state.chat ? String(state.chat.mode || '') : '';
    state.chat = Object.assign({}, state.chat || {},
      { mode: frame.mode || (state.chat && state.chat.mode) || 'chat', status: frame.status || 'idle' });
    if (frame.message) appendTimeline(window.ChatRender.statusRow(String(frame.message), { kind: 'handoff' }));
    renderHeader();
    syncComposer();
    scheduleNotify();
    if (previous && frame.mode && previous !== frame.mode
      && (frame.mode === 'chat' || frame.mode === 'tui') && state.deps.onModeChange) {
      state.deps.onModeChange(state.sessionId, frame.mode);
    }
  }

  function handleInputEcho(frame) {
    const content = frame.content;
    const text = window.ChatRender.contentText(content);
    if (!text && !Array.isArray(content)) return;
    const el = window.ChatRender.userBubble(content);
    appendTimeline(el);
    state.lastUser = { el, uuid: '' };
    state.optimistic.push({ text: text.trim(), el, used: false, at: Date.now() });
    pruneOptimistic();
    markReadSoon();
  }

  function handleError(frame) {
    const message = frame && frame.message ? String(frame.message) : 'Chat error';
    appendTimeline(window.ChatRender.errorRow({ message }));
    resetDecidingCards();
    state.streaming = false;
    if (frame && (frame.code === 'offline' || frame.code === 'chat_exit')) {
      state.chat = Object.assign({}, state.chat || {}, { status: 'offline' });
    }
    renderHeader();
    syncComposer();
    if (Date.now() - state.lastErrorToast > 4000) {
      state.lastErrorToast = Date.now();
      if (state.deps.toast) state.deps.toast(message, 'error');
    }
  }

  /* --------------------------------------------------------------- events */
  function applyEvent(event) {
    if (!event || typeof event !== 'object') return;
    const seq = Number(event.seq) || 0;
    if (seq) {
      if (state.applied.has(seq)) return;
      state.applied.add(seq);
    }
    const eventId = event.event_id ? String(event.event_id) : '';
    if (eventId) {
      if (state.appliedIds.has(eventId)) return;
      state.appliedIds.add(eventId);
    }
    if (seq && state.loaded && !state.loading && state.lastSeq && seq > state.lastSeq + 1) {
      requestResync(state.lastSeq);
    }
    if (seq) state.lastSeq = Math.max(state.lastSeq, seq);
    removeLoading();
    removeEmpty();
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    switch (event.kind) {
      case 'user_message': handleUserMessage(payload); break;
      case 'assistant_block': handleAssistantBlock(payload); break;
      case 'message_start':
        if (payload.model) {
          state.chat = Object.assign({}, state.chat || {}, { model: String(payload.model) });
          renderHeader();
        }
        break;
      case 'message_stop': handleMessageStop(payload); break;
      case 'tool_result': handleToolResult(payload); break;
      case 'result': handleResult(payload); break;
      case 'error': appendTimeline(window.ChatRender.errorRow(payload)); break;
      case 'status':
        if (payload.status && payload.status !== 'idle') {
          appendTimeline(window.ChatRender.statusRow(String(payload.status), { kind: 'status' }));
        }
        break;
      case 'rate_limit': {
        const info = payload && typeof payload === 'object' ? payload : {};
        const text = info.message || info.status || info.rate_limit_type || 'Rate limited — retrying';
        appendTimeline(window.ChatRender.statusRow(String(text), { kind: 'status' }));
        break;
      }
      case 'compact_boundary':
        appendTimeline(window.ChatRender.statusRow('Conversation compacted', { kind: 'status' }));
        break;
      case 'prompt_suggestion':
        setSuggestion(payload && payload.suggestion);
        break;
      case 'permission': addPermission(payload); break;
      case 'permission_resolved':
        resolvePermission(payload.permission_id || payload.id, payload.status, payload.decision);
        break;
      case 'handoff':
        appendTimeline(window.ChatRender.statusRow(
          `Handoff ${payload.from || ''} → ${payload.to || ''}`.replace(/\s+/g, ' ').trim(),
          { kind: 'handoff' },
        ));
        break;
      case 'system_init':
        if (payload.model || payload.permission_mode) {
          state.chat = Object.assign({}, state.chat || {}, {
            model: payload.model ? String(payload.model) : (state.chat && state.chat.model),
            permission_mode: payload.permission_mode ? String(payload.permission_mode) : (state.chat && state.chat.permission_mode),
          });
          renderHeader();
        }
        break;
      case 'checkpoint': handleCheckpoint(payload, seq); break;
      case 'rewind': handleRewind(payload); break;
      default: break;
    }
    // Remember which rendered element carries each seq so search results can
    // jump back to an event that is already in the timeline.
    if (seq) {
      const tl = timeline();
      const last = tl ? tl.lastElementChild : null;
      if (last) state.seqEls.set(seq, last);
    }
    scrollIfPinned();
  }

  function handleUserMessage(payload) {
    const content = payload.content;
    const text = window.ChatRender.contentText(content);
    const optimistic = takeOptimistic(text);
    let el = optimistic && optimistic.el ? optimistic.el : null;
    if (!el) {
      el = window.ChatRender.userBubble(content);
      appendTimeline(el);
    }
    state.lastUser = { el, uuid: payload.uuid ? String(payload.uuid) : '' };
  }

  function handleAssistantBlock(payload) {
    const block = payload.block && typeof payload.block === 'object' ? payload.block : {};
    const messageId = payload.message_id || '';
    const parent = payload.parent_tool_use_id || null;
    const blockIndex = Number(payload.block_index) || 0;
    const group = ensureGroup(parent, messageId);
    if (!group) return;
    group.finalBlocks.add(blockIndex);
    const type = block.type;
    if (type === 'text') {
      const text = block.text || '';
      const streamed = takeStream(group.key, blockIndex, 'text');
      const rendered = text ? window.ChatRender.textBlock(String(text)) : null;
      if (streamed) {
        if (rendered) streamed.el.replaceWith(rendered);
        else streamed.el.remove();
      } else if (rendered) {
        group.body.append(rendered);
      }
      return;
    }
    if (type === 'thinking' || type === 'redacted_thinking') {
      const text = block.thinking || block.text || (type === 'redacted_thinking' ? '[redacted thinking]' : '');
      const streamed = takeStream(group.key, blockIndex, 'thinking');
      const rendered = text ? window.ChatRender.thinkingBlock(String(text)) : null;
      if (streamed) {
        if (rendered) streamed.el.replaceWith(rendered);
        else streamed.el.remove();
      } else if (rendered) {
        group.body.append(rendered);
      }
      return;
    }
    if (type === 'image') {
      group.body.append(window.ChatRender.imageBlock(block));
      return;
    }
    if (type === 'tool_use' || type === 'server_tool_use') {
      handleToolUse(block, group);
      return;
    }
    if (type) {
      group.body.append(window.ChatRender.statusRow(`Unsupported block: ${String(type)}`, { kind: 'status' }));
    }
  }

  function handleMessageStop(payload) {
    const messageId = payload.message_id ? String(payload.message_id) : '';
    for (const group of state.groups.values()) {
      if (!messageId || group.messageId === messageId) group.finalized = true;
    }
    for (const entry of state.streams.values()) {
      const parts = entry.groupKey.split('|');
      if (!messageId || parts[parts.length - 1] === messageId) entry.controller.removeCursor();
    }
  }

  function handleToolResult(payload) {
    const id = String(payload.tool_use_id || payload.id || '');
    const card = id ? state.toolCards.get(id) : null;
    state.streaming = false;
    if (card) card.setResult(payload);
    else appendTimeline(window.ChatRender.orphanToolResult(payload));
    scheduleComposerSync();
  }

  function handleResult(payload) {
    state.streaming = false;
    state.chat = Object.assign({}, state.chat || {}, { status: 'idle' });
    const cost = Number(payload.total_cost_usd);
    if (Number.isFinite(cost)) state.chat.cost_usd = cost;
    if (payload.usage && typeof payload.usage === 'object') {
      if (payload.usage.input_tokens != null) state.chat.tokens_in = Number(payload.usage.input_tokens) || 0;
      if (payload.usage.output_tokens != null) state.chat.tokens_out = Number(payload.usage.output_tokens) || 0;
    }
    appendTimeline(window.ChatRender.resultRow(payload));
    renderHeader();
    syncComposer();
    scheduleNotify();
  }

  function handleToolUse(block, group) {
    const id = block.id || block.tool_use_id || `tool_${++state.toolSeq}`;
    if (state.toolCards.has(id)) return;
    const name = String(block.name || 'tool');
    const input = block.input && typeof block.input === 'object' ? block.input : {};
    const card = window.ChatRender.toolCard(name, input, id);
    state.toolCards.set(id, card);
    if (/^(Agent|Task)$/i.test(name)) {
      const existing = state.nestedContainers.get(id);
      if (existing && existing !== card.nested) {
        while (existing.firstChild) card.nested.append(existing.firstChild);
        existing.remove();
      }
      card.nested.hidden = !card.nested.firstChild;
      state.nestedContainers.set(id, card.nested);
    }
    group.body.append(card.el);
  }

  /* --------------------------------------------------------------- groups */
  function ensureGroup(parent, messageId) {
    const tl = timeline();
    if (!tl) return null;
    const parentKey = parent ? String(parent) : '';
    const messageKey = messageId ? String(messageId) : 'stream';
    let key = `${parentKey}|${messageKey}`;
    let group = state.groups.get(key);
    if (!group && messageId) {
      const streamKey = `${parentKey}|stream`;
      const streamed = state.groups.get(streamKey);
      if (streamed && !streamed.finalized) {
        state.groups.delete(streamKey);
        for (const [streamMapKey, entry] of Array.from(state.streams)) {
          if (entry.groupKey !== streamKey) continue;
          state.streams.delete(streamMapKey);
          const nextKey = key + streamMapKey.slice(streamKey.length);
          entry.groupKey = key;
          state.streams.set(nextKey, entry);
        }
        streamed.key = key;
        streamed.messageId = messageKey;
        state.groups.set(key, streamed);
        group = streamed;
      }
    }
    if (!group) {
      group = window.ChatRender.assistantGroup();
      group.key = key;
      group.parent = parentKey;
      group.messageId = messageKey;
      group.finalized = false;
      group.finalBlocks = new Set();
      state.groups.set(key, group);
      const container = containerForParent(parentKey);
      if (!container) return null;
      container.hidden = false;
      container.append(group.el);
    }
    return group;
  }

  function containerForParent(parentId) {
    if (!parentId) return timeline();
    let container = state.nestedContainers.get(parentId);
    if (!container) {
      container = state.deps.h('div', { class: 'chat-nested chat-nested-orphan' });
      const tl = timeline();
      if (tl) tl.append(container);
      state.nestedContainers.set(parentId, container);
    }
    container.hidden = false;
    return container;
  }

  function takeStream(groupKey, blockIndex, kind) {
    const key = `${groupKey}|${blockIndex}|${kind}`;
    const entry = state.streams.get(key);
    if (!entry) return null;
    state.streams.delete(key);
    return entry.controller;
  }

  function deltaInfo(delta) {
    if (typeof delta === 'string') return { text: delta, kind: 'text' };
    if (!delta || typeof delta !== 'object') return null;
    if (delta.delta && typeof delta.delta === 'object') {
      const nested = deltaInfo(delta.delta);
      if (nested) return nested;
    }
    if (typeof delta.delta === 'string') return { text: delta.delta, kind: 'text' };
    if (typeof delta.text === 'string' && (!delta.type || delta.type === 'text_delta')) {
      return { text: delta.text, kind: 'text' };
    }
    if (delta.type === 'thinking_delta') return { text: String(delta.thinking || ''), kind: 'thinking' };
    if (delta.type === 'text_delta') return { text: String(delta.text || ''), kind: 'text' };
    if (delta.type === 'input_json_delta') return null;
    if (typeof delta.thinking === 'string') return { text: delta.thinking, kind: 'thinking' };
    return null;
  }

  function applyDelta(frame) {
    const info = deltaInfo(frame.delta);
    if (!info || !info.text) return;
    const group = ensureGroup(frame.parent_tool_use_id, frame.message_id);
    if (!group) return;
    const blockIndex = Number(frame.block_index) || 0;
    // The runner may flush buffered deltas after the final block event.
    // Ignore them so streaming text cannot duplicate the rendered block.
    if (group.finalized || group.finalBlocks.has(blockIndex)) return;
    const key = `${group.key}|${blockIndex}|${info.kind}`;
    let entry = state.streams.get(key);
    if (!entry) {
      const controller = info.kind === 'thinking'
        ? window.ChatRender.streamThinking()
        : window.ChatRender.streamText();
      entry = { controller, groupKey: group.key };
      state.streams.set(key, entry);
      group.body.append(controller.el);
    }
    entry.controller.append(info.text);
    state.streaming = true;
    removeEmpty();
    scheduleComposerSync();
    scrollIfPinned();
  }

  /* ----------------------------------------------------------- permissions */
  function addPermission(permission) {
    const perm = permission && typeof permission === 'object' ? permission : {};
    const id = String(perm.id || perm.permission_id || '');
    if (!id) return;
    const normalized = Object.assign({}, perm, { id });
    state.pending.set(id, normalized);
    const existing = state.permissionCards.get(id);
    if (existing) {
      if (perm.expires_at) existing.setExpiry(perm.expires_at);
      renderHeader();
      return;
    }
    const card = window.ChatRender.permissionCard(normalized, {
      decide: (decision) => decidePermission(id, decision),
    });
    state.permissionCards.set(id, card);
    appendTimeline(card.el);
    renderHeader();
    syncComposer();
    scheduleNotify();
  }

  function decidePermission(id, decision) {
    const sessionId = state.sessionId;
    const card = state.permissionCards.get(id);
    if (!sessionId) return Promise.reject(new Error('No active chat session'));
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      const message = { t: 'permission', permission_id: id, behavior: decision.behavior };
      if (decision.updated_input !== undefined) message.updated_input = decision.updated_input;
      if (decision.updated_permissions !== undefined) message.updated_permissions = decision.updated_permissions;
      if (decision.message !== undefined) message.message = decision.message;
      try {
        state.ws.send(JSON.stringify(message));
      } catch (error) {
        if (card) card.reset();
        return Promise.reject(error);
      }
      return Promise.resolve();
    }
    return state.deps.api(
      `/api/sessions/${encodeURIComponent(sessionId)}/chat/permissions/${encodeURIComponent(id)}`,
      { method: 'POST', body: decision },
    ).then((result) => {
      if (result && result.status) resolvePermission(id, result.status, decision);
    }).catch((error) => {
      if (card) card.reset();
      throw error;
    });
  }

  function resolvePermission(id, status, decision) {
    const key = String(id || '');
    if (!key) return;
    state.pending.delete(key);
    const card = state.permissionCards.get(key);
    if (card) card.resolve(status, decision);
    renderHeader();
    syncComposer();
    scheduleNotify();
  }

  function resetDecidingCards() {
    for (const card of state.permissionCards.values()) {
      try { card.reset(); } catch (_) { /* ignore */ }
    }
  }

  /* --------------------------------------------------------------- header */
  function humanStatus(status) {
    const value = String(status || '');
    const labels = {
      thinking: 'Claude is thinking…',
      working: 'Claude is working…',
      running: 'Running…',
      streaming: 'Streaming…',
      tool: 'Running a tool…',
      tool_use: 'Running a tool…',
      compacting: 'Compacting context…',
      waiting_permission: 'Waiting for approval…',
      starting: 'Starting…',
      retrying: 'Retrying…',
      interrupting: 'Stopping…',
      handoff: 'Switching modes…',
      rewinding: 'Restoring files…',
      restarting: 'Restarting…',
      idle: 'idle',
      offline: 'offline',
      error: 'error',
    };
    return labels[value] || value || '—';
  }

  function renderHeader() {
    const root = node('chat-status');
    if (!root) return;
    const deps = state.deps;
    if (!deps) return;
    deps.clear(root);
    const chat = state.chat || {};
    const session = state.session || {};
    const mode = modeOf(chat, session);
    const status = chat.status || 'offline';
    const pending = state.pending.size;
    const model = chat.model || session.model || '';
    const permissionMode = chat.permission_mode || 'default';

    let permControl;
    if (mode === 'chat' && !isBusy()) {
      permControl = deps.h(
        'select',
        {
          class: 'chat-perm-select',
          title: 'Claude permission mode (restarts the headless session)',
          'aria-label': 'Permission mode',
          onchange: (event) => setPermissionMode(event.target.value),
        },
        ...PERMISSION_MODES.map((value) => deps.h('option', { value }, value)),
      );
      permControl.value = permissionMode;
    } else {
      permControl = deps.h('span', {
        class: 'badge chat-perm-mode',
        title: 'Claude permission mode',
        text: `perm: ${permissionMode}`,
      });
    }

    const left = deps.h('div', { class: 'chat-head-left' },
      deps.h('span', {
        class: `badge chat-mode-badge ${mode === 'tui' ? 'is-tty' : mode === 'handoff' ? 'is-handoff' : 'is-chat'}`,
        text: mode === 'chat' ? 'chat' : mode === 'tui' ? 'terminal' : mode,
      }),
      model ? deps.h('code', { class: 'chat-model', text: model }) : null,
      permControl);
    if (pending > 0) {
      left.append(deps.h('button', {
        class: 'badge chat-pending-badge',
        type: 'button',
        title: `${pending} pending approval(s)`,
        onclick: () => focusPending(),
      }, deps.h('span', { text: String(pending) }), deps.h('span', { text: ' pending' })));
    }
    left.append(deps.h('span', {
      class: `chat-conn ${state.connected ? 'is-live' : state.connecting ? 'is-connecting' : 'is-down'}`,
      title: state.connected ? 'Live connection' : state.connecting ? 'Connecting' : 'Not connected',
      text: state.connected ? 'live' : state.connecting ? 'connecting' : 'offline',
    }));

    const right = deps.h('div', { class: 'chat-head-right' },
      deps.h('span', { class: `chat-state is-${String(status).replace(/[^a-z_]/g, '')}`, text: humanStatus(status) }));
    if (Number(chat.cost_usd)) right.append(deps.h('span', { class: 'chat-metric', text: window.ChatRender.fmtCost(chat.cost_usd) }));
    if (Number(chat.tokens_in) || Number(chat.tokens_out)) {
      right.append(deps.h('span', {
        class: 'chat-metric',
        text: `${window.ChatRender.fmtTokens(chat.tokens_in)} in · ${window.ChatRender.fmtTokens(chat.tokens_out)} out`,
      }));
    }
    if (Number(chat.context_tokens)) {
      right.append(deps.h('span', { class: 'chat-metric', text: `${window.ChatRender.fmtTokens(chat.context_tokens)} ctx` }));
    }
    if (mode === 'chat' && state.sessionId) {
      right.append(deps.h('button', {
        class: 'chat-clear',
        type: 'button',
        title: 'Delete opencloddie chat history (Claude\'s transcript is untouched)',
        text: 'Clear history',
        onclick: clearHistory,
      }));
    }
    root.append(left, right);
    syncCheckpointActions();
    if (state.search.open) positionSearchPanel();
  }

  function isNearBottom() {
    const tl = timeline();
    if (!tl) return true;
    return tl.scrollHeight - tl.scrollTop - tl.clientHeight < 140;
  }

  /* ------------------------------------------------------------- composer */
  function canSendNow() {
    const mode = modeOf(state.chat, state.session);
    return Boolean(state.session && state.session.online) && mode === 'chat' && !state.loading;
  }

  function syncComposer() {
    const input = node('chat-input');
    const send = node('chat-send');
    const stop = node('chat-stop');
    const hint = node('chat-composer-hint');
    if (!input || !send || !stop) return;
    const online = Boolean(state.session && state.session.online);
    const mode = modeOf(state.chat, state.session);
    const busy = isBusy();
    const sendable = canSendNow();
    const hasAttachments = state.attachments.length > 0;
    input.disabled = !sendable;
    send.disabled = !sendable || (!input.value.trim() && !hasAttachments);
    stop.hidden = !busy;
    stop.disabled = !online || String((state.chat && state.chat.status) || '') === 'interrupting';
    const attach = node('chat-attach');
    if (attach) attach.disabled = !sendable;
    const fileInput = node('chat-image-input');
    if (fileInput) fileInput.disabled = !sendable;
    const searchInput = node('chat-search-input');
    if (searchInput) searchInput.disabled = !state.sessionId;
    const searchToggle = node('chat-search-toggle');
    if (searchToggle) searchToggle.disabled = !state.sessionId;
    let text = '';
    if (!state.session) text = '';
    else if (!online) text = 'Session offline — history is read-only';
    else if (mode === 'handoff') text = 'Switching modes…';
    else if (mode !== 'chat') text = 'Switch to chat to send messages';
    else if (state.loading) text = 'Loading chat…';
    else if (!state.connected) text = 'Reconnecting — messages use the REST fallback';
    else if (busy) text = 'Claude is working…';
    else text = 'Enter to send · Shift+Enter for newline';
    if (state.pending.size) text = `${state.pending.size} approval pending · ${text}`;
    if (hint) hint.textContent = text;
    const placeholder = sendable ? 'Message Claude…' : '…';
    if (input.placeholder !== placeholder) input.placeholder = placeholder;
  }

  function scheduleComposerSync() {
    if (state.composerScheduled) return;
    state.composerScheduled = true;
    const run = () => {
      state.composerScheduled = false;
      syncComposer();
    };
    if (window.requestAnimationFrame) window.requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  function autoResize() {
    const input = node('chat-input');
    if (!input) return;
    input.style.height = 'auto';
    const height = Math.min(160, Math.max(38, input.scrollHeight));
    input.style.height = `${height}px`;
  }

  function sendViaRest(content, clientMsgId) {
    const sessionId = state.sessionId;
    if (!sessionId) return;
    const el = window.ChatRender.userBubble(content);
    appendTimeline(el);
    state.lastUser = { el, uuid: '' };
    state.optimistic.push({
      text: window.ChatRender.contentText(content).trim(),
      el,
      used: false,
      at: Date.now(),
    });
    pruneOptimistic();
    state.deps.api(`/api/sessions/${encodeURIComponent(sessionId)}/chat/input`, {
      method: 'POST',
      body: { content, client_msg_id: clientMsgId },
    }).then(() => {
      state.streaming = true;
      scheduleComposerSync();
      markReadSoon();
    }).catch((error) => {
      if (state.deps.toast) state.deps.toast(error && error.message ? error.message : 'Message not sent', 'error');
    });
  }

  function buildOutgoingContent(text, attachments) {
    if (!attachments || !attachments.length) return text;
    const blocks = [];
    if (text) blocks.push({ type: 'text', text });
    for (const attachment of attachments) {
      blocks.push({
        type: 'image',
        source: {
          type: 'base64',
          media_type: attachment.media_type,
          data: attachment.data,
        },
      });
    }
    return blocks;
  }

  function setSuggestion(text) {
    state.suggestion = text ? String(text) : '';
    const el = node('chat-suggestion');
    if (!el) return;
    el.hidden = !state.suggestion;
    el.textContent = state.suggestion ? `Suggested: ${state.suggestion}` : '';
  }

  function useSuggestion() {
    const input = node('chat-input');
    if (!input || !state.suggestion) return;
    input.value = state.suggestion;
    input.focus();
    setSuggestion('');
    autoResize();
    syncComposer();
  }

  function sendMessage() {
    const input = node('chat-input');
    if (!input) return;
    const text = input.value.trim();
    const attachments = state.attachments.slice();
    if (!text && !attachments.length) return;
    if (!canSendNow()) {
      if (state.deps.toast) {
        const mode = modeOf(state.chat, state.session);
        if (!state.session || !state.session.online) state.deps.toast('Session is offline', 'warn');
        else if (mode !== 'chat') state.deps.toast('Switch to chat mode to send', 'warn');
        else state.deps.toast('Chat is not ready yet', 'warn');
      }
      return;
    }
    const content = buildOutgoingContent(text, attachments);
    const clientMsgId = uid();
    input.value = '';
    clearAttachments();
    setSuggestion('');
    autoResize();
    syncComposer();
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      try {
        state.ws.send(JSON.stringify({ t: 'input', client_msg_id: clientMsgId, content }));
        state.streaming = true;
        scheduleComposerSync();
        return;
      } catch (_) { /* fall back to REST */ }
    }
    sendViaRest(content, clientMsgId);
  }

  function stopTurn() {
    if (!isBusy()) return;
    const request = { t: 'interrupt', request_id: uid() };
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      try { state.ws.send(JSON.stringify(request)); } catch (_) { /* ignore */ }
    } else if (state.session && state.session.online && state.sessionId) {
      state.deps.api(`/api/sessions/${encodeURIComponent(state.sessionId)}/chat/interrupt`, { method: 'POST' })
        .catch((error) => { if (state.deps.toast) state.deps.toast(error.message, 'error'); });
    }
    state.streaming = false;
    state.chat = Object.assign({}, state.chat || {}, { status: 'interrupting' });
    renderHeader();
    syncComposer();
  }

  /* --------------------------------------------------------------- scroll */
  function scrollIfPinned() {
    if (!state.pinned || state.scrollScheduled) return;
    state.scrollScheduled = true;
    const run = () => {
      state.scrollScheduled = false;
      const tl = timeline();
      if (tl && state.pinned) tl.scrollTop = tl.scrollHeight;
    };
    if (window.requestAnimationFrame) window.requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  function scrollToBottom(force) {
    if (force) state.pinned = true;
    scrollIfPinned();
  }

  function appendTimeline(child) {
    if (!child) return;
    removeLoading();
    removeEmpty();
    const tl = timeline();
    if (!tl) return;
    tl.append(child);
    scrollIfPinned();
  }

  function removeLoading() {
    const tl = timeline();
    const row = tl && tl.querySelector('.chat-loading');
    if (row) row.remove();
  }

  function removeEmpty() {
    const tl = timeline();
    const empty = tl && tl.querySelector('.chat-empty');
    if (empty) empty.remove();
  }

  /* ------------------------------------------------------- read + notify */
  function markReadSoon() {
    if (!state.visible || document.hidden || !state.sessionId) return;
    if (state.readTimer) clearTimeout(state.readTimer);
    state.readTimer = setTimeout(() => {
      state.readTimer = null;
      if (!state.visible || document.hidden) return;
      const seq = Math.max(0, Number(state.lastSeq) || 0);
      if (!seq || seq <= state.lastMarkedRead) return;
      state.lastMarkedRead = seq;
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        try { state.ws.send(JSON.stringify({ t: 'read', last_seq: seq })); } catch (_) { /* ignore */ }
      } else if (state.session && state.session.online) {
        state.deps.api(`/api/sessions/${encodeURIComponent(state.sessionId)}/chat/read`, {
          method: 'POST', body: { last_seq: seq },
        }).catch(() => { /* best effort */ });
      }
      if (state.session && state.session.chat) state.session.chat.unread = 0;
      scheduleNotify();
      if (typeof state.deps.onRead === 'function') state.deps.onRead(state.sessionId, seq);
    }, 400);
  }

  function scheduleNotify() {
    if (!state.deps || typeof state.deps.onChatUpdate !== 'function' || !state.sessionId) return;
    if (state.notifyTimer) return;
    state.notifyTimer = setTimeout(() => {
      state.notifyTimer = null;
      if (!state.sessionId) return;
      const chat = state.chat || {};
      const unread = state.visible && !document.hidden
        ? 0
        : Number(chat.unread != null ? chat.unread
          : (state.session && state.session.chat && state.session.chat.unread)) || 0;
      state.deps.onChatUpdate(state.sessionId, {
        mode: modeOf(chat, state.session),
        status: chat.status || 'offline',
        pending: state.pending.size,
        unread,
        last_seq: state.lastSeq,
      });
    }, 150);
  }

  function takeOptimistic(text) {
    const target = String(text || '').trim();
    if (!target) return null;
    const index = state.optimistic.findIndex((entry) => !entry.used && entry.text === target);
    if (index < 0) return null;
    state.optimistic[index].used = true;
    const entry = state.optimistic[index];
    pruneOptimistic();
    return entry;
  }

  function pruneOptimistic() {
    const cutoff = Date.now() - 300000;
    state.optimistic = state.optimistic.filter((entry) => !entry.used && entry.at > cutoff).slice(-50);
  }

  /* ------------------------------------------------------------ attachments */
  function imageMediaType(file) {
    const type = String((file && file.type) || '').toLowerCase();
    if (CHAT_IMAGE_TYPES.has(type)) return type;
    if (type === 'image/jpg') return 'image/jpeg';
    if (type) return '';
    const name = String((file && file.name) || '').toLowerCase();
    if (/\.png$/.test(name)) return 'image/png';
    if (/\.jpe?g$/.test(name)) return 'image/jpeg';
    if (/\.gif$/.test(name)) return 'image/gif';
    if (/\.webp$/.test(name)) return 'image/webp';
    return '';
  }

  function readImageFile(file, mediaType) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error(`Could not read ${file.name || 'image'}`));
      reader.onload = () => {
        const result = String(reader.result || '');
        const comma = result.indexOf(',');
        const header = comma > 0 ? result.slice(0, comma) : '';
        if (!/;base64$/i.test(header) || comma < 0) {
          reject(new Error(`${file.name || 'Image'} could not be encoded`));
          return;
        }
        const data = result.slice(comma + 1);
        if (!data) {
          reject(new Error(`${file.name || 'Image'} is empty`));
          return;
        }
        resolve({
          id: `att_${++state.attachSeq}`,
          name: String(file.name || 'image'),
          media_type: mediaType,
          data,
          bytes: Number(file.size) || 0,
        });
      };
      reader.readAsDataURL(file);
    });
  }

  function addImageFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const rawCount = Number(state.limits.max_input_images);
    const maxCount = Number.isFinite(rawCount) ? Math.max(0, Math.floor(rawCount)) : 4;
    const maxBytes = Math.max(1, Number(state.limits.max_image_bytes) || 5 * 1024 * 1024);
    if (maxCount <= 0) {
      if (state.deps && state.deps.toast) state.deps.toast('Image attachments are disabled', 'warn');
      syncComposer();
      return;
    }
    const accepted = [];
    for (const file of files) {
      if (!file) continue;
      const mediaType = imageMediaType(file);
      if (!mediaType) {
        if (state.deps && state.deps.toast) {
          state.deps.toast(`Unsupported file “${file.name || 'file'}” — use PNG, JPEG, GIF, or WebP`, 'warn');
        }
        continue;
      }
      if (Number(file.size) > maxBytes) {
        if (state.deps && state.deps.toast) {
          state.deps.toast(`“${file.name || 'image'}” is too large (max ${window.ChatRender.fmtBytes(maxBytes)})`, 'warn');
        }
        continue;
      }
      if (state.attachments.length + accepted.length >= maxCount) {
        if (state.deps && state.deps.toast) {
          state.deps.toast(`Up to ${maxCount} images per message`, 'warn');
        }
        break;
      }
      accepted.push({ file, mediaType });
    }
    if (!accepted.length) {
      syncComposer();
      return;
    }
    Promise.all(accepted.map((entry) => readImageFile(entry.file, entry.mediaType).catch((error) => {
      if (state.deps && state.deps.toast) {
        state.deps.toast(error && error.message ? error.message : 'Could not read image', 'error');
      }
      return null;
    }))).then((items) => {
      for (const item of items) {
        if (!item) continue;
        if (state.attachments.length >= maxCount) break;
        state.attachments.push(item);
      }
      renderAttachments();
      syncComposer();
    });
  }

  function renderAttachments() {
    const tray = node('chat-attachments');
    if (!tray || !state.deps) return;
    state.deps.clear(tray);
    tray.hidden = state.attachments.length === 0;
    for (const attachment of state.attachments) {
      const thumb = state.deps.h('img', {
        class: 'chat-attachment-thumb',
        src: `data:${attachment.media_type};base64,${attachment.data}`,
        alt: '',
        loading: 'lazy',
      });
      const remove = state.deps.h('button', {
        class: 'chat-attachment-remove',
        type: 'button',
        title: `Remove ${attachment.name}`,
        'aria-label': `Remove ${attachment.name}`,
        text: '×',
        onclick: () => removeAttachment(attachment.id),
      });
      tray.append(state.deps.h('div', {
        class: 'chat-attachment',
        'data-attachment-id': attachment.id,
        title: `${attachment.name} · ${window.ChatRender.fmtBytes(attachment.bytes)}`,
      }, thumb, state.deps.h('span', { class: 'chat-attachment-name', text: attachment.name }), remove));
    }
  }

  function removeAttachment(id) {
    state.attachments = state.attachments.filter((attachment) => attachment.id !== id);
    renderAttachments();
    syncComposer();
  }

  function clearAttachments() {
    state.attachments = [];
    renderAttachments();
    syncComposer();
  }

  function resetAttachments() {
    state.attachments = [];
    state.dragDepth = 0;
    const composer = node('chat-composer');
    if (composer) composer.classList.remove('is-drag-over');
    renderAttachments();
  }

  function hasFiles(event) {
    const data = event && event.dataTransfer;
    if (!data) return false;
    const types = data.types ? Array.from(data.types) : [];
    return types.indexOf('Files') >= 0;
  }

  function handlePasteImages(event) {
    const data = event.clipboardData;
    if (!data) return;
    const files = [];
    for (const item of Array.from(data.items || [])) {
      if (item.kind !== 'file') continue;
      if (!/^image\//i.test(String(item.type || ''))) continue;
      const file = item.getAsFile();
      if (file) files.push(file);
    }
    if (!files.length) return;
    event.preventDefault();
    addImageFiles(files);
  }

  function buildComposerExtras() {
    const deps = state.deps;
    const composer = node('chat-composer');
    if (!deps || !composer || node('chat-image-input')) return;
    const box = composer.querySelector('.chat-composer-box');
    if (box) {
      const suggestion = deps.h('button', {
        id: 'chat-suggestion',
        class: 'chat-suggestion',
        type: 'button',
        hidden: true,
        title: 'Insert this suggested prompt',
        onclick: useSuggestion,
      });
      box.insertBefore(suggestion, box.firstChild);
      const tray = deps.h('div', { id: 'chat-attachments', class: 'chat-attachments', hidden: true });
      box.insertBefore(tray, box.firstChild);
    }
    const fileInput = deps.h('input', {
      id: 'chat-image-input',
      class: 'chat-image-input',
      type: 'file',
      accept: 'image/png,image/jpeg,image/gif,image/webp',
      multiple: true,
      hidden: true,
      'aria-hidden': 'true',
    });
    fileInput.addEventListener('change', () => {
      const files = fileInput.files;
      fileInput.value = '';
      addImageFiles(files);
    });
    composer.append(fileInput);
    const attach = deps.h('button', {
      id: 'chat-attach',
      class: 'btn btn-ghost btn-sm chat-attach',
      type: 'button',
      title: 'Attach images (PNG, JPEG, GIF, WebP)',
      'aria-label': 'Attach images',
    },
    deps.h('span', { class: 'chat-attach-plus', 'aria-hidden': 'true', text: '+' }),
    deps.h('span', { class: 'chat-attach-label', text: 'Image' }));
    attach.addEventListener('click', () => {
      if (!attach.disabled) fileInput.click();
    });
    const bar = box ? box.querySelector('.chat-composer-bar') : null;
    if (bar) bar.insertBefore(attach, bar.firstChild);

    const input = node('chat-input');
    if (input) input.addEventListener('paste', handlePasteImages);

    const pane = node('chat-pane');
    if (pane) {
      let depth = 0;
      const setDragging = (on) => {
        if (on) composer.classList.add('is-drag-over');
        else composer.classList.remove('is-drag-over');
      };
      pane.addEventListener('dragenter', (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        depth += 1;
        state.dragDepth = depth;
        setDragging(true);
      });
      pane.addEventListener('dragover', (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
        setDragging(true);
      });
      pane.addEventListener('dragleave', () => {
        if (depth <= 0) return;
        depth -= 1;
        state.dragDepth = depth;
        if (!depth) setDragging(false);
      });
      pane.addEventListener('drop', (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        depth = 0;
        state.dragDepth = 0;
        setDragging(false);
        const files = event.dataTransfer ? event.dataTransfer.files : null;
        addImageFiles(files);
      });
    }
  }

  /* ---------------------------------------------------------------- search */
  function buildSearchUI() {
    const deps = state.deps;
    const pane = node('chat-pane');
    if (!deps || !pane || node('chat-search-wrap')) return;
    const input = deps.h('input', {
      id: 'chat-search-input',
      class: 'chat-search-input',
      type: 'search',
      placeholder: 'Search chat…',
      autocomplete: 'off',
      spellcheck: 'false',
      'aria-label': 'Search conversation',
    });
    const toggle = deps.h('button', {
      id: 'chat-search-toggle',
      class: 'icon-btn chat-search-toggle',
      type: 'button',
      title: 'Search conversation',
      'aria-label': 'Search conversation',
      'aria-controls': 'chat-search-panel',
      'aria-expanded': 'false',
    }, deps.h('span', { class: 'chat-search-glyph', 'aria-hidden': 'true', text: '⌕' }));
    const close = deps.h('button', {
      id: 'chat-search-close',
      class: 'icon-btn chat-search-close',
      type: 'button',
      title: 'Close search',
      'aria-label': 'Close search',
      text: '×',
    });
    const wrap = deps.h('div', { id: 'chat-search-wrap', class: 'chat-search-wrap', role: 'search' },
      toggle, input, close);
    const head = deps.h('div', { id: 'chat-search-head', class: 'chat-search-head', text: 'Search' });
    const note = deps.h('div', { id: 'chat-search-note', class: 'chat-search-note' });
    const list = deps.h('div', { id: 'chat-search-results', class: 'chat-search-results' });
    const panel = deps.h('div', { id: 'chat-search-panel', class: 'chat-search-panel', hidden: true },
      head, note, list);
    pane.append(wrap, panel);
    state.search.wrap = wrap;
    state.search.input = input;
    state.search.panel = panel;
    state.search.head = head;
    state.search.note = note;
    state.search.list = list;

    toggle.addEventListener('click', () => {
      if (state.search.open) closeSearch();
      else openSearch();
    });
    close.addEventListener('click', () => closeSearch());
    input.addEventListener('input', onSearchInput);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeSearch();
        const chatInput = node('chat-input');
        if (chatInput) chatInput.focus();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (state.search.timer) { clearTimeout(state.search.timer); state.search.timer = null; }
        const query = input.value.trim();
        if (query) runSearch(query);
      }
    });
    panel.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      closeSearch();
    });
    pane.addEventListener('click', (event) => {
      if (!state.search.open) return;
      const target = event.target;
      if (state.search.wrap.contains(target) || state.search.panel.contains(target)) return;
      closeSearch();
    });
  }

  function openSearch() {
    if (!state.search.wrap || !state.sessionId) return;
    state.search.open = true;
    state.search.wrap.classList.add('is-open');
    const toggle = node('chat-search-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
    const pane = node('chat-pane');
    if (pane) pane.classList.add('is-searching');
    state.search.panel.hidden = false;
    positionSearchPanel();
    renderSearchPanel();
    try { state.search.input.focus({ preventScroll: true }); } catch (_) { state.search.input.focus(); }
  }

  function closeSearch() {
    if (state.search.timer) {
      clearTimeout(state.search.timer);
      state.search.timer = null;
    }
    state.search.token += 1;
    state.search.open = false;
    state.search.query = '';
    state.search.lastQuery = '';
    state.search.loading = false;
    state.search.results = null;
    state.search.error = '';
    state.search.hasMore = false;
    if (state.search.wrap) state.search.wrap.classList.remove('is-open');
    const toggle = node('chat-search-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
    const pane = node('chat-pane');
    if (pane) pane.classList.remove('is-searching');
    if (state.search.panel) state.search.panel.hidden = true;
    if (state.search.input) state.search.input.value = '';
    if (state.search.head) state.search.head.textContent = 'Search';
    if (state.search.note) state.search.note.textContent = '';
    if (state.search.list && state.deps) state.deps.clear(state.search.list);
  }

  function resetSearch() {
    closeSearch();
  }

  function positionSearchPanel() {
    const panel = state.search.panel;
    if (!panel) return;
    const header = node('chat-status');
    const top = header ? header.offsetTop + header.offsetHeight + 4 : 52;
    panel.style.top = `${Math.max(44, top)}px`;
  }

  function onSearchInput() {
    const input = state.search.input;
    if (!input) return;
    state.search.query = input.value;
    if (state.search.timer) {
      clearTimeout(state.search.timer);
      state.search.timer = null;
    }
    const query = input.value.trim();
    if (!query) {
      state.search.token += 1;
      state.search.lastQuery = '';
      state.search.loading = false;
      state.search.results = null;
      state.search.error = '';
      state.search.hasMore = false;
      renderSearchPanel();
      return;
    }
    state.search.timer = setTimeout(() => {
      state.search.timer = null;
      runSearch(query);
    }, 300);
  }

  function runSearch(query) {
    const sessionId = state.sessionId;
    if (!sessionId || !state.deps || typeof state.deps.api !== 'function') return;
    const token = ++state.search.token;
    state.search.lastQuery = query;
    state.search.loading = true;
    state.search.error = '';
    state.search.results = null;
    state.search.hasMore = false;
    renderSearchPanel();
    state.deps.api(
      `/api/sessions/${encodeURIComponent(sessionId)}/chat/search?q=${encodeURIComponent(query)}&limit=50`,
    ).then((data) => {
      if (token !== state.search.token || state.sessionId !== sessionId) return;
      state.search.loading = false;
      state.search.results = Array.isArray(data && data.events) ? data.events : [];
      state.search.hasMore = Boolean(data && data.has_more);
      renderSearchPanel();
    }).catch((error) => {
      if (token !== state.search.token || state.sessionId !== sessionId) return;
      state.search.loading = false;
      state.search.error = error && error.message ? error.message : 'Search failed';
      renderSearchPanel();
    });
  }

  function renderSearchPanel() {
    const search = state.search;
    if (!search.open || !search.head || !search.note || !search.list || !state.deps) return;
    positionSearchPanel();
    state.deps.clear(search.list);
    const query = search.lastQuery || search.query.trim();
    if (!query) {
      search.head.textContent = 'Search';
      search.note.textContent = 'Type to search messages.';
      return;
    }
    if (search.loading) {
      search.head.textContent = 'Searching…';
      search.note.textContent = '';
      return;
    }
    if (search.error) {
      search.head.textContent = 'Search failed';
      search.note.textContent = search.error;
      return;
    }
    const events = Array.isArray(search.results) ? search.results : [];
    search.head.textContent = events.length
      ? `${events.length} match${events.length === 1 ? '' : 'es'}`
      : 'No matches';
    search.note.textContent = '';
    if (!events.length) {
      search.note.textContent = `No matches for “${query}”.`;
      return;
    }
    if (search.hasMore) search.note.textContent = 'Showing the first 50 matches.';
    for (const event of events) {
      const seq = Number(event && event.seq) || 0;
      const excerpt = window.ChatRender.eventExcerpt(event && event.payload, query) || '(no text)';
      const result = state.deps.h('button', {
        class: 'chat-search-result',
        type: 'button',
        'data-seq': String(seq),
        title: seq ? `Go to event #${seq}` : '',
      },
      state.deps.h('span', { class: 'chat-search-result-meta' },
        state.deps.h('span', { class: 'chat-search-result-seq', text: seq ? `#${seq}` : '?' }),
        state.deps.h('span', { class: 'chat-search-result-kind', text: String((event && event.kind) || 'event') }),
        state.deps.h('span', { class: 'chat-search-result-time', text: fmtWhen(event && event.created_at) })),
      state.deps.h('span', { class: 'chat-search-result-text', text: excerpt }));
      result.addEventListener('click', () => jumpToSeq(seq, result));
      search.list.append(result);
    }
  }

  function fmtWhen(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return sameDay ? time : `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
  }

  function jumpToSeq(seq, resultEl) {
    const target = state.seqEls.get(Number(seq));
    if (target && target.isConnected) {
      state.pinned = false;
      try { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { target.scrollIntoView(); }
      target.classList.add('is-search-target');
      setTimeout(() => {
        try { target.classList.remove('is-search-target'); } catch (_) { /* ignore */ }
      }, 2400);
      if (resultEl) resultEl.classList.remove('is-missing');
      if (state.search.note) state.search.note.textContent = '';
      return;
    }
    if (resultEl) resultEl.classList.add('is-missing');
    if (state.search.note) {
      state.search.note.textContent = `Event #${seq} is not in the loaded timeline; the excerpt above is from the server search.`;
    }
  }

  /* ---------------------------------------------------------- checkpoints */
  function handleCheckpoint(payload, seq) {
    const data = payload && typeof payload === 'object' ? payload : {};
    const uuid = String(data.uuid || data.checkpoint_uuid || '');
    if (!uuid) return;
    const rec = {
      uuid,
      label: String(data.label || ''),
      seq: Number(seq) || 0,
      el: state.lastUser && state.lastUser.el ? state.lastUser.el : null,
    };
    state.checkpoints.set(uuid, rec);
    syncCheckpointAction(rec);
  }

  function rewindBlocked() {
    const status = String((state.chat && state.chat.status) || '');
    return Boolean(state.rewind.busy) || isBusy()
      || status === 'restarting' || status === 'interrupting';
  }

  function syncCheckpointAction(rec) {
    if (!rec || !rec.el || !rec.el.isConnected || !state.deps) return;
    const actions = rec.el.actions || rec.el.querySelector('.chat-user-actions');
    if (!actions) return;
    const chatMode = modeOf(state.chat, state.session) === 'chat';
    let button = actions.querySelector('.chat-rewind-btn');
    if (!chatMode) {
      if (button) button.hidden = true;
      actions.hidden = true;
      return;
    }
    if (!button) {
      button = state.deps.h('button', {
        class: 'chat-rewind-btn',
        type: 'button',
      });
      button.addEventListener('click', () => requestRewind(rec));
      actions.append(button);
    }
    const blocked = rewindBlocked();
    actions.hidden = false;
    button.hidden = false;
    button.disabled = blocked;
    button.textContent = state.rewind.busy ? 'Restoring files…' : 'Restore files to here';
    button.title = blocked && !state.rewind.busy
      ? 'Wait for the current turn to finish before restoring files'
      : 'Restore files to this checkpoint (files only; the conversation is not rewound)';
  }

  function syncCheckpointActions() {
    for (const rec of state.checkpoints.values()) syncCheckpointAction(rec);
  }

  function requestRewind(rec) {
    if (!rec || !rec.uuid || !state.sessionId) return;
    if (modeOf(state.chat, state.session) !== 'chat') {
      if (state.deps.toast) state.deps.toast('Switch to chat mode to restore files', 'warn');
      return;
    }
    if (rewindBlocked()) {
      if (state.deps.toast) state.deps.toast('Wait for the current turn to finish', 'warn');
      return;
    }
    const run = () => {
      const sessionId = state.sessionId;
      state.rewind.busy = true;
      state.rewind.pending = true;
      if (state.rewind.timer) clearTimeout(state.rewind.timer);
      state.rewind.timer = setTimeout(() => {
        state.rewind.timer = null;
        state.rewind.busy = false;
        state.rewind.pending = false;
        syncCheckpointActions();
        if (state.deps.toast) state.deps.toast('Restoring files is taking longer than expected', 'warn');
      }, 45000);
      syncCheckpointActions();
      state.deps.api(`/api/sessions/${encodeURIComponent(sessionId)}/chat/rewind`, {
        method: 'POST',
        body: { checkpoint_uuid: rec.uuid },
      }).then((result) => {
        if (result && result.ok === false) {
          throw new Error(result.error && result.error.message ? result.error.message : 'Could not restore files');
        }
        if (state.deps.toast) state.deps.toast('Restoring files…');
      }).catch((error) => {
        if (state.rewind.timer) {
          clearTimeout(state.rewind.timer);
          state.rewind.timer = null;
        }
        state.rewind.busy = false;
        state.rewind.pending = false;
        syncCheckpointActions();
        const message = error && error.message ? error.message : 'Could not restore files';
        if (state.deps.toast) state.deps.toast(message, 'error');
        appendTimeline(window.ChatRender.errorRow({ message }, { title: 'Restore failed' }));
      });
    };
    if (typeof state.deps.confirmDialog === 'function') {
      state.deps.confirmDialog(
        'Restore files to this checkpoint?',
        'The files on the host will be restored to their state at this point. The conversation is not rewound, and the chat session restarts.',
        { confirmLabel: 'Restore files', danger: true },
      ).then((confirmed) => {
        if (confirmed) run();
      }).catch(() => { /* dismissed */ });
    } else {
      run();
    }
  }

  function cancelRewind() {
    if (state.rewind.timer) { clearTimeout(state.rewind.timer); state.rewind.timer = null; }
    if (state.rewind.refreshTimer) { clearTimeout(state.rewind.refreshTimer); state.rewind.refreshTimer = null; }
    state.rewind.busy = false;
    state.rewind.pending = false;
  }

  function handleRewind(payload) {
    const data = payload && typeof payload === 'object' ? payload : {};
    const ok = data.ok !== false;
    if (state.rewind.timer) { clearTimeout(state.rewind.timer); state.rewind.timer = null; }
    state.rewind.busy = false;
    // Only a rewind we just requested triggers a reload; replaying an old
    // rewind event from the snapshot just renders its status row.
    const shouldRefresh = ok && state.rewind.pending;
    state.rewind.pending = false;
    const message = String(data.message || (ok ? 'Files restored.' : 'Could not restore files.'));
    if (ok) {
      appendTimeline(window.ChatRender.statusRow(message, { kind: 'rewind' }));
      state.chat = Object.assign({}, state.chat || {}, { status: 'idle' });
      if (shouldRefresh) scheduleRewindRefresh();
    } else {
      appendTimeline(window.ChatRender.errorRow({ message }, { title: 'Restore failed' }));
    }
    syncCheckpointActions();
    if (state.deps.toast && (shouldRefresh || !ok)) {
      state.deps.toast(message, ok ? 'info' : 'error');
    }
    renderHeader();
    syncComposer();
  }

  function scheduleRewindRefresh() {
    if (state.rewind.refreshTimer) clearTimeout(state.rewind.refreshTimer);
    state.rewind.refreshTimer = setTimeout(() => {
      state.rewind.refreshTimer = null;
      refreshChat();
    }, 1200);
  }

  function refreshChat() {
    if (!state.sessionId || !state.session || state.loading) return;
    closeSocket();
    resetTimeline();
    loadSnapshot(state.session);
  }

  /* ---------------------------------------------------------------- timers */
  function startTick() {
    stopTick();
    state.tickTimer = setInterval(() => {
      if (!state.visible || document.hidden) return;
      for (const [id, card] of state.permissionCards) {
        if (!state.pending.has(id)) continue;
        try { card.tick(); } catch (_) { /* ignore */ }
      }
    }, 1000);
  }

  function stopTick() {
    if (state.tickTimer) {
      clearInterval(state.tickTimer);
      state.tickTimer = null;
    }
  }

  function stopTimers() {
    stopTick();
    if (state.readTimer) { clearTimeout(state.readTimer); state.readTimer = null; }
    if (state.notifyTimer) { clearTimeout(state.notifyTimer); state.notifyTimer = null; }
  }

  /* ------------------------------------------------------------------ bind */
  function bindOnce() {
    if (state.bound) return;
    state.bound = true;
    const input = node('chat-input');
    if (input) {
      input.addEventListener('input', () => { autoResize(); syncComposer(); });
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
        event.preventDefault();
        sendMessage();
      });
    }
    const send = node('chat-send');
    if (send) send.addEventListener('click', sendMessage);
    const stop = node('chat-stop');
    if (stop) stop.addEventListener('click', stopTurn);
    const tl = timeline();
    if (tl) tl.addEventListener('scroll', () => { state.pinned = isNearBottom(); }, { passive: true });
    window.addEventListener('resize', () => {
      if (state.search.open) positionSearchPanel();
    }, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { stopTick(); return; }
      if (!state.visible) return;
      startTick();
      if (!state.ws && !state.connecting && !state.loading) connect(state.lastSeq);
      markReadSoon();
    });
  }

  window.ChatView = { init, show, hide, destroy, handoff, focusPending };
})();
