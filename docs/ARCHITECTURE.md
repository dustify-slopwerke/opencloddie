# Architecture

## Goals

- Let a Claude Code user authenticate to a shared gateway with a key they
  generated in a web UI, spend against **their own** OpenCode Go subscription,
  and use **their chosen model**.
- Let that user run Claude Code wherever their code lives, keep it alive in
  tmux, and reach the same live terminal from a browser.
- Keep the server small enough to audit: Python 3.12 standard library only, no
  build step, no database server, one container.

Non-goals for v0.2: multi-node fan-out, server-hosted workspaces, collaborative
multi-cursor editing, request translation between protocols.

## Process model

One process (`opencloddie-server run`) starts two `ThreadingHTTPServer`
listeners and a watchdog thread:

```
┌──────────────────────────── opencloddie-server ────────────────────────────┐
│                                                                            │
│  web listener :8787 (internal)        api listener :8788 (public)          │
│  ├─ /                  UI             ├─ /v1/messages       gateway        │
│  ├─ /api/me ...        account API    ├─ /v1/models                        │
│  ├─ /api/sessions      list/delete    ├─ /client/me|sessions  CLI API      │
│  └─ /api/sessions/:id/ws  terminal    ├─ /agent/ws           agent WS      │
│                                       └─ /install/*          client file   │
│                                                                            │
│  App: config, SQLite Database, Gateway, AgentHub, model catalogue           │
│  Watchdog: closes agents silent for >120s                                   │
└────────────────────────────────────────────────────────────────────────────┘
```

They must share one process because the browser terminal handler needs the live
agent WebSocket object.  The alternative (two containers) would require an IPC
bridge for every keystroke, which is disproportionate for this design.  The
split is preserved logically: different ports, different auth, different route
tables.  nginx enforces the network boundary.

Threads are used rather than asyncio because everything is blocking I/O (PTY
reads, upstream HTTP streaming, WebSocket reads), and a thread per connection
is simple and adequate for the expected user count.  `ThreadingHTTPServer` uses
daemon threads, so a wedged keep-alive cannot block shutdown.

## Data model (SQLite)

`opencloddie/db.py`, WAL mode, one connection guarded by an `RLock`.

```
users(id, username UNIQUE, email, name, groups JSON, default_model,
      opencode_key_enc, opencode_key_hint, created_at, updated_at, last_seen_at)

api_keys(id, user_id FK, name, prefix, token_hash UNIQUE, model,
         created_at, last_used_at, revoked_at)

sessions(id PK, user_id FK, api_key_id, name, hostname, cwd, model,
         online, agent_id, created_at, last_seen_at, metadata JSON)

usage_minutes(bucket, user_id, api_key_id, api_key_name, username, model,
              requests, ok, client_errors, server_errors, rate_limited,
              bytes_in, bytes_out, tokens_in, tokens_out,
              cache_read_tokens, cache_creation_tokens)

instance_minutes(bucket, sessions_online, sessions_total, agents, viewers,
                 rss_bytes, cpu_seconds, threads, db_bytes)
```

- Users are created on first web request from `Remote-User`; there is no
  password store.
- `token_hash = HMAC-SHA256(master-derived key, "opencloddie-api-key-v1" + token)`
  and the token itself is only returned at creation.
- `opencode_key_enc` is the `v1.<nonce>.<ct>.<tag>` format documented in
  `SECURITY.md`.
- `sessions` is a registry, not a process table.  The terminal lives in the
  client's tmux; deleting a row and asking the agent to kill the tmux session is
  the only "server-side" lifecycle action.
- On startup all sessions are marked offline (no agent can have survived), and
  offline rows older than 90 days are purged.  Agents reconnect and re-upsert.

## Gateway request flow

Client (Claude Code) → `POST https://host/v1/messages`:

1. Parse `Authorization: Bearer ocd_sk_...` (or `x-api-key`, or `?token=`).
2. Hash and look up a live API key → user, key, key's model.
3. Decrypt the owner's OpenCode key; error 400 with a UI hint if absent.
4. Enforce the per-key sliding-window rate limit.
5. Parse the JSON body, set `model` to the key's model (unless
   `allow_client_model_override` and the requested model is in the catalogue).
