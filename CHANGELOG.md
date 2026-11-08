# Changelog

All notable changes to opencloddie are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/dustify-slopwerke/opencloddie/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.2.0
[0.1.0]: https://github.com/dustify-slopwerke/opencloddie/releases/tag/v0.1.0
