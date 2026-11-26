# Changelog

All notable changes to opencloddie are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.4] - 2026-11-26

### Fixed

- Approvals are reachable even when the inline card is not: the "N pending"
  chip now opens a modal that reuses the real permission card, including the
  `AskUserQuestion` answer form and plan approval, and posts the decision
  directly.
- The permission-mode picker no longer closes itself: the chat header skips a
  rebuild while a select/input inside it has focus.
- Scroll pinning now ignores the scroll events caused by its own auto-scroll
  and re-pins after mobile keyboard/viewport changes, so new output keeps up.

## [0.6.3] - 2026-11-26

### Fixed

- Web chat `/clear` now starts a new conversation like Claude Code: the runner
  records the CLI's conversation reset as a durable boundary, the server hides
  (but retains) earlier events from reads, search, pagination, checkpoints and
  unread counts, and the open timeline clears as soon as the boundary arrives.
  The previous transcript stays stored; **Clear history** still deletes it.
- Web chat no longer loses history on reload.  It used to load only the newest
  200 events and had no way to request older ones, so a refresh after a
  tool-heavy stretch dropped earlier turns (the data was never gone).  The
  initial page is now 500 events, a "Load earlier messages" control and
  near-top auto-load page backwards with `before_seq`, and replayed pages do
  not disturb the live turn's status, tools or scroll position.  `has_more`
  now reports whether an older visible event exists.

## [0.6.2] - 2026-11-26

### Fixed

- Chat no longer forces `--permission-mode default`; by default it defers to
  Claude Code's `permissions.defaultMode` and allow rules, matching the TUI.
  `chat.permission_mode` (server config) or the web header picker can still
  force a mode; the picker is now visible on mobile too.
- New approval cards scroll themselves into view; the "N pending" chip reloads
  the snapshot and focuses the card if it is not loaded (mobile reconnect).
- Pending approvals are expired when the chat runner exits or the session
  switches to terminal mode, and pending badges are hidden outside chat mode,
  so a stopped runner cannot leave a stale "1 pending" chip.
- The permission-mode picker includes a "settings" (defer) option.

## [0.6.1] - 2026-11-25

### Fixed

- Chat sessions no longer sit on "Starting…"/"Claude is working…" before the
  first turn; the runner marks itself ready once the child is spawned.
- Chat ↔ terminal handoff no longer calls `tmux respawn-pane -k`, whose kill
  signal raced and could tear down the freshly started runner.  The mode
  supervisor now swaps the child in place (the TUI prompt loop notices the mode
  file within a second, and chat→TUI stops the runner).
- Pre-0.6.1 sessions are migrated automatically on `opencloddie run`; the
  migration waits for the old runner to release its socket before starting the
  replacement.
- The chat runner only uses `--resume` when the session transcript actually
  exists; otherwise it starts with the preassigned id.  It also records the id
  the CLI reports if it differs, so handoff no longer retries a nonexistent
  conversation.
- Web chat: duplicate "No messages yet" empty states are removed before each
  snapshot; tab clicks activate on pointerdown and the tab strip is not rebuilt
  when nothing changed; tool cards left in the orange "running" state at the end
  of a turn are marked done.

## [0.6.0] - 2026-11-25

### Added

- Structured chat sessions (experimental): `opencloddie run --chat` runs Claude
  Code in its documented headless `stream-json` mode and ships normalized
  events instead of terminal bytes.  The web UI renders streaming Markdown,
  tool calls and diffs, inline approvals and `AskUserQuestion`, plan mode,
  subagents and todos.  Requires Claude Code 2.1.300+.
- Chat/terminal handoff for the same Claude conversation
  (`claude --resume <session-id>`) through the tmux mode supervisor.
- Chat persistence in SQLite schema v4 (`chat_state`, `chat_events`,
  `chat_permissions`, `chat_inputs`, `chat_reads`, `chat_checkpoints`), with
  owner-only access, retention, history deletion and configurable limits.
- A separate `opencloddie_permissions` MCP server and `approval_prompt` tool;
  chat mode refuses to start if approvals cannot be routed (it never bypasses
  them silently).
- `/api/sessions/<id>/chat` REST and WebSocket endpoints; the session list
  gains a `chat` summary (`mode`, `status`, `pending`, `unread`, `last_seq`).
- Image input: the composer accepts pasted/dropped PNG/JPEG/GIF/WebP images
  (size/count caps); inline images render from `data:` URLs and oversized ones
  become placeholders.  Anthropic content-block images pass through to Claude.
- Conversation search (`GET /api/sessions/<id>/chat/search`) and a header search
  panel; owner-scoped, plain `LIKE` over stored payloads.
- File checkpoint rewind: user turns expose "Restore files to here", which
  pauses the runner, runs `claude -p --resume <id> --rewind-files <uuid>`, and
  resumes.  Files are restored; the conversation is not rewound.
- Permission-mode picker in the chat header (restart+resume; refused while
  busy), and an end-to-end approval test through the real permission MCP shim.
- Chat status rows for API retries, rate limits, auto-compaction boundaries and
  prompt suggestions (the runner forwards the CLI events).
- `#session=<id>` deep links from `opencloddie run --chat`.
- Protocol notes and Phase 0 validation in `docs/CHAT-PROTOCOL.md`.

### Changed

- The generated client runner is now a mode supervisor driven by
  `state_dir/mode-<session>`; `tmux respawn-pane -k` switches between the TUI
  and the chat runner without losing the tmux session or agent connection.
- Multiple `--mcp-config` flags are now merged; a user-supplied config no
  longer suppresses opencloddie's mailbox server.

## [0.5.1] - 2026-11-14

### Added

- Tile mode can hide individual sessions from the grid.  Hidden sessions are
  remembered across reloads and can be restored from the topbar
  `Hidden (n)` menu.

### Changed

- Removed the topbar `Connect session`, `Refresh`, `Copy attach`, and `tmux`
  buttons from the sessions view.  The `+` tab still connects a session.

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

[Unreleased]: https://github.com/dustify-slopwerke/opencloddie/compare/v0.6.3...HEAD
[0.6.3]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.6.3
[0.6.2]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.6.2
[0.6.1]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.6.1
[0.6.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.6.0
[0.5.1]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.5.1
[0.5.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.5.0
[0.4.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.4.0
[0.3.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.3.0
[0.2.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.2.0
[0.1.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.1.0