6. Build upstream headers:
   - forward `content-type`, `anthropic-version`, `anthropic-beta`,
     `x-app`, the client `User-Agent`, Stainless telemetry, and duplicates
     joined with `, `;
   - drop `host`, `content-length`, client `authorization`/`x-api-key`,
     cookies, `accept-encoding`, hop-by-hop headers;
   - preserve `X-Claude-Code-Session-Id` and mirror it into
     `x-opencode-session`; if neither exists, derive a stable affinity id from
     the API key so prompt caching is not defeated;
   - set `Authorization: Bearer <opencode key>` (or `x-api-key`).
7. `http.client` connects (connect timeout) and then raises the socket timeout
   to the read timeout for SSE.
8. Upstream status/headers are relayed (minus hop-by-hop, `content-length`,
   `content-encoding`, `set-cookie`), plus `x-opencloddie-model` and
   `x-opencloddie-request-id`.
9. For `text/event-stream`, chunks are re-chunked to the client and flushed
   immediately; gzip is decoded incrementally if upstream returns it.  For JSON
   responses the body is read, decompressed if needed, and returned with a
   content length.
10. Optional one-shot fallback: if the first upstream response is 408/429/5xx
    and `gateway.fallback_model` is configured and different, retry once with
    the fallback model (only before anything is streamed).
11. Log model, key name, status, stream flag, request id — never the key or
    message content.

Why no translation: OpenCode Go exposes an Anthropic-compatible endpoint, so
Claude Code's request goes through verbatim except for auth/model.  That keeps
tool use, prompt caching, thinking blocks, and future Anthropic features
working.

`count_tokens` follows the same path.  `GET /v1/models` returns the catalogue in
Anthropic list shape.  `X-Claude-Code-Session-Id` was verified against the real
upstream; OpenCode recognizes it natively, and `x-opencode-session` is sent as
well for routing/prompt-cache affinity.

## Session flow

### Start (`opencloddie run`)

1. Read config (server URL + API key).  If absent and interactive, prompt.
2. `GET /client/me` → user, key model, context window, public web URL.
3. Resolve the session mapping (`~/.config/opencloddie/sessions.json`) by
   name+cwd; reuse it or create `s_<hex>` and tmux name `ocd-<slug>-<hex>`.
4. Write a 0700 runner script containing the gateway env and a restart loop:
   ```
   export ANTHROPIC_BASE_URL=<api>
   export ANTHROPIC_AUTH_TOKEN=<api key>
   export ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL=<key model>
   export CLAUDE_CODE_MAX_CONTEXT_TOKENS=<context>
   while :; do claude --model <key model> "$@"; read -r _ || break; done
   ```
5. `tmux new-session -d -s <tmux> -c <cwd> -- bash <runner>`.
6. Spawn the detached agent (`opencloddie __agent ...`), PID/log under the
   state directory.
7. Wait up to 10s for the server to report the session online.
8. Attach the local terminal with `tmux attach-session` (unless `--no-attach`).

### Agent registration and terminal channels

```
client                                      server
  │ WS /agent/ws  Authorization: Bearer key │
  │──── hello {agent_id, session{id,name,cwd}} ───▶│  validate key -> user
  │◀─── welcome {session_id, model} ───────────────│  upsert session, online=1
  │                                                │
  │◀─── open {ch, cols, rows} ─────────────────────│  browser opened terminal
  │  spawn `tmux attach` in a PTY                  │
  │──── opened {ch} ──────────────────────────────▶│
  │──── out {ch, base64(bytes)} ──────────────────▶│──binary WS──▶ browser
  │◀─── in {ch, base64(bytes)} ────────────────────│◀─binary WS── browser
  │◀─── resize {ch, cols, rows} ───────────────────│
  │──── exit {ch, code} ──────────────────────────▶│  close viewer
```

Each browser viewer gets its own `tmux attach` PTY on the client.  That is why
resizing and full-screen redraws behave correctly with multiple viewers, and
why a browser reconnect is a fresh attach rather than a replay of a ring
buffer.  Input is shared: tmux is the single source of truth, so local and web
terminals mirror each other.

Heartbeats: the agent sends `{"t":"ping"}` every 20s; the server replies
`{"t":"pong"}` and touches `last_seen_at`.  The watchdog closes connections
silent for 120s and marks their session offline.

Reconnect: the agent loops with 1, 2, 4 … 30s backoff while its tmux session
exists.  On 401/403 it backs off 60s (the key may have been rotated).  A server
restart is invisible apart from a brief offline/online transition.

Kill: `DELETE /client/sessions/<id>` or the web UI sends `{"t":"kill"}` to the
agent; the agent kills its tmux session and exits.  If the agent is gone, the
server just deletes the row.

