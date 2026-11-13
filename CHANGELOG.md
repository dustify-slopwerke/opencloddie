# Changelog

All notable changes to opencloddie are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-13

### Added

- Session-to-session mailbox: a durable, same-account message store (schema
  v3) with `POST/GET /client/mailbox` and `POST /client/mailbox/read`,
  pull-only delivery, and unread counts in the session listings.
- `opencloddie msg` and `opencloddie inbox` commands; `opencloddie ls` now
  shows an `UNREAD` column.
- A per-session MCP server (`opencloddie __mcp`) exposing `list_sessions`,
  `send_session_message`, and `check_mailbox`.  The client runner writes a
  per-session `--mcp-config` file and exports `OPENCLODDIE_SESSION_ID` and
  `OPENCLODDIE_CONFIG` to the session environment.
- Web mailbox modal with unread tab badges, backed by
  `GET/POST /api/sessions/<id>/messages` and
  `POST /api/sessions/<id>/messages/read`.
- `messages` config block: enablement, retention (30 days), body size
  (64 KiB), unread cap (500), and send rate limit (60/minute).

### Upgrade notes

- Sessions already running in tmux do not pick up the MCP tools until they are
  recreated: `opencloddie stop NAME`, then
  `opencloddie run --name NAME -- --continue`.  `opencloddie msg` / `inbox`
  still work through Bash.

## [0.4.0] - 2026-11-09

### Added

- Statistics view in the web UI: instance health (uptime, memory, CPU, threads,
  FDs, load, database and disk size), gateway usage (requests, status codes,
  errors, rate-limit hits, bytes, input/output/cache tokens, upstream latency,
  per model and per API key), and session activity (online state, viewers,
  connected duration, last seen, terminal bytes).
- `GET /api/metrics?window=` with 1h / 6h / 24h / 7d ranges. Live counters are
  process-lifetime; per-minute aggregates are persisted in SQLite (schema v2)
  and retained for `metrics.retention_days` (default 7).
- Token accounting parsed from non-stream JSON `usage` and streamed
  `message_start` / `message_delta` SSE events. `count_tokens` is counted as a
  request without token usage.
- Members of `metrics.admin_groups` (default `god`, injected by the auth proxy)
  see all users' usage; everyone else sees only their own keys, sessions, and
  usage. Instance/process health is global, like `/api/system`.

## [0.3.0] - 2026-11-09

### Added

- WebGL renderer for the browser terminal (`@xterm/addon-webgl` 0.18.0), with
  automatic fallback to xterm's DOM renderer when WebGL2 is unavailable or the
  GPU context is lost.
- Mobile drag-to-scroll: a one-finger pan sends wheel reports to tmux
  copy-mode.  The client enables mouse mode on its own tmux session (the
  session option, not the user's global setting) and advertises the
  capability to the web UI, so older clients keep their previous touch
  behaviour.

### Fixed

- Mobile: activating a session no longer focuses the terminal, so the
  on-screen keyboard opens only when the terminal is tapped.
- Mobile: the app now follows `visualViewport`, so the keyboard shrinks the
  layout above it instead of covering the terminal and cursor.
- The agent's WebSocket transport now performs all TLS socket I/O in a single
  thread.  Concurrent reads and writes on Python's `SSLSocket` could corrupt
  the TLS record stream under load (`BAD_RECORD_MAC`), which is what closed
  the agent connection during fast scrolling.
- Fast scrolling no longer blocks the agent: pane input is buffered and
  written by a small writer thread, so the agent keeps reading its WebSocket
  while a TUI is busy redrawing.
- The agent resets its reconnect backoff after a connection stays up, so a
  single drop no longer leaves later reconnects waiting 30 seconds.
- Agent connection closures now log the initiator and reason.

## [0.2.0] - 2026-11-08

### Added

- Single/Tile layout switch in the web UI, persisted across reloads.  Tile
  mode shows every session at once with a per-tile header (live name, model,
  and status), an offline overlay, and a focus-to-single button; the grid is
  recomputed from the container size on layout and resize, and phones stay in
  single mode.

### Changed

- Tiled mode keeps a 2x2 grid and gives tile width back to terminal columns;
  wide windows still gain a column when the cells stay readable.
- Updated GitHub Actions dependencies (`checkout` v7, `login-action` v4,
  `metadata-action` v6, `build-push-action` v7, `action-gh-release` v3).

### Fixed

- The terminal scrollbar gutter is stable and does not paint over the last
  column; scrollbar arrow buttons are hidden and the last tile row is no
  longer clipped.
- Dropped the always-empty terminal scrollbar (`scrollback: 0`); tmux copy
  mode (`Ctrl-b [`) remains the history path and the width returns to the
  terminal.

## [0.1.0] - 2026-11-08

First public release.

### Added

- Anthropic-compatible `/v1/messages` and `/v1/messages/count_tokens` gateway
  to OpenCode Go, with SSE streaming, header forwarding, session affinity,
  per-key rate limits, and optional fallback model.
- Per-user OpenCode keys, encrypted at rest, plus per-API-key model selection
  in the web UI.
- `opencloddie` client: first-run setup, tmux-backed Claude Code sessions,
  detached agents, detach/reattach, and a standalone Python implementation.
- Web terminal over WebSocket with PTY resize, multiple sessions, and
  tabbed switching.
- tmux paste-buffer retrieval for selections made with tmux mouse mode, with
  clipboard fallbacks (manual text dialog and download).
- Web UI with burger navigation, live session tabs, API-key management,
  OpenCode settings, and model picker.
- nginx + Authelia integration, multi-arch Docker image, compose examples,
  and a systemd unit.
- 65 offline tests (unit, API, gateway SSE, client/agent end-to-end), CI, and
  comprehensive architecture, API, security, deployment, and troubleshooting
  documentation.

### Fixed

- `tmux attach` now receives `SIGWINCH` via `TIOCSCTTY`, so browser resizes
  actually resize tmux.
- Session tabs keep a stable order instead of re-sorting on every heartbeat.
- Agent read timeout no longer races the heartbeat and reconnects every few
  minutes.
- Clipboard copy works from xterm selections, the visible screen, and the tmux
  paste buffer.

[Unreleased]: https://github.com/dustify-slopwerke/opencloddie/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.5.0
[0.4.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.4.0
[0.3.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.3.0
[0.2.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.2.0
[0.1.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.1.0
