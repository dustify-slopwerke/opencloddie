/* Statistics view: instance health, gateway usage, and session activity.
 *
 * Loaded as a classic script.  DOM helpers (h, clear, api, formatUptime) come
 * from app.js and are only called after the page has booted.
 */
(() => {
  'use strict';

  const REFRESH_MS = 5000;
  const RANGES = [3600, 21600, 86400, 604800];
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const state = {
    window: 3600,
    data: null,
    prev: null,
    timer: null,
    loading: false,
    paused: false,
    visible: false,
    bound: false,
    error: '',
  };

  /* -------------------------------------------------------------- format */
  function fmtNum(value) {
    const n = Number(value || 0);
    if (n >= 1e9) return `${trimZero(n / 1e9)}B`;
    if (n >= 1e6) return `${trimZero(n / 1e6)}M`;
    if (n >= 1e4) return `${trimZero(n / 1e3)}K`;
    return n.toLocaleString();
  }

  function trimZero(value) {
    return value.toFixed(1).replace(/\.0$/, '');
  }

  function fmtBytes(value) {
    if (value === null || value === undefined) return 'not available';
    let n = Number(value);
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let unit = 0;
    while (n >= 1024 && unit < units.length - 1) {
      n /= 1024;
      unit += 1;
    }
    return `${unit === 0 ? Math.round(n) : n.toFixed(1)} ${units[unit]}`;
  }

  function fmtPct(part, total) {
    const n = Number(total || 0);
    if (!n) return '0%';
    const pct = (Number(part || 0) / n) * 100;
    return pct >= 10 ? `${pct.toFixed(0)}%` : `${pct.toFixed(1)}%`;
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const n = Math.max(0, Number(seconds));
    if (n < 60) return `${Math.floor(n)}s`;
    if (n < 3600) return `${Math.floor(n / 60)}m`;
    if (n < 86400) return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
    return `${Math.floor(n / 86400)}d ${Math.floor((n % 86400) / 3600)}h`;
  }

  function fmtTime(epoch, windowSeconds) {
    const date = new Date(Number(epoch) * 1000);
    if (!Number.isFinite(date.getTime())) return '—';
    if (windowSeconds <= 86400) {
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${date.toLocaleTimeString([], { hour: '2-digit' })}`;
  }

  function niceMax(value) {
    if (value <= 5) return Math.max(1, value);
    const exponent = Math.floor(Math.log10(value));
    const base = Math.pow(10, exponent);
    const fraction = value / base;
    const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    return nice * base;
  }

  function svgEl(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      node.setAttribute(key, String(value));
    }
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /* ---------------------------------------------------------------- load */
  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    const content = document.querySelector('.stats-content');
    if (content) content.classList.add('is-refreshing');
    try {
      const data = await api(`/api/metrics?window=${state.window}`);
      state.data = data;
      state.error = '';
      render();
    } catch (error) {
      state.error = error && error.message ? error.message : 'Could not load statistics.';
      renderError();
    } finally {
      state.loading = false;
      if (content) content.classList.remove('is-refreshing');
    }
  }

  function setRange(value) {
    if (!RANGES.includes(Number(value))) return;
    state.window = Number(value);
    document.querySelectorAll('#stats-range [data-window]').forEach((button) => {
      const active = Number(button.dataset.window) === state.window;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    refresh();
  }

  function togglePause() {
    state.paused = !state.paused;
    const button = document.getElementById('stats-pause');
    if (button) button.textContent = state.paused ? 'Resume' : 'Pause';
    if (!state.paused) refresh();
    else updateUpdated();
  }

  /* -------------------------------------------------------------- render */
  function render() {
    const data = state.data;
    if (!data) return;
    const rates = updateRates(data);
    renderScope(data);
    renderTiles(data, rates);
    renderCharts(data);
    renderGateway(data);
    renderSessions(data);
    renderProcess(data);
    renderError();
    updateUpdated();
    const subtitle = document.getElementById('view-subtitle');
    if (subtitle && document.getElementById('view-stats') && !document.getElementById('view-stats').hidden) {
      subtitle.textContent = StatsView.subtitle();
    }
  }

  function updateRates(data) {
    const now = Date.parse(data.generated_at) / 1000;
    const current = {
      t: now,
      requests: data.live.gateway.requests,
      bytes: data.live.gateway.bytes_out,
      tokens: data.live.gateway.tokens.output,
      uptime: data.live.process.uptime_seconds,
    };
    const previous = state.prev;
    const rates = { requests: null, bytes: null, tokens: null };
    if (previous && Number.isFinite(previous.t) && current.t > previous.t && current.uptime >= previous.uptime) {
      const elapsed = current.t - previous.t;
      rates.requests = Math.max(0, (current.requests - previous.requests) / elapsed);
      rates.bytes = Math.max(0, (current.bytes - previous.bytes) / elapsed);
      rates.tokens = Math.max(0, (current.tokens - previous.tokens) / elapsed);
    }
    state.prev = current;
    return rates;
  }

  function renderScope(data) {
    const badge = document.getElementById('stats-scope');
    if (!badge) return;
    badge.textContent = data.scope === 'admin' ? 'all users' : 'my usage';
    badge.classList.toggle('ok', data.scope === 'admin');
    badge.title = data.scope === 'admin'
      ? 'Visible because you are in an admin group'
      : 'Only your keys and sessions are shown';
  }

  function updateUpdated() {
    const node = document.getElementById('stats-updated');
    if (!node) return;
    if (state.paused) {
      node.textContent = 'paused';
      return;
    }
    if (state.error) {
      node.textContent = 'stale · retrying';
      return;
    }
    node.textContent = state.data
      ? `updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
      : '';
  }

  function renderError() {
    const node = document.getElementById('stats-error');
    if (!node) return;
    clear(node);
    if (!state.error) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    node.append(
      h('strong', { text: 'Statistics unavailable. ' }),
      document.createTextNode(state.error),
      h('button', {
        class: 'btn btn-sm',
        style: 'margin-left:10px',
        text: 'Retry',
        onclick: () => refresh(),
      }),
    );
  }

  /* --------------------------------------------------------------- tiles */
  function renderTiles(data, rates) {
    const host = document.getElementById('stats-tiles');
    if (!host) return;
    clear(host);
    const gateway = data.live.gateway;
    const instance = data.live.instance;
    const process = data.live.process;
    const memory = process.memory || {};
    const errors = gateway.client_errors + gateway.server_errors + gateway.rate_limited;
    const tiles = [
      ['Uptime', formatUptime(process.uptime_seconds), `pid ${process.pid}`],
      ['Gateway requests', fmtNum(gateway.requests), rates.requests === null ? 'since server start' : `${rates.requests.toFixed(1)}/s now`],
      ['Error rate', fmtPct(errors, gateway.requests), `${fmtNum(errors)} failed`],
      ['Bytes out', fmtBytes(gateway.bytes_out), rates.bytes === null ? `${fmtBytes(gateway.bytes_in)} in` : `${fmtBytes(rates.bytes)}/s now`],
      ['Tokens out', fmtNum(gateway.tokens.output), `${fmtNum(gateway.tokens.input)} in · ${fmtNum(gateway.tokens.cache_read)} cache read`],
      ['Sessions online', `${fmtNum(instance.sessions_online)}/${fmtNum(instance.sessions_total)}`, `${fmtNum(instance.agents)} agents · ${fmtNum(instance.viewers)} viewers`],
      ['Viewers', fmtNum(instance.viewers), `${fmtNum(data.live.hub.channels_opened_total)} channels opened`],
      ['Memory (RSS)', fmtBytes(memory.rss_bytes), `${fmtNum(process.threads)} threads`],
    ];
    for (const [label, value, sub] of tiles) {
      host.append(
        h('div', { class: 'stat-tile' },
          h('div', { class: 'stat-label', text: label }),
          h('div', { class: 'stat-value', text: value }),
          h('div', { class: 'stat-sub', text: sub })),
      );
    }
  }

  /* -------------------------------------------------------------- charts */
  function renderCharts(data) {
    const gatewayPoints = (data.history.gateway || []).map((point) => ({
      t: point.t,
      value: Number(point.requests || 0),
    }));
    const sessionPoints = (data.history.instance || []).map((point) => ({
      t: point.t,
      value: Number(point.sessions_online || 0),
    }));
    const now = Math.floor(Date.parse(data.generated_at) / 1000);
    const start = Math.floor((now - data.window_seconds) / data.step_seconds) * data.step_seconds;

    renderChart(
      document.getElementById('chart-gateway'),
      document.getElementById('chart-gateway-table'),
      gatewayPoints,
      {
        start,
        end: now,
        step: data.step_seconds,
        window: data.window_seconds,
        title: 'Requests',
        hue: 'var(--blue)',
        format: fmtNum,
        empty: 'No gateway requests in this range yet.',
      },
    );
    renderChart(
      document.getElementById('chart-sessions'),
      document.getElementById('chart-sessions-table'),
      sessionPoints,
      {
        start,
        end: now,
        step: data.step_seconds,
        window: data.window_seconds,
        title: 'Sessions online',
        hue: 'var(--accent)',
        format: fmtNum,
        empty: 'No session samples in this range yet.',
      },
    );

    const gatewaySub = document.getElementById('chart-gateway-sub');
    const sessionSub = document.getElementById('chart-sessions-sub');
    const bucket = data.step_seconds >= 3600
      ? `${data.step_seconds / 3600}h buckets`
      : `${Math.round(data.step_seconds / 60)}m buckets`;
    if (gatewaySub) gatewaySub.textContent = `Persisted history · ${bucket}`;
    if (sessionSub) sessionSub.textContent = `Sampled from the instance · ${bucket}`;
  }

  function fillSeries(points, start, end, step) {
    const byTime = new Map(points.map((point) => [Number(point.t), point.value]));
    const series = [];
    for (let t = start; t <= end; t += step) {
      series.push({ t, value: byTime.get(t) || 0 });
    }
    if (series.length === 0) series.push({ t: end, value: 0 });
    return series;
  }

  function renderChart(host, tableHost, points, options) {
    if (!host) return;
    clear(host);
    host.onpointermove = null;
    host.onpointerleave = null;
    host.onfocus = null;
    host.onblur = null;
    host.onkeydown = null;
    const series = fillSeries(points, options.start, options.end, options.step);
    renderSeriesTable(tableHost, series, options);
    if (!series.some((point) => point.value > 0)) {
      host.append(h('div', { class: 'chart-empty', text: options.empty }));
      return;
    }

    host.style.setProperty('--chart-hue', options.hue);
    const W = 640;
    const H = 190;
    const left = 46;
    const right = 14;
    const top = 14;
    const plotWidth = W - left - right;
    const plotHeight = H - top - 26;
    const max = niceMax(Math.max(...series.map((point) => point.value)));
    const x = (index) => left + (series.length === 1
      ? plotWidth / 2
      : (index / (series.length - 1)) * plotWidth);
    const y = (value) => top + plotHeight - (value / max) * plotHeight;

    const svg = svgEl('svg', {
      viewBox: `0 0 ${W} ${H}`,
      class: 'chart-svg',
      'aria-hidden': 'true',
      focusable: 'false',
    });

    for (const tick of [0, max / 2, max]) {
      svg.append(svgEl('line', {
        x1: left,
        x2: W - right,
        y1: y(tick),
        y2: y(tick),
        class: tick === 0 ? 'chart-baseline' : 'chart-grid',
      }));
      // Label only the baseline and the top; the midline is an unlabeled
      // gridline, so counts are never presented as fractional axis ticks.
      if (tick === 0 || tick === max) {
        svg.append(svgEl('text', {
          x: left - 8,
          y: y(tick) + 3.5,
          'text-anchor': 'end',
          class: 'chart-axis',
        }, fmtNum(tick)));
      }
    }
    const firstLabel = svgEl('text', { x: left, y: H - 8, class: 'chart-axis' }, fmtTime(series[0].t, options.window));
    const lastLabel = svgEl('text', {
      x: W - right,
      y: H - 8,
      'text-anchor': 'end',
      class: 'chart-axis',
    }, fmtTime(series[series.length - 1].t, options.window));
    svg.append(firstLabel, lastLabel);

    let area = `M ${x(0)} ${y(series[0].value)}`;
    let line = `M ${x(0)} ${y(series[0].value)}`;
    for (let index = 1; index < series.length; index += 1) {
      const command = ` L ${x(index)} ${y(series[index].value)}`;
      area += command;
      line += command;
    }
    area += ` L ${x(series.length - 1)} ${top + plotHeight} L ${x(0)} ${top + plotHeight} Z`;
    svg.append(svgEl('path', { d: area, class: 'chart-area' }));
    svg.append(svgEl('path', { d: line, class: 'chart-line' }));

    const lastIndex = series.length - 1;
    svg.append(svgEl('circle', {
      cx: x(lastIndex),
      cy: y(series[lastIndex].value),
      r: 4,
      class: 'chart-dot',
    }));
    svg.append(svgEl('text', {
      x: x(lastIndex) - 8,
      y: Math.max(top + 10, y(series[lastIndex].value) - 10),
      'text-anchor': 'end',
      class: 'chart-endlabel',
    }, options.format(series[lastIndex].value)));

    const crosshair = svgEl('line', {
      x1: 0,
      x2: 0,
      y1: top,
      y2: top + plotHeight,
      class: 'chart-cross',
    });
    crosshair.style.display = 'none';
    const cursor = svgEl('circle', { r: 4.5, class: 'chart-hover-dot' });
    cursor.style.display = 'none';
    svg.append(crosshair, cursor);
    svg.append(svgEl('rect', {
      x: left,
      y: top,
      width: plotWidth,
      height: plotHeight,
      class: 'chart-hit',
    }));
    host.append(svg);

    const tip = h('div', { class: 'chart-tip', hidden: true });
    host.append(tip);
    let activeIndex = lastIndex;

    function showAt(index) {
      activeIndex = Math.max(0, Math.min(series.length - 1, index));
      const px = x(activeIndex);
      const py = y(series[activeIndex].value);
      crosshair.setAttribute('x1', px);
      crosshair.setAttribute('x2', px);
      crosshair.style.display = '';
      cursor.setAttribute('cx', px);
      cursor.setAttribute('cy', py);
      cursor.style.display = '';
      clear(tip);
      tip.append(
        h('div', { class: 'chart-tip-time', text: fmtTime(series[activeIndex].t, options.window) }),
        h('div', { class: 'chart-tip-row' },
          h('span', { class: 'chart-tip-key' }),
          h('span', { class: 'chart-tip-value', text: options.format(series[activeIndex].value) }),
          h('span', { class: 'chart-tip-label', text: options.title })),
      );
      tip.hidden = false;
      const rect = host.getBoundingClientRect();
      const fraction = px / W;
      tip.style.left = `${Math.max(60, Math.min(rect.width - 150, fraction * rect.width + 10))}px`;
    }

    function hideCursor() {
      crosshair.style.display = 'none';
      cursor.style.display = 'none';
      tip.hidden = true;
    }

    host.onpointermove = (event) => {
      const rect = host.getBoundingClientRect();
      if (!rect.width) return;
      const px = ((event.clientX - rect.left) / rect.width) * W;
      const fraction = Math.max(0, Math.min(1, (px - left) / plotWidth));
      showAt(Math.round(fraction * (series.length - 1)));
    };
    host.onpointerleave = hideCursor;
    host.onfocus = () => showAt(activeIndex);
    host.onblur = hideCursor;
    host.onkeydown = (event) => {
      if (event.key === 'ArrowLeft') {
        showAt(activeIndex - 1);
        event.preventDefault();
      } else if (event.key === 'ArrowRight') {
        showAt(activeIndex + 1);
        event.preventDefault();
      } else if (event.key === 'Home') {
        showAt(0);
        event.preventDefault();
      } else if (event.key === 'End') {
        showAt(lastIndex);
        event.preventDefault();
      }
    };
    host.setAttribute(
      'aria-label',
      `${options.title} over the last ${rangeLabel(options.window)}. Use arrow keys to inspect values.`,
    );
  }

  function renderSeriesTable(host, series, options) {
    if (!host) return;
    clear(host);
    const rows = series.slice().reverse().map((point) => h('tr', {},
      h('td', { text: fmtTime(point.t, options.window) }),
      h('td', { class: 'num', text: options.format(point.value) }),
    ));
    host.append(
      h('div', { class: 'table-scroll' },
        h('table', { class: 'stats-table' },
          h('thead', {}, h('tr', {},
            h('th', { text: 'Time' }),
            h('th', { class: 'num', text: options.title }),
          )),
          h('tbody', {}, ...rows))),
    );
  }

  function rangeLabel(seconds) {
    if (seconds >= 604800) return '7 days';
    if (seconds >= 86400) return '24 hours';
    if (seconds >= 21600) return '6 hours';
    return 'hour';
  }

  /* ------------------------------------------------------------- gateway */
  function renderGateway(data) {
    const host = document.getElementById('stats-gateway');
    if (!host) return;
    clear(host);
    const gateway = data.live.gateway;
    const errors = gateway.client_errors + gateway.server_errors + gateway.rate_limited;
    const limit = Number(gateway.rate_limit.limit_per_minute || 0);

    host.append(factsGrid([
      ['Requests', `${fmtNum(gateway.requests)} (${fmtNum(gateway.ok)} ok)`],
      ['In flight', fmtNum(gateway.in_flight)],
      ['Errors', `${fmtNum(errors)} · ${fmtNum(gateway.client_errors)} 4xx · ${fmtNum(gateway.server_errors)} 5xx · ${fmtNum(gateway.rate_limited)} limited`],
      ['Bytes', `${fmtBytes(gateway.bytes_in)} in · ${fmtBytes(gateway.bytes_out)} out`],
      ['Tokens', `${fmtNum(gateway.tokens.input)} in · ${fmtNum(gateway.tokens.output)} out`],
      ['Cache tokens', `${fmtNum(gateway.tokens.cache_read)} read · ${fmtNum(gateway.tokens.cache_creation)} write`],
      ['Upstream response', `p50 ${gateway.upstream_ms.p50} ms · p95 ${gateway.upstream_ms.p95} ms · max ${gateway.upstream_ms.max} ms`],
      ['Rate limit', limit > 0 ? `${limit} requests/minute per key` : 'disabled'],
    ]));

    const statuses = Object.entries(gateway.by_status || {});
    if (statuses.length) {
      host.append(h('h3', { class: 'stats-subhead', text: 'Status codes' }));
      host.append(dataTable(
        ['Status', 'Requests'],
        statuses.map(([status, count]) => [
          h('span', { class: `badge ${Number(status) < 400 ? 'ok' : 'bad'}`, text: status }),
          fmtNum(count),
        ]),
        [1],
      ));
    }

    const models = gateway.by_model || [];
    if (models.length) {
      host.append(h('h3', { class: 'stats-subhead', text: 'By model' }));
      host.append(dataTable(
        ['Model', 'Requests', 'Errors', 'Bytes in/out', 'Tokens in/out'],
        models.map((model) => [
          h('code', { text: model.model || 'unknown' }),
          fmtNum(model.requests),
          fmtNum(model.errors),
          `${fmtBytes(model.bytes_in)} / ${fmtBytes(model.bytes_out)}`,
          `${fmtNum(model.tokens_in)} / ${fmtNum(model.tokens_out)}`,
        ]),
        [1, 2, 3, 4],
      ));
    }

    const keys = gateway.by_key || [];
    host.append(h('h3', { class: 'stats-subhead', text: 'By API key' }));
    if (!keys.length) {
      host.append(h('p', { class: 'muted stats-empty', text: 'No API keys yet.' }));
    } else {
      const admin = data.scope === 'admin';
      const headers = ['Key'];
      if (admin) headers.push('User');
      headers.push('Model', 'Requests', 'Errors', 'Tokens in/out', 'Rate limit', 'Last used');
      const numeric = headers.map((header, index) => [ 'Requests', 'Errors', 'Tokens in/out', 'Rate limit' ].includes(header) ? index : -1).filter((index) => index >= 0);
      host.append(dataTable(
        headers,
        keys.map((key) => {
          const cells = [
            h('div', {},
              h('div', { class: 'cell-strong', text: key.name || key.id }),
              h('div', { class: 'cell-sub', text: `${key.prefix || key.id}${key.revoked ? ' · revoked' : ''}` })),
          ];
          if (admin) cells.push(key.user || '—');
          cells.push(
            h('code', { text: key.model || '—' }),
            fmtNum(key.requests),
            fmtNum(key.errors),
            `${fmtNum(key.tokens_in)} / ${fmtNum(key.tokens_out)}`,
            meter(key.rate),
            key.last_used_at ? fmtAge(key.last_used_at) : 'never',
          );
          return cells;
        }),
        numeric,
      ));
    }

    if (gateway.requests === 0) {
      host.append(h('p', { class: 'muted stats-empty', text: 'No gateway requests since the server started.' }));
    }
  }

  function meter(rate) {
    const limit = Number((rate || {}).limit || 0);
    const used = Number((rate || {}).used || 0);
    if (limit <= 0) return h('span', { class: 'muted', text: 'disabled' });
    const pct = Math.min(100, (used / limit) * 100);
    const severity = pct >= 100 ? 'danger' : pct >= 70 ? 'warn' : '';
    return h('div', { class: 'meter-wrap' },
      h('div', {
        class: `meter${severity ? ` is-${severity}` : ''}`,
        role: 'progressbar',
        'aria-valuenow': used,
        'aria-valuemin': 0,
        'aria-valuemax': limit,
        'aria-label': `${used} of ${limit} requests used`,
      }, h('div', { class: `meter-fill ${severity}`, style: `width:${pct}%` })),
      h('span', { class: 'meter-text', text: `${used}/${limit}` }));
  }

  /* ------------------------------------------------------------ sessions */
  function renderSessions(data) {
    const host = document.getElementById('stats-sessions');
    if (!host) return;
    clear(host);
    const sessions = data.live.sessions || [];
    if (!sessions.length) {
      host.append(h('p', { class: 'muted stats-empty', text: 'No sessions registered. Run opencloddie on a machine with Claude Code.' }));
      return;
    }
    const admin = data.scope === 'admin';
    const headers = ['Session'];
    if (admin) headers.push('User');
    headers.push('Host / cwd', 'Model', 'Status', 'Viewers', 'Connected', 'Last seen', 'Terminal bytes');
    const numeric = headers.map((header, index) => ['Viewers', 'Connected', 'Terminal bytes'].includes(header) ? index : -1).filter((index) => index >= 0);
    const rows = sessions.map((session) => {
      const cells = [
        h('button', {
          class: 'link-btn',
          text: session.name || session.id,
          onclick: () => {
            setView('sessions');
            if (typeof activate === 'function' && session.online) activate(session.id);
          },
        }),
      ];
      if (admin) cells.push(session.user || '—');
      cells.push(
        h('div', {},
          h('div', { class: 'cell-strong', text: session.hostname || '—' }),
          h('div', { class: 'cell-sub', text: session.cwd || '' })),
        h('code', { text: session.model || '—' }),
        h('span', { class: `badge ${session.online ? 'ok' : 'bad'}`, text: session.online ? 'online' : 'offline' }),
        fmtNum(session.viewers),
        fmtDuration(session.connected_seconds),
        fmtAge(session.last_seen_at),
        `${fmtBytes(session.terminal_bytes_out)} out · ${fmtBytes(session.terminal_bytes_in)} in`,
      );
      return cells;
    });
    host.append(dataTable(headers, rows, numeric));
  }

  /* ------------------------------------------------------------- process */
  function renderProcess(data) {
    const host = document.getElementById('stats-process');
    if (!host) return;
    clear(host);
    const process = data.live.process;
    const memory = process.memory || {};
    const cpu = process.cpu || {};
    const load = process.load || {};
    const disk = process.disk || {};
    const database = process.database || {};
    const hub = data.live.hub || {};
    host.append(factsGrid([
      ['Version', process.version || '—'],
      ['Uptime', formatUptime(process.uptime_seconds)],
      ['PID', fmtNum(process.pid)],
      ['Python', `${process.python} · ${process.platform}`],
      ['Threads', fmtNum(process.threads)],
      ['Open files', process.open_fds === null ? 'not available' : fmtNum(process.open_fds)],
      ['RSS / peak', `${fmtBytes(memory.rss_bytes)} / ${fmtBytes(memory.peak_rss_bytes)}`],
      ['Virtual memory', fmtBytes(memory.vms_bytes)],
      ['CPU time', `${Number(cpu.user_seconds || 0).toFixed(2)}s user · ${Number(cpu.system_seconds || 0).toFixed(2)}s system`],
      ['Load average', [load['1m'], load['5m'], load['15m']].map((value) => value === null || value === undefined ? '—' : value).join(' / ')],
      ['Database', fmtBytes(database.size_bytes)],
      ['Disk free', disk.free_bytes === null || disk.free_bytes === undefined ? 'not available' : `${fmtBytes(disk.free_bytes)} of ${fmtBytes(disk.total_bytes)}`],
      ['Agent connections', `${fmtNum(hub.agents_connected_total)} total · ${fmtNum(hub.agents_disconnected_total)} closed`],
      ['Terminal relay', `${fmtBytes(hub.terminal_bytes_out_total)} out · ${fmtBytes(hub.terminal_bytes_in_total)} in`],
    ]));
  }

  /* -------------------------------------------------------------- tables */
  function factsGrid(rows) {
    const list = h('dl', { class: 'facts stats-facts' });
    for (const [term, value] of rows) {
      list.append(h('dt', { text: term }), h('dd', { text: value }));
    }
    return list;
  }

  function dataTable(headers, rows, numeric = []) {
    const numericSet = new Set(numeric);
    const head = h('tr');
    headers.forEach((header, index) => {
      head.append(h('th', { class: numericSet.has(index) ? 'num' : '', text: header }));
    });
    const body = h('tbody');
    rows.forEach((cells) => {
      const tr = h('tr');
      cells.forEach((cell, index) => {
        tr.append(h('td', {
          class: numericSet.has(index) ? 'num' : '',
        }, cell instanceof Node ? cell : String(cell)));
      });
      body.append(tr);
    });
    return h('div', { class: 'table-scroll' },
      h('table', { class: 'stats-table' }, h('thead', {}, head), body));
  }

  /* --------------------------------------------------------------- shell */
  function bindOnce() {
    if (state.bound) return;
    state.bound = true;
    document.querySelectorAll('#stats-range [data-window]').forEach((button) => {
      button.addEventListener('click', () => setRange(button.dataset.window));
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.visible) refresh();
    });
  }

  const StatsView = {
    show() {
      bindOnce();
      state.visible = true;
      document.querySelectorAll('#stats-range [data-window]').forEach((button) => {
        const active = Number(button.dataset.window) === state.window;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      refresh();
      if (!state.timer) {
        state.timer = setInterval(() => {
          if (!document.hidden && !state.paused && state.visible) refresh();
        }, REFRESH_MS);
      }
    },
    hide() {
      state.visible = false;
      if (state.timer) {
        clearInterval(state.timer);
        state.timer = null;
      }
    },
    refresh,
    togglePause,
    isPaused() {
      return state.paused;
    },
    subtitle() {
      if (!state.data) return 'instance and usage';
      return `${fmtNum(state.data.live.gateway.requests)} requests · ${fmtNum(state.data.live.instance.agents)} agents`;
    },
  };

  window.StatsView = StatsView;
})();