### Browser terminal protocol

- WebSocket `/api/sessions/<id>/ws?cols=&rows=` on the **web** listener.
- Auth: `Remote-User` header injected by nginx (Origin checked against Host).
- Server → browser: binary frames = raw PTY bytes; text frames = JSON control
  (`hello`, `error`, `exit`, `pong`).
- Browser → server: binary frames = raw keystrokes/paste; text JSON =
  `{"t":"resize","cols":..,"rows":..}` or `{"t":"ping"}`.
- The handler owns a `Channel`; a pump thread forwards the channel queue to the
  browser and a reader loop forwards browser input to the agent.

## URL derivation

Public URLs are needed by `/client/me` (client gateway base + web link) and the
UI (install commands).

1. Use `server.public_web_url` / `public_api_url` if set.
2. Else if `server.behind_proxy` (Docker default), derive one origin from
   `X-Forwarded-Proto` + `Host`; both listeners share it.
3. Else derive `http://<host>:web_port` and `http://<host>:api_port`, which is
   what local development needs.

## Statistics and instrumentation

`opencloddie/metrics.py` is a process-local, thread-safe registry for the web
Statistics view.  Nothing in it is required for the gateway or terminal paths;
if the registry is unavailable, requests still flow.

- **Gateway** (`gateway.py`): `proxy()` takes one metric token per logical
  request.  `ProxyResult.finish_metrics()` records status, bytes, latency, and
  tokens exactly once, whether the body is read with `read_all()`, streamed with
  `iter_bytes()`, or closed after a client disconnect.  A fallback retry is one
  request, not two.  Streams feed an incremental SSE parser for
  `message_start` / `message_delta` usage; non-stream bodies parse `usage` from
  JSON.  `Gateway.rate_snapshot()` exposes the sliding-window limit read-only.
- **Agent hub** (`sessions.py`): each connection tracks `connected_at` and
  terminal payload bytes; the hub keeps lifetime connection/channel/byte
  counters.  `snapshot(user_id)` returns live connections scoped by owner and
  reports the real `last_seen`, not the request time.
- **Process health**: `process_snapshot()` samples `/proc/self/status`,
  `/proc/self/io`, `/proc/self/fd`, `os.times()`, load average, disk usage, and
  SQLite file size.  Every field degrades to `null` instead of failing.
- **Persistence**: `App.metrics_tick()` (called by the watchdog and by
  `GET /api/metrics`) flushes pending per-key deltas and one instance sample
  into `usage_minutes` / `instance_minutes`, at most once per
  `metrics.sample_seconds` (default 60s).  Rows older than
  `metrics.retention_days` (default 7) are purged hourly and at startup.
- **Scoping**: `GET /api/metrics` filters per-key/session/usage data to the
  caller unless they are in `metrics.admin_groups` (default `["god"]`, supplied
  by the auth proxy).  Instance/process counters are global, matching
  `/api/system`.
- **Single node**: counters are in memory, so live numbers reset when the
  process restarts.  Persisted minute buckets survive and are the source for
  the charts.  A multi-replica deployment would need a shared metrics store.

## Design decisions

- **Model per API key, not per account or time.**  The user asked to pick in the
  UI; a key is the natural unit because that is what a client config carries.
  Changing it applies on the next request.
- **No server-hosted Claude sessions.**  Claude Code runs where the code is.
  The server never needs a Claude install, a workspace volume, or tmux.
- **One agent per session.**  Simple registration, independent lifecycles, no
  multiplexing bugs.  A client with ten sessions runs ten tiny agents.
- **tmux is the persistence layer.**  Closing a terminal, losing Wi-Fi, or
  restarting the server does not kill Claude.  The agent is disposable.
- **Registry, not truth.**  `sessions.online` is a cache; the hub is consulted
  first when serving a terminal.  A stale row can never fake an online session.
- **Gateway is a pass-through.**  No message transformation means the long tail
  of Claude Code features keeps working.

## Limits

- Single node: the agent hub is in-memory.  Running multiple API replicas would
  split agents from web viewers.  Scale vertically or add a routing layer.
- One process serves both listeners; a Python GIL is fine for streaming I/O but
  CPU-heavy workloads are not this server's job.
- SQLite is appropriate for a personal/team instance.  WAL supports the
  concurrent readers/writers here; move to Postgres if you outgrow it.
- Browser terminals are shared-input by design (tmux semantics), not
  collaborative editors.
