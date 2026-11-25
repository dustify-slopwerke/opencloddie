/* Chat rendering: safe Markdown and message/tool components.
 *
 * Loaded before app.js as a classic script.  All model-produced text goes
 * through marked + DOMPurify (or the small fallback renderer below).  Tool
 * input/result fields are rendered with textContent, never raw innerHTML.
 */
(() => {
  'use strict';

  let deps = null;
  const h = (...args) => deps.h(...args);
  const clear = (node) => deps.clear(node);

  const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);
  const IMAGE_MEDIA_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  const MAX_DIFF_LINES = 600;
  const MAX_DIFF_CELLS = 250000;
  let questionSeq = 0;

  /* ------------------------------------------------------------- helpers */
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function safeHref(raw) {
    if (raw == null) return null;
    const value = String(raw).trim();
    if (!value) return null;
    if (value.startsWith('#')) return value;
    try {
      const url = new URL(value, window.location.href);
      return SAFE_PROTOCOLS.has(url.protocol) ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function hardenLinks(root) {
    for (const anchor of root.querySelectorAll('a')) {
      const safe = safeHref(anchor.getAttribute('href'));
      if (!safe) {
        anchor.removeAttribute('href');
        anchor.classList.add('chat-link-blocked');
        anchor.setAttribute('title', 'Blocked link');
        continue;
      }
      anchor.setAttribute('href', safe);
      anchor.setAttribute('target', '_blank');
      anchor.setAttribute('rel', 'noopener noreferrer');
    }
  }

  function fmtTokens(value) {
    const n = Number(value || 0);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
    return n.toLocaleString();
  }

  function fmtCost(value) {
    const n = Number(value || 0);
    if (!n) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
  }

  function fmtBytes(value) {
    const n = Number(value || 0);
    if (!n) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    let amount = n;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return `${unit === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[unit]}`;
  }

  /* -------------------------------------------------------------- images */
  // Image blocks are rendered strictly from base64 data as data: URLs.  A
  // remote source is never fetched; it becomes a placeholder chip instead.
  function imageInfo(block) {
    const data = block && typeof block === 'object' ? block : {};
    const source = data.source && typeof data.source === 'object' ? data.source : {};
    const mediaType = String(data.media_type || source.media_type || '').toLowerCase();
    const bytes = Number(data.bytes != null ? data.bytes : source.bytes) || 0;
    let base64 = '';
    if (source.type === 'base64' && typeof source.data === 'string') base64 = source.data;
    else if (!source.type && typeof data.data === 'string') base64 = data.data;
    return {
      mediaType,
      bytes,
      base64: base64.replace(/\s+/g, ''),
      placeholder: Boolean(data.placeholder),
    };
  }

  function imagePlaceholder(info, reason) {
    const meta = `${info.mediaType || 'image'}${info.bytes ? ` · ${fmtBytes(info.bytes)}` : ''}`;
    return h('div', {
      class: 'chat-image-placeholder',
      title: reason || 'Image is not available inline',
    },
    h('span', { class: 'chat-image-icon', 'aria-hidden': 'true', text: '▧' }),
    h('span', { class: 'chat-image-meta', text: meta }),
    h('span', { class: 'chat-image-note', text: reason || 'not included inline' }));
  }

  function imageBlock(block) {
    const info = imageInfo(block);
    const usable = !info.placeholder
      && IMAGE_MEDIA_TYPES.has(info.mediaType)
      && info.base64.length > 0
      && /^[A-Za-z0-9+/=_-]+$/.test(info.base64);
    if (!usable) {
      return imagePlaceholder(info, info.placeholder ? 'not included inline' : 'preview unavailable');
    }
    const caption = `${info.mediaType}${info.bytes ? ` · ${fmtBytes(info.bytes)}` : ''}`;
    const img = h('img', {
      class: 'chat-image',
      src: `data:${info.mediaType};base64,${info.base64}`,
      alt: `Attached image (${info.mediaType})`,
      loading: 'lazy',
      decoding: 'async',
    });
    img.addEventListener('error', () => {
      const fallback = imagePlaceholder(info, 'preview unavailable');
      if (img.parentNode) img.parentNode.replaceWith(fallback);
    });
    return h('figure', { class: 'chat-image-block' },
      img,
      info.bytes ? h('figcaption', { class: 'chat-image-caption', text: caption }) : null);
  }

  // Render tool-result / message content that may mix text and image blocks.
  function contentNodes(content, isError) {
    const nodes = [];
    if (typeof content === 'string') {
      if (content.trim()) nodes.push(preOutput(content, isError));
      return nodes;
    }
    if (Array.isArray(content)) {
      const buffer = [];
      const flush = () => {
        if (!buffer.length) return;
        nodes.push(preOutput(buffer.join('\n'), isError));
        buffer.length = 0;
      };
      for (const part of content) {
        if (part == null) continue;
        if (typeof part === 'string') { buffer.push(part); continue; }
        if (part.type === 'image') { flush(); nodes.push(imageBlock(part)); continue; }
        if (part.type === 'text') { buffer.push(String(part.text || '')); continue; }
        if (part.type === 'tool_result') {
          flush();
          nodes.push(...contentNodes(part.content, Boolean(part.is_error) || isError));
          continue;
        }
        buffer.push(contentText(part));
      }
      flush();
      return nodes;
    }
    if (content && typeof content === 'object') {
      if (content.type === 'image') return [imageBlock(content)];
      const text = contentText(content);
      if (text.trim()) nodes.push(preOutput(text, isError));
    }
    return nodes;
  }

  function baseName(path) {
    if (!path) return '';
    const parts = String(path).split(/[\\/]/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(path);
  }

  function firstLine(value) {
    const text = String(value == null ? '' : value).trim();
    if (!text) return '';
    return text.split('\n')[0].slice(0, 160);
  }

  function displayName(name) {
    const value = String(name || 'tool');
    if (value.startsWith('mcp__')) {
      const parts = value.split('__');
      if (parts.length >= 3) return `${parts.slice(1, -1).join('__')}/${parts[parts.length - 1]}`;
    }
    return value;
  }

  function slug(name) {
    return String(name || 'tool').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'tool';
  }

  function isAgentName(name) {
    return /^(Agent|Task)$/i.test(String(name || ''));
  }

  function contentText(content) {
    if (content == null) return '';
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content.map((part) => {
        if (typeof part === 'string') return part;
        if (part && typeof part === 'object') {
          if (part.type === 'text') return part.text || '';
          if (part.type === 'image') return '[image]';
          if (part.type === 'tool_result') return contentText(part.content);
          if (typeof part.text === 'string') return part.text;
          if (typeof part.content === 'string') return part.content;
        }
        try { return JSON.stringify(part); } catch (_) { return ''; }
      }).filter(Boolean).join('\n');
    }
    if (typeof content === 'object') {
      if (typeof content.text === 'string') return content.text;
      if (content.content !== undefined) return contentText(content.content);
      try { return JSON.stringify(content, null, 2); } catch (_) { return String(content); }
    }
    return String(content);
  }

  // One-line search excerpt from a stored event payload.  Text is only ever
  // going into textContent by the caller; this just selects the useful field.
  function eventExcerpt(payload, query, maxLength) {
    let text = '';
    if (payload && typeof payload === 'object') {
      if (payload.content !== undefined) text = contentText(payload.content);
      else if (payload.block !== undefined) text = contentText(payload.block);
      else if (payload.result !== undefined) text = contentText(payload.result);
      else if (typeof payload.message === 'string') text = payload.message;
      else if (typeof payload.label === 'string') text = payload.label;
      else if (typeof payload.text === 'string') text = payload.text;
      else if (typeof payload.status === 'string') text = payload.status;
      else {
        try { text = JSON.stringify(payload); } catch (_) { text = ''; }
      }
    } else {
      text = contentText(payload);
    }
    text = String(text || '').replace(/\s+/g, ' ').trim();
    const limit = Math.max(40, Number(maxLength) || 180);
    if (text.length <= limit) return text;
    const needle = String(query || '').trim().toLowerCase();
    let start = 0;
    if (needle) {
      const at = text.toLowerCase().indexOf(needle);
      if (at > Math.floor(limit / 3)) start = at - Math.floor(limit / 3);
    }
    let out = text.slice(start, start + limit);
    if (start > 0) out = `…${out}`;
    if (start + limit < text.length) out = `${out}…`;
    return out;
  }

  /* ------------------------------------------------------------- markdown */
  function init(next) {
    deps = next;
    if (window.marked && typeof window.marked.setOptions === 'function') {
      try { window.marked.setOptions({ gfm: true, breaks: true }); } catch (_) { /* ignore */ }
    }
  }

  function md(text) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-md';
    const source = text == null ? '' : String(text);
    if (!source) return wrap;
    if (window.marked && typeof window.marked.parse === 'function' && window.DOMPurify) {
      let html = '';
      try { html = window.marked.parse(source); } catch (_) { html = ''; }
      if (html) {
        wrap.innerHTML = window.DOMPurify.sanitize(html, {
          FORBID_TAGS: [
            'script', 'style', 'iframe', 'object', 'embed', 'form', 'input',
            'button', 'textarea', 'select', 'option', 'video', 'audio',
            'source', 'base', 'meta', 'link',
          ],
          FORBID_ATTR: ['style', 'srcset', 'formaction', 'xlink:href'],
          ALLOW_DATA_ATTR: false,
        });
        hardenLinks(wrap);
        return wrap;
      }
    }
    wrap.append(fallbackMarkdown(source));
    return wrap;
  }

  function appendInline(parent, text) {
    const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^)\s]+\))/g;
    let last = 0;
    let match = re.exec(text);
    while (match) {
      if (match.index > last) appendText(parent, text.slice(last, match.index));
      const token = match[0];
      if (token.startsWith('`')) {
        parent.append(h('code', { text: token.slice(1, -1) }));
      } else if (token.startsWith('**') || token.startsWith('__')) {
        const strong = h('strong');
        appendInline(strong, token.slice(2, -2));
        parent.append(strong);
      } else if (token.startsWith('*') || token.startsWith('_')) {
        const em = h('em');
        appendInline(em, token.slice(1, -1));
        parent.append(em);
      } else {
        const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const safe = link ? safeHref(link[2]) : null;
        if (link && safe) {
          const anchor = h('a', { href: safe, target: '_blank', rel: 'noopener noreferrer' });
          appendInline(anchor, link[1]);
          parent.append(anchor);
        } else {
          appendText(parent, token);
        }
      }
      last = re.lastIndex;
      match = re.exec(text);
    }
    if (last < text.length) appendText(parent, text.slice(last));
  }

  function appendText(parent, text) {
    const parts = text.split('\n');
    parts.forEach((part, index) => {
      if (part) parent.append(document.createTextNode(part));
      if (index < parts.length - 1) parent.append(document.createElement('br'));
    });
  }

  // Small safe Markdown fallback used only when marked or DOMPurify failed
  // to load.  It never sets innerHTML.
  function fallbackMarkdown(source) {
    const frag = document.createDocumentFragment();
    const lines = String(source).replace(/\r\n?/g, '\n').split('\n');
    const blank = (line) => /^\s*$/.test(line);
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (blank(line)) { index += 1; continue; }
      const fence = line.match(/^\s*```(\S*)\s*$/);
      if (fence) {
        index += 1;
        const buffer = [];
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          buffer.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = buffer.join('\n');
        pre.append(code);
        frag.append(pre);
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const node = document.createElement(`h${heading[1].length}`);
        appendInline(node, heading[2]);
        frag.append(node);
        index += 1;
        continue;
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        frag.append(document.createElement('hr'));
        index += 1;
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const quote = document.createElement('blockquote');
        const buffer = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          buffer.push(lines[index].replace(/^\s*>\s?/, ''));
          index += 1;
        }
        appendInline(quote, buffer.join('\n'));
        frag.append(quote);
        continue;
      }
      if (/^\s*(?:[-*+]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\.\s+/.test(line);
        const list = document.createElement(ordered ? 'ol' : 'ul');
        const itemRe = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/;
        while (index < lines.length && itemRe.test(lines[index])) {
          const item = document.createElement('li');
          appendInline(item, lines[index].replace(itemRe, ''));
          list.append(item);
          index += 1;
        }
        frag.append(list);
        continue;
      }
      const buffer = [];
      while (index < lines.length && !blank(lines[index])
        && !/^\s*```/.test(lines[index])
        && !/^(#{1,6})\s+/.test(lines[index])
        && !/^\s*>\s?/.test(lines[index])
        && !/^\s*(?:[-*+]|\d+\.)\s+/.test(lines[index])) {
        buffer.push(lines[index]);
        index += 1;
      }
      const paragraph = document.createElement('p');
      appendInline(paragraph, buffer.join('\n'));
      frag.append(paragraph);
    }
    return frag;
  }

  /* --------------------------------------------------------------- tools */
  function toolIcon(name) {
    switch (name) {
      case 'Bash': return '$';
      case 'Read': return '▤';
      case 'Edit':
      case 'MultiEdit': return '±';
      case 'Write': return '✎';
      case 'Glob':
      case 'Grep': return '⌕';
      case 'WebFetch': return '↗';
      case 'WebSearch': return '⌕';
      case 'Agent':
      case 'Task': return '◈';
      case 'TodoWrite': return '☑';
      case 'ExitPlanMode': return '▶';
      case 'AskUserQuestion': return '?';
      default: return '•';
    }
  }

  function toolSummary(name, input) {
    const data = input || {};
    switch (name) {
      case 'Bash':
        return firstLine(data.description || data.command || '');
      case 'Read':
        return baseName(data.file_path || data.path) + (data.offset || data.limit ? ` · ${data.offset || 1}${data.limit ? `–${Number(data.offset || 1) + Number(data.limit) - 1}` : ''}` : '');
      case 'Edit':
      case 'MultiEdit':
      case 'Write':
        return baseName(data.file_path || data.path);
      case 'Glob':
        return String(data.pattern || '');
      case 'Grep':
        return String(data.pattern || '');
      case 'WebFetch':
        return String(data.url || '');
      case 'WebSearch':
        return String(data.query || '');
      case 'Agent':
      case 'Task':
        return firstLine(data.description || data.prompt || '');
      case 'TodoWrite':
        return `${Array.isArray(data.todos) ? data.todos.length : 0} items`;
      case 'ExitPlanMode':
        return 'plan ready';
      case 'AskUserQuestion':
        return `${Array.isArray(data.questions) ? data.questions.length : 0} question(s)`;
      default:
        return displayName(name);
    }
  }

  function jsonBlock(value) {
    let text;
    try { text = JSON.stringify(value, null, 2); } catch (_) { text = String(value); }
    return h('pre', { class: 'chat-code chat-json', text: text == null ? 'null' : text });
  }

  function pathLine(path, extra) {
    return h('div', { class: 'chat-tool-path' },
      h('code', { text: String(path || '') }),
      extra ? h('span', { class: 'chat-tool-extra', text: extra }) : null);
  }

  function todoList(todos) {
    const list = h('ul', { class: 'chat-todos' });
    for (const todo of Array.isArray(todos) ? todos : []) {
      const status = String((todo && todo.status) || 'pending');
      const mark = status === 'completed' ? '✓' : status === 'in_progress' ? '◐' : '○';
      list.append(h('li', { class: `chat-todo is-${status.replace(/[^a-z_]/g, '') || 'pending'}` },
        h('span', { class: 'chat-todo-mark', text: mark }),
        h('span', { class: 'chat-todo-text', text: String((todo && (todo.content || todo.activeForm)) || '') })));
    }
    return list;
  }

  function questionPreview(questions) {
    const wrap = h('div', { class: 'chat-question-preview' });
    for (const question of Array.isArray(questions) ? questions : []) {
      if (!question || typeof question !== 'object') continue;
      const item = h('div', { class: 'chat-question-preview-item' });
      if (question.header) item.append(h('span', { class: 'badge', text: String(question.header) }));
      item.append(h('div', { class: 'chat-question-text', text: String(question.question || '') }));
      for (const option of Array.isArray(question.options) ? question.options : []) {
        const label = typeof option === 'string' ? option : String((option && option.label) || '');
        const desc = option && typeof option === 'object' ? option.description : '';
        item.append(h('div', { class: 'chat-question-option' },
          h('span', { class: 'chat-question-option-label', text: label }),
          desc ? h('span', { class: 'chat-question-option-desc', text: String(desc) }) : null));
      }
      wrap.append(item);
    }
    if (!wrap.firstChild) wrap.append(h('p', { class: 'muted', text: 'No questions supplied.' }));
    return wrap;
  }

  function toolBody(name, input) {
    const data = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
    const nodes = [];
    switch (name) {
      case 'Bash':
        if (data.description) nodes.push(h('div', { class: 'chat-tool-desc', text: String(data.description) }));
        if (data.command) nodes.push(h('pre', { class: 'chat-code', text: String(data.command) }));
        break;
      case 'Read': {
        let range = '';
        if (data.offset || data.limit) {
          const start = Number(data.offset || 1);
          const end = data.limit ? start + Number(data.limit) - 1 : '';
          range = `${start}${end ? `–${end}` : ''}`;
        }
        nodes.push(pathLine(data.file_path || data.path, range));
        break;
      }
      case 'Edit':
      case 'MultiEdit':
        nodes.push(pathLine(data.file_path || data.path));
        if (typeof data.old_string === 'string' || typeof data.new_string === 'string') {
          nodes.push(diff(data.old_string || '', data.new_string || ''));
        } else {
          nodes.push(jsonBlock(data));
        }
        break;
      case 'Write':
        nodes.push(pathLine(data.file_path || data.path));
        if (typeof data.content === 'string') nodes.push(h('pre', { class: 'chat-code chat-scroll', text: data.content }));
        break;
      case 'Glob':
        nodes.push(h('div', { class: 'chat-tool-line' },
          h('span', { class: 'chat-tool-label', text: 'pattern' }),
          h('code', { text: String(data.pattern || '') })));
        if (data.path) nodes.push(h('div', { class: 'chat-tool-line' },
          h('span', { class: 'chat-tool-label', text: 'in' }),
          h('code', { text: String(data.path) })));
        break;
      case 'Grep':
        nodes.push(h('div', { class: 'chat-tool-line' },
          h('span', { class: 'chat-tool-label', text: 'pattern' }),
          h('code', { text: String(data.pattern || '') })));
        if (data.path || data.glob) nodes.push(h('div', { class: 'chat-tool-line' },
          h('span', { class: 'chat-tool-label', text: 'in' }),
          h('code', { text: String(data.path || data.glob) })));
        break;
      case 'WebFetch': {
        const href = safeHref(data.url);
        if (href) nodes.push(h('div', { class: 'chat-tool-line' }, h('a', { href, target: '_blank', rel: 'noopener noreferrer', text: String(data.url) })));
        else if (data.url) nodes.push(h('div', { class: 'chat-tool-line' }, h('code', { text: String(data.url) })));
        if (data.prompt) nodes.push(h('div', { class: 'chat-tool-desc', text: String(data.prompt) }));
        break;
      }
      case 'WebSearch':
        if (data.query) nodes.push(h('div', { class: 'chat-tool-line' }, h('code', { text: String(data.query) })));
        break;
      case 'Agent':
      case 'Task':
        if (data.description) nodes.push(h('div', { class: 'chat-tool-desc', text: String(data.description) }));
        if (data.prompt) {
          nodes.push(h('details', { class: 'chat-subdetails' },
            h('summary', { text: 'Subagent prompt' }),
            h('pre', { class: 'chat-code chat-scroll', text: String(data.prompt) })));
        }
        break;
      case 'TodoWrite':
        nodes.push(todoList(data.todos));
        break;
      case 'ExitPlanMode': {
        const plan = data.plan || data.plan_text || '';
        if (plan) nodes.push(md(plan));
        else nodes.push(h('p', { class: 'muted', text: 'The plan is shown in the approval card below.' }));
        break;
      }
      case 'AskUserQuestion':
        nodes.push(questionPreview(data.questions));
        break;
      default:
        if (data && Object.keys(data).length) nodes.push(jsonBlock(data));
        else nodes.push(h('div', { class: 'chat-tool-note', text: 'No input.' }));
        break;
    }
    return nodes;
  }

  function preOutput(text, isError) {
    return h('pre', { class: `chat-code chat-tool-output${isError ? ' is-error' : ''}`, text: String(text) });
  }

  function truncationNotice(payload) {
    const bytes = payload && payload.original_bytes ? ` (original ${fmtBytes(payload.original_bytes)})` : '';
    return h('div', { class: 'chat-truncation', text: `Output truncated${bytes}` });
  }

  function toolCard(name, input, id) {
    const data = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
    const statusEl = h('span', { class: 'chat-tool-status is-running', text: 'running' });
    const summaryEl = h('span', { class: 'chat-tool-summary', text: toolSummary(name, data) });
    const nested = h('div', { class: 'chat-nested', hidden: true });
    const resultHost = h('div', { class: 'chat-tool-result', hidden: true });
    const body = h('div', { class: 'chat-tool-body' }, ...toolBody(name, data), nested, resultHost);
    const open = ['Bash', 'Edit', 'MultiEdit', 'Write', 'Agent', 'Task', 'TodoWrite', 'ExitPlanMode', 'AskUserQuestion'].includes(name);
    const el = h('details', {
      class: `chat-tool chat-tool-${slug(name)}`,
      'data-tool-use-id': id || '',
      open: open ? true : undefined,
    },
      h('summary', { class: 'chat-tool-head' },
        h('span', { class: 'chat-tool-icon', text: toolIcon(name) }),
        h('span', { class: 'chat-tool-name', text: displayName(name) }),
        summaryEl,
        statusEl),
      body);

    const card = {
      el,
      nested,
      name,
      setStatus(text, kind) {
        statusEl.textContent = text;
        statusEl.className = `chat-tool-status${kind ? ` is-${kind}` : ''}`;
      },
      setResult(payload) {
        const result = payload && typeof payload === 'object' ? payload : { content: payload };
        const isError = Boolean(result.is_error);
        const nodes = contentNodes(result.content, isError);
        clear(resultHost);
        resultHost.hidden = false;
        if (nodes.length) resultHost.append(...nodes);
        else if (!isError) resultHost.append(h('div', { class: 'chat-tool-note', text: 'Completed' }));
        if (result.truncated) resultHost.append(truncationNotice(result));
        card.setStatus(isError ? 'error' : 'done', isError ? 'error' : 'ok');
      },
    };
    return card;
  }

  function orphanToolResult(payload) {
    const result = payload && typeof payload === 'object' ? payload : { content: payload };
    const isError = Boolean(result.is_error);
    const nodes = contentNodes(result.content, isError);
    if (!nodes.length) nodes.push(preOutput('(no output)', isError));
    const resultHost = h('div', { class: 'chat-tool-result' }, ...nodes);
    const el = h('div', { class: `chat-tool chat-tool-orphan${isError ? ' is-error' : ''}` },
      h('div', { class: 'chat-tool-head' },
        h('span', { class: 'chat-tool-icon', text: '•' }),
        h('span', { class: 'chat-tool-name', text: 'tool result' }),
        h('span', { class: 'chat-tool-status is-error', text: isError ? 'error' : 'done' })),
      h('div', { class: 'chat-tool-body' }, resultHost));
    if (result.truncated) resultHost.append(truncationNotice(result));
    return el;
  }

  /* ---------------------------------------------------------------- diff */
  function lcsOps(before, after) {
    const n = before.length;
    const m = after.length;
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = before[i] === after[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (before[i] === after[j]) {
        ops.push({ type: 'ctx', text: before[i] });
        i += 1;
        j += 1;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        ops.push({ type: 'del', text: before[i] });
        i += 1;
      } else {
        ops.push({ type: 'add', text: after[j] });
        j += 1;
      }
    }
    while (i < n) { ops.push({ type: 'del', text: before[i] }); i += 1; }
    while (j < m) { ops.push({ type: 'add', text: after[j] }); j += 1; }
    return ops;
  }

  function lineSplit(value) {
    const text = String(value == null ? '' : value).replace(/\n$/, '');
    return text === '' ? [] : text.split('\n');
  }

  function diff(oldText, newText) {
    const before = lineSplit(oldText);
    const after = lineSplit(newText);
    let ops;
    if (before.length > MAX_DIFF_LINES || after.length > MAX_DIFF_LINES
      || before.length * after.length > MAX_DIFF_CELLS) {
      ops = before.map((text) => ({ type: 'del', text }))
        .concat(after.map((text) => ({ type: 'add', text })));
    } else {
      ops = lcsOps(before, after);
    }
    const box = h('div', { class: 'chat-diff', role: 'group', 'aria-label': 'Proposed edit' });
    let oldNo = 1;
    let newNo = 1;
    for (const op of ops) {
      box.append(h('div', { class: `chat-diff-line is-${op.type}` },
        h('span', { class: 'chat-diff-no', text: op.type === 'add' ? '' : String(oldNo) }),
        h('span', { class: 'chat-diff-no', text: op.type === 'del' ? '' : String(newNo) }),
        h('span', { class: 'chat-diff-sign', text: op.type === 'add' ? '+' : op.type === 'del' ? '-' : ' ' }),
        h('span', { class: 'chat-diff-text', text: op.text })));
      if (op.type !== 'add') oldNo += 1;
      if (op.type !== 'del') newNo += 1;
    }
    return box;
  }

  /* ---------------------------------------------------------- permissions */
  function questionFormFor(input, submit) {
    const questions = Array.isArray(input.questions) ? input.questions : [];
    const wrap = h('div', { class: 'chat-questions' });
    const entries = [];
    questions.forEach((raw) => {
      const question = raw && typeof raw === 'object' ? raw : { question: String(raw || '') };
      const entry = { question, inputs: [], other: null };
      const card = h('div', { class: 'chat-question' });
      if (question.header) card.append(h('span', { class: 'badge', text: String(question.header) }));
      card.append(h('div', { class: 'chat-question-text', text: String(question.question || '') }));
      const group = `chat-q-${++questionSeq}`;
      const options = Array.isArray(question.options) ? question.options : [];
      options.forEach((option) => {
        const label = typeof option === 'string' ? option : String((option && option.label) || '');
        const desc = option && typeof option === 'object' ? String(option.description || '') : '';
        const inputEl = h('input', { type: question.multiSelect ? 'checkbox' : 'radio', name: group, value: label });
        entry.inputs.push(inputEl);
        card.append(h('label', { class: 'chat-option' },
          inputEl,
          h('span', { class: 'chat-option-label', text: label }),
          desc ? h('span', { class: 'chat-option-desc', text: desc }) : null));
      });
      const other = h('textarea', { class: 'input chat-other', rows: 2, placeholder: 'Other — type your answer' });
      entry.other = other;
      card.append(h('label', { class: 'chat-option chat-other-row' },
        h('span', { class: 'chat-option-label', text: 'Other' }), other));
      entries.push(entry);
      wrap.append(card);
    });
    if (!entries.length) wrap.append(h('p', { class: 'muted', text: 'No questions supplied.' }));

    const submitAnswers = () => {
      const answers = {};
      let response = '';
      entries.forEach((entry) => {
        const key = String(entry.question.question || '');
        const picked = entry.inputs.filter((inputEl) => inputEl.checked).map((inputEl) => inputEl.value);
        const other = entry.other && entry.other.value ? entry.other.value.trim() : '';
        if (entry.question.multiSelect) {
          const values = picked.slice();
          if (other) values.push(other);
          answers[key] = values;
        } else {
          answers[key] = other || picked[0] || '';
        }
        if (other) response = response ? `${response}\n${other}` : other;
      });
      const updated = { questions: input.questions || [], answers };
      if (response) updated.response = response;
      submit({ behavior: 'allow', updated_input: updated });
    };

    wrap.append(h('div', { class: 'chat-question-actions' },
      h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Submit answers', onclick: submitAnswers }),
      h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Skip question', onclick: () => submit({ behavior: 'deny', message: 'User skipped the question' }) })));
    return wrap;
  }

  function permissionCard(permission, options = {}) {
    const perm = permission && typeof permission === 'object' ? permission : {};
    const id = String(perm.id || perm.permission_id || '');
    const name = String(perm.tool_name || 'tool');
    const input = perm.input && typeof perm.input === 'object' ? perm.input : {};
    const suggestions = Array.isArray(perm.suggestions)
      ? perm.suggestions.filter((suggestion) => suggestion && typeof suggestion === 'object')
      : [];
    let expiresAt = perm.expires_at ? Date.parse(perm.expires_at) : NaN;
    let resolved = false;
    let deciding = false;
    let expired = false;

    const countdown = h('span', { class: 'chat-perm-countdown' });
    const badge = h('span', {
      class: 'chat-perm-badge',
      text: name === 'AskUserQuestion' ? 'Question' : name === 'ExitPlanMode' ? 'Plan approval' : 'Approval needed',
    });
    const head = h('div', { class: 'chat-perm-head' },
      badge,
      h('span', { class: 'chat-perm-tool', text: displayName(name) }),
      countdown);
    const preview = name === 'AskUserQuestion'
      ? null
      : h('div', { class: 'chat-perm-preview' }, ...toolBody(name, input));
    const actions = h('div', { class: 'chat-perm-actions' });
    const denyBox = h('div', { class: 'chat-perm-deny', hidden: true });
    const decisionEl = h('div', { class: 'chat-perm-decision', hidden: true });

    const denyInput = h('textarea', { class: 'input chat-perm-deny-input', rows: 2, placeholder: 'Optional message to Claude' });
    const denyConfirm = h('button', {
      class: 'btn btn-danger btn-sm', type: 'button', text: 'Deny',
      onclick: () => submit({ behavior: 'deny', message: denyInput.value.trim() || undefined }),
    });
    const denyCancel = h('button', {
      class: 'btn btn-ghost btn-sm', type: 'button', text: 'Cancel',
      onclick: () => { denyBox.hidden = true; },
    });
    denyBox.append(
      h('div', { class: 'chat-perm-deny-label', text: 'Reason (optional)' }),
      denyInput,
      h('div', { class: 'chat-perm-deny-actions' }, denyConfirm, denyCancel));

    const allowBtn = h('button', {
      class: 'btn btn-primary btn-sm', type: 'button',
      text: name === 'ExitPlanMode' ? 'Approve plan' : 'Allow once',
    });
    const alwaysBtn = suggestions.length
      ? h('button', {
          class: 'btn btn-sm', type: 'button', text: 'Always allow',
          title: 'Add these rules to local settings',
        })
      : null;
    const denyBtn = h('button', {
      class: 'btn btn-danger btn-sm', type: 'button',
      text: name === 'ExitPlanMode' ? 'Keep planning' : name === 'AskUserQuestion' ? 'Skip question' : 'Deny',
    });
    const buttons = [allowBtn, alwaysBtn, denyBtn].filter(Boolean);

    const questionForm = name === 'AskUserQuestion' ? questionFormFor(input, submit) : null;

    function setButtonsEnabled(enabled) {
      for (const button of buttons) button.disabled = !enabled;
    }

    function submit(decision) {
      if (resolved || deciding || expired) return;
      deciding = true;
      setButtonsEnabled(false);
      if (el) el.classList.add('is-deciding');
      let result;
      try { result = options.decide ? options.decide(decision) : null; } catch (_) { result = null; }
      Promise.resolve(result).catch(() => reset());
    }

    function reset() {
      deciding = false;
      setButtonsEnabled(!expired);
      if (el) el.classList.remove('is-deciding');
    }

    allowBtn.addEventListener('click', () => submit({ behavior: 'allow' }));
    if (alwaysBtn) {
      alwaysBtn.addEventListener('click', () => {
        const updated_permissions = suggestions.map((suggestion) => ({ ...suggestion, destination: 'localSettings' }));
        submit({ behavior: 'allow', updated_permissions });
      });
    }
    denyBtn.addEventListener('click', () => {
      if (name === 'ExitPlanMode') submit({ behavior: 'deny', message: 'Keep planning' });
      else if (name === 'AskUserQuestion') submit({ behavior: 'deny', message: 'User skipped the question' });
      else denyBox.hidden = false;
    });

    actions.append(...(questionForm ? [questionForm] : buttons));
    const el = h('section', {
      class: `chat-permission${name === 'AskUserQuestion' ? ' is-question' : ''}`,
      'data-permission-id': id,
    },
      head,
      preview,
      actions,
      questionForm ? null : denyBox,
      decisionEl);

    function tick() {
      if (resolved || !Number.isFinite(expiresAt)) return;
      const seconds = Math.floor((expiresAt - Date.now()) / 1000);
      if (seconds <= 0) {
        if (expired) return;
        expired = true;
        countdown.textContent = 'expired';
        countdown.classList.add('is-urgent');
        el.classList.add('is-expired');
        setButtonsEnabled(false);
        decisionEl.hidden = false;
        clear(decisionEl);
        decisionEl.append(h('strong', { text: 'Expired' }), h('span', { text: ' — no decision was sent.' }));
        return;
      }
      countdown.textContent = seconds >= 60
        ? `expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
        : `expires in ${seconds}s`;
      countdown.classList.toggle('is-urgent', seconds <= 30);
    }

    function resolve(status, decision) {
      resolved = true;
      deciding = false;
      expired = false;
      actions.hidden = true;
      denyBox.hidden = true;
      el.classList.remove('is-deciding', 'is-expired');
      el.classList.add('is-resolved');
      if (status) el.classList.add(`is-${String(status).replace(/[^a-z]/g, '')}`);
      countdown.textContent = '';
      countdown.classList.remove('is-urgent');
      badge.textContent = status === 'allowed' ? 'Allowed'
        : status === 'denied' ? 'Denied'
          : status === 'expired' ? 'Expired' : 'Resolved';
      decisionEl.hidden = false;
      clear(decisionEl);
      const title = status === 'allowed' ? 'Allowed'
        : status === 'denied' ? 'Denied'
          : status === 'expired' ? 'Expired' : String(status || 'Resolved');
      decisionEl.append(h('strong', { text: title }));
      let message = '';
      if (decision && typeof decision === 'object') {
        message = decision.message || (decision.updatedInput && decision.updatedInput.answers ? 'Answers submitted' : '');
      }
      if (message) decisionEl.append(h('span', { class: 'chat-perm-decision-msg', text: ` · ${String(message)}` }));
    }

    function setExpiry(value) {
      const ts = Date.parse(value);
      if (Number.isFinite(ts)) {
        expiresAt = ts;
        tick();
      }
    }

    tick();
    return { el, id, tick, resolve, reset, setExpiry, dispose() { /* no per-card timer */ } };
  }

  /* ------------------------------------------------------------ message UI */
  function userBubble(content) {
    const bubble = h('div', { class: 'chat-bubble' });
    if (Array.isArray(content)) {
      for (const part of content) {
        if (part == null) continue;
        if (typeof part === 'string') {
          if (part) bubble.append(h('span', { class: 'chat-text-part', text: part }));
        } else if (typeof part === 'object' && part.type === 'image') {
          bubble.append(imageBlock(part));
        } else {
          const text = contentText(part);
          if (text) bubble.append(h('span', { class: 'chat-text-part', text }));
        }
      }
    } else {
      const text = contentText(content);
      if (text) bubble.append(h('span', { class: 'chat-text-part', text }));
    }
    const actions = h('div', { class: 'chat-user-actions', hidden: true });
    const shell = h('div', { class: 'chat-user-shell' }, bubble, actions);
    const el = h('article', { class: 'chat-msg chat-user' }, shell);
    el.bubble = bubble;
    el.actions = actions;
    return el;
  }

  function assistantGroup() {
    const body = h('div', { class: 'chat-assistant-body' });
    const el = h('article', { class: 'chat-msg chat-assistant' },
      h('div', { class: 'chat-avatar', 'aria-hidden': 'true', text: 'AI' }),
      body);
    return { el, body };
  }

  function textBlock(text) {
    return h('div', { class: 'chat-block chat-text' }, md(text));
  }

  function streamText() {
    const textEl = h('span', { class: 'chat-streaming-text' });
    const cursor = h('span', { class: 'chat-cursor', 'aria-hidden': 'true' });
    const el = h('div', { class: 'chat-block chat-text is-streaming' }, textEl, cursor);
    return {
      el,
      append(chunk) { textEl.append(document.createTextNode(String(chunk))); },
      removeCursor() { cursor.remove(); el.classList.remove('is-streaming'); },
      dispose() { el.remove(); },
    };
  }

  function thinkingBlock(text) {
    return h('details', { class: 'chat-thinking' },
      h('summary', { text: 'Thinking' }),
      h('div', { class: 'chat-thinking-text', text: String(text || '') }));
  }

  function streamThinking() {
    const textEl = h('span', { class: 'chat-streaming-text' });
    const cursor = h('span', { class: 'chat-cursor', 'aria-hidden': 'true' });
    const el = h('details', { class: 'chat-thinking is-streaming', open: true },
      h('summary', { text: 'Thinking…' }),
      h('div', { class: 'chat-thinking-text' }, textEl, cursor));
    return {
      el,
      append(chunk) { textEl.append(document.createTextNode(String(chunk))); },
      removeCursor() { cursor.remove(); el.classList.remove('is-streaming'); },
      dispose() { el.remove(); },
    };
  }

  /* ---------------------------------------------------------------- rows */
  function resultRow(payload) {
    const data = payload && typeof payload === 'object' ? payload : {};
    const isError = Boolean(data.is_error);
    const body = h('div', { class: 'chat-row-body' });
    const subtype = data.subtype ? String(data.subtype) : '';
    body.append(h('div', {
      class: 'chat-row-title',
      text: isError ? 'Turn failed' : subtype && subtype !== 'success' ? subtype : 'Turn complete',
    }));
    const text = typeof data.result === 'string' ? data.result : (data.result != null ? contentText(data.result) : '');
    if (text) body.append(md(text));
    const meta = [];
    const cost = Number(data.total_cost_usd);
    if (cost) meta.push(fmtCost(cost));
    const usage = data.usage;
    if (usage && typeof usage === 'object') {
      const input = Number(usage.input_tokens || 0);
      const output = Number(usage.output_tokens || 0);
      if (input || output) meta.push(`${fmtTokens(input)} in · ${fmtTokens(output)} out`);
    }
    if (data.terminal_reason) meta.push(String(data.terminal_reason));
    if (Array.isArray(data.permission_denials) && data.permission_denials.length) {
      meta.push(`${data.permission_denials.length} permission denial(s)`);
    }
    if (meta.length) body.append(h('div', { class: 'chat-row-meta', text: meta.join(' · ') }));
    return h('div', { class: `chat-row chat-result${isError ? ' is-error' : ''}` },
      h('span', { class: 'chat-row-icon', text: isError ? '!' : '✓' }),
      body);
  }

  function errorRow(payload, options = {}) {
    const data = payload && typeof payload === 'object' ? payload : { message: payload };
    const body = h('div', { class: 'chat-row-body' },
      h('div', { class: 'chat-row-title', text: options.title || 'Chat error' }),
      h('div', { class: 'chat-row-text', text: String(data.message || 'Unknown error') }));
    if (options.retry) {
      body.append(h('button', { class: 'btn btn-sm', type: 'button', text: 'Retry', onclick: options.retry }));
    }
    return h('div', { class: 'chat-row chat-error' },
      h('span', { class: 'chat-row-icon', text: '!' }), body);
  }

  function statusRow(text, options = {}) {
    return h('div', { class: `chat-row chat-status-row${options.kind ? ` is-${options.kind}` : ''}` },
      h('span', { class: 'chat-status-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'chat-row-text', text: String(text || '') }));
  }

  function emptyState(text) {
    return h('div', { class: 'chat-empty', text: String(text || '') });
  }

  window.ChatRender = {
    init,
    md,
    contentText,
    textFromContent: contentText,
    eventExcerpt,
    imageBlock,
    userBubble,
    assistantGroup,
    textBlock,
    streamText,
    thinkingBlock,
    streamThinking,
    toolCard,
    orphanToolResult,
    permissionCard,
    resultRow,
    errorRow,
    statusRow,
    emptyState,
    diff,
    fmtTokens,
    fmtCost,
    fmtBytes,
  };
})();
