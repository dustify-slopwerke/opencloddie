# Backlog

## OpenCode usage indicator

**Implemented (2026-11-13).**  Three thin bars in the session toolbar (left of
the Single/Tile toggle) show the rolling (5-hour), weekly, and monthly Go
windows; tooltips carry the percentage and reset time.  Backend:
`GET /api/usage` (`docs/API.md`) calls
`https://opencode.ai/inference/go/v1/usage` with the user's stored inference
key, normalizes the result to `{rolling, weekly, monthly}`, and caches it for
60 seconds.

Notes from the probe:

- The Console endpoint (`/console/api/activity/usage`) needs a **Usage: Read**
  permission, but the inference endpoint above works with the normal key — no
  key change is required.
- opencode.ai is behind Cloudflare and returns **error 1010
  (`browser_signature_banned`)** to Python-urllib's default User-Agent.  Send
  `gateway.user_agent` (default `opencloddie-gateway/0.1`); curl's UA is fine.
- Response shape:
  `{"usage":{"rolling":{"status","percent","resetsAt"}, ...}}`.
- Bar color: accent below 80%, warn 80–94%, danger 95%+.

The older fallback (accumulate `usage.opencode.allowance` from gateway
responses) is not needed.

## Terminal tab order

Fixed: `Database.list_sessions` now orders by `created_at, id` instead of
`online, last_seen_at`, so heartbeat updates no longer reshuffle tabs. A
regression test covers it.

## Possible follow-ups

- Per-viewer terminal sizing policy (`window-size largest` vs `latest`) exposed
  as a session option.
- Debounce/`visualViewport` handling for mobile resize.
- Optional user-defined tab ordering / pinning.
- Usage alerts when a window crosses a configurable threshold.
