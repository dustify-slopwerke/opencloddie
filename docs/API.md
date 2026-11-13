# API reference

Two listeners with different auth:

| Listener | Default | Auth | Reachable from |
| --- | --- | --- | --- |
| API | `:8788` | `Authorization: Bearer ocd_sk_...` (or `x-api-key`) | the internet via nginx |
| Web | `:8787` | `Remote-User` header injected by nginx/Authelia | nginx only |

The API is "open to all" in the sense that it needs no network allowlist; every
request is authenticated by an API key and rate limited.  The web listener is
not safe to expose: it trusts the identity header.

## Public API (`:8788`)

### `GET /healthz`

```json
{"ok": true, "version": "0.4.0", "mode": "api", "uptime": 42}
```

No auth.

### `GET /install/opencloddie`

Returns the client script as `text/x-python` (no auth).  `GET /install` returns
an HTML install page.

### `POST /v1/messages`

Anthropic Messages API, streamed or not.  This is the endpoint Claude Code
uses.

Required headers:

```http
Authorization: Bearer ocd_sk_...
Content-Type: application/json
anthropic-version: 2023-06-01
```

Optional and forwarded to OpenCode Go: `anthropic-beta`, `x-app`, `User-Agent`,
`X-Claude-Code-Session-Id`, Stainless headers.  `X-Claude-Code-Session-Id` is
mirrored into `x-opencode-session` for routing/prompt-cache affinity.  If no
session id is present, a stable id derived from the API key is used.

Request body is the normal Messages payload.  `model` is overwritten with the
API key's selected model.  Example:

```bash
curl -N https://cloddie.example.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Claude-Code-Session-Id: $(uuidgen)" \
  -d '{
    "model": "claude-sonnet-5-5",
    "max_tokens": 64,
    "stream": true,
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

The response is the upstream Anthropic response, byte-compatible for SSE.  For
streams the server uses chunked transfer and flushes each read.  Additive
response headers:

| Header | Meaning |
| --- | --- |
| `x-opencloddie-model` | model actually used |
| `x-opencloddie-request-id` | server request id (`req_...`) |

Errors use the Anthropic shape:

```json
{"type":"error","error":{"type":"authentication_error","message":"invalid or revoked opencloddie API key"}}
```

Statuses:

| Status | Meaning |
| --- | --- |
| 400 | bad JSON, invalid request, no OpenCode key configured |
| 401 | missing/invalid API key |
| 403 | forbidden (rare) |
| 429 | per-key rate limit; `Retry-After: 60` |
| 502 | upstream unreachable/read failure |
| upstream status | 4xx/5xx from OpenCode Go is relayed verbatim |

Model selection errors (no key) include a message pointing at the web UI.

### `POST /v1/messages/count_tokens`

Same auth and model forcing; proxies to the upstream count-tokens endpoint.

### `GET /v1/models`

API-key authenticated.  Anthropic list shape built from `gateway.models`:

```json
{"data":[{"type":"model","id":"deepseek-v4.1-flash","display_name":"DeepSeek V4.1 Flash","created_at":"2026-01-01T00:00:00Z"}],"has_more":false,"first_id":"...","last_id":"...","ok":true}
```

### `GET /client/me`

The client's bootstrap endpoint.

```json
{
  "ok": true,
  "user": {"username": "alice", "name": "Alice"},
  "key": {"id": "k_...", "name": "laptop", "model": "deepseek-v4.1-flash"},
  "model": "deepseek-v4.1-flash",
  "context_tokens": 1000000,
  "models": [{"id": "...", "label": "...", "context_tokens": 1000000, "note": "..."}],
  "opencode_key_set": true,
  "messages_enabled": true,
  "gateway_base_url": "https://cloddie.example.com",
  "web_url": "https://cloddie.example.com",
  "install": {"command": "curl -fsSL .../install/opencloddie ..."}
}
```

`context_tokens` is `null` for models whose window is not published; the client
then leaves Claude Code's default in place.

### `GET /client/sessions`

Owner-scoped registry:

```json
{"ok":true,"unread_total":1,"sessions":[{
  "id":"s_9f3a...","name":"work","hostname":"laptop",
  "cwd":"/home/alice/src/api","model":"deepseek-v4.1-flash",
  "online":true,"created_at":"2026-11-07T12:00:00Z",
  "last_seen_at":"2026-11-07T12:34:56Z","last_seen":"12s ago","viewers":1,
  "unread":1
}]}
```

`unread` is the number of unread mailbox messages addressed to that session;
`unread_total` is the sum across the caller's sessions.

### `GET /client/sessions/<id>`

Single session in the same shape.

### `DELETE /client/sessions/<id>`

Asks the owning agent to kill its tmux session, then removes the registry row.
Returns `{"ok":true}`.  404 if the id is not owned by the key's user.

## Session mailbox

A durable, same-account mailbox between sessions.  Delivery is pull-only: the
receiving session reads with `GET /client/mailbox`; nothing is pushed into a
running Claude session.  Messages survive server restarts and offline sessions.
Every query is scoped to the API key's user.

### `POST /client/mailbox`

```json
{"session": "s_abc", "to": "eve-o-tool", "body": "please work up X", "reply_to": 42}
```

`session` is the sender's session id (optional for a human CLI call); `to`
accepts an exact session id, an exact case-insensitive name, or
`name@hostname`.  `reply_to` is optional.  The target may be offline; the
message is queued.

```json
{"ok":true,
 "message":{"id":43,"from_session_id":"s_abc","from_name":"consumer",
            "to_session_id":"s_def","to_name":"eve-o-tool","body":"...",
            "reply_to":null,"created_at":"2026-11-13T12:00:00Z","read_at":null},
 "target":{"id":"s_def","name":"eve-o-tool","hostname":"buildbox","online":true},
 "unread":1}
```

Errors: `messages_disabled` (403), `invalid_request` (400),
`target_not_found` (404, with `available`), `ambiguous_target` (409, with
`candidates`), `cannot_message_self` (400), `mailbox_full` (409),
`message_too_large` (413), `rate_limit_error` (429 with `Retry-After`).

### `GET /client/mailbox?session=<id>&unread=1&limit=20&after=0`

Returns messages addressed to that session, oldest first.  `unread=0` includes
read messages; `after` pages by message id.  `limit` is capped at 100.

```json
{"ok":true,"messages":[...],"unread":1,"has_more":false}
```

### `POST /client/mailbox/read`

```json
{"session":"s_def","ids":[43,44]}
```

Marks those messages read (only if addressed to that session and owned by the
caller).  Returns `{"ok":true,"marked":2}`.

## Agent WebSocket (`/agent/ws`)

Used by `opencloddie __agent`.  Auth via the same API key (header or query).
Handshake, then a JSON hello:

```json
{"t":"hello","agent_id":"a-...","version":"0.4.0","hostname":"laptop",
 "platform":"linux","session":{"id":"s_...","name":"work","cwd":"/home/alice/src/api"}}
```

Server replies:

```json
{"t":"welcome","server_time":"...","session_id":"s_...","model":"deepseek-v4.1-flash"}
```

Then the framed protocol (text JSON frames; `data` is base64):

| Direction | Message | Meaning |
| --- | --- | --- |
| server→agent | `{"t":"open","ch":1,"cols":100,"rows":30}` | spawn a `tmux attach` PTY |
| agent→server | `{"t":"opened","ch":1}` | ready |
| agent→server | `{"t":"out","ch":1,"data":"..."}` | PTY output |
| server→agent | `{"t":"in","ch":1,"data":"..."}` | keystrokes |
| server→agent | `{"t":"resize","ch":1,"cols":120,"rows":40}` | resize |
| server→agent | `{"t":"get_buffer","ch":1}` | read this host's newest tmux paste buffer |
| agent→server | `{"t":"buffer","ch":1,"data":"...","created":1794110356,"size":33,"name":"buffer30"}` | tmux buffer contents (base64); `error` instead when empty |
| server→agent | `{"t":"close","ch":1}` | close this viewer's PTY |
| agent→server | `{"t":"exit","ch":1,"code":0}` | PTY ended |
| agent→server | `{"t":"error","ch":1,"message":"..."}` | channel error |
| server→agent | `{"t":"kill"}` | kill tmux and exit |
| both | `{"t":"ping","ts":...}` / `{"t":"pong","ts":...}` | heartbeat (20s) |

A connection registered for one session id cannot steal another user's id
(409/`handshake failed`).  The hub is in-memory; a reconnecting agent replaces
the old connection and re-marks the session online.

## Web terminal WebSocket (web listener)

`GET /api/sessions/<id>/ws?cols=120&rows=32`

Auth: `Remote-User` (injected by nginx); the browser's `Origin` must match
`Host`.  The user must own the session.

Frames:

| Direction | Type | Payload |
| --- | --- | --- |
| server→browser | binary | raw PTY bytes (UTF-8 terminal stream) |
| server→browser | text | `{"t":"hello","session":{...}}`, `{"t":"error","message":"..."}`, `{"t":"exit","code":0}` |
| server→browser | text | `{"t":"tmux_buffer","data":"<base64>","created":1794110356,"size":33,"name":"buffer30"}` or `{"t":"tmux_buffer","error":"..."}` |
| browser→server | binary | raw input bytes |
| browser→server | text | `{"t":"resize","cols":120,"rows":40}`, `{"t":"ping"}`, `{"t":"get_tmux_buffer"}` |

`get_tmux_buffer` is proxied to the agent, which runs
`tmux show-buffer` on the session host.  This is how the UI copies text the
user selected with tmux mouse mode (that selection lives in tmux's buffer, not
xterm's).  `created` is the buffer's Unix timestamp; the UI only uses a buffer
automatically for Copy when it is under 120 seconds old and falls back to the
visible screen otherwise.  The dedicated **tmux** action always uses it.

If the session is offline, the server accepts the handshake, sends an error
text frame, and closes with 1011.  The browser UI shows an offline overlay and
reconnects when the agent returns.

## Web management API (`:8787`)

All routes require the trusted identity header.  Mutating routes additionally
require `X-OpenCloddie-Client: web`, a same-origin `Origin` (when present), and
`Sec-Fetch-Site` not cross-site.  Responses are JSON
`{"ok":true,...}` or `{"ok":false,"error":{"type":"...","message":"..."}}`.

### `GET /api/me`

```json
{"ok":true,
 "user":{"username":"alice","name":"Alice","email":"a@example.com","groups":["god"],"default_model":"deepseek-v4.1-flash"},
 "opencode_key_set":true,"opencode_key_hint":"***********1234",
 "messages_enabled":true,
 "models":[...],"default_model":"deepseek-v4.1-flash",
 "urls":{"web":"https://cloddie.example.com","api":"https://cloddie.example.com"},
 "install":{"command":"...","login":"..."}}
```

### `GET /api/models`

`{"ok":true,"models":[...]}`.

### `GET /api/stats`

`{"ok":true,"stats":{"api_keys":2,"sessions_online":3,"sessions_total":5,"agents":3,"sessions":3}}`.

### `GET /api/metrics?window=3600`

Powers the Statistics view.  `window` is one of `3600` (default), `21600`,
`86400`, or `604800` seconds, clamped to `metrics.retention_days`.  The server
returns the requested (or nearest allowed) range and its resolution in
`step_seconds` (1m / 5m / 15m / 1h).

```json
{"ok":true,"generated_at":"...","scope":"admin|self",
 "window_seconds":3600,"step_seconds":60,
 "live":{
   "process":{"version":"0.4.0","uptime_seconds":9200,"pid":123,"python":"3.12.8",
              "platform":"linux","threads":24,"open_fds":34,
              "memory":{"rss_bytes":52428800,"vms_bytes":0,"peak_rss_bytes":0},
              "cpu":{"user_seconds":1.2,"system_seconds":0.4},
              "load":{"1m":0.3,"5m":0.2,"15m":0.1},
              "io":{"read_bytes":0,"write_bytes":0},
              "disk":{"total_bytes":0,"free_bytes":0},
              "database":{"size_bytes":123456}},
   "instance":{"sessions_online":1,"sessions_total":3,"agents":1,"viewers":0,
               "users":2,"api_keys_active":2},
   "gateway":{"since":"...","requests":42,"in_flight":0,"ok":40,
              "client_errors":1,"server_errors":0,"rate_limited":1,
              "bytes_in":1234,"bytes_out":5678,
              "tokens":{"input":100,"output":50,"cache_read":0,"cache_creation":0},
              "upstream_ms":{"avg":120.5,"p50":100,"p95":300,"max":900},
              "by_status":{"200":40,"429":1,"502":1},
              "by_model":[{"model":"...","requests":40,"errors":1,
                           "bytes_in":0,"bytes_out":0,"tokens_in":0,"tokens_out":0}],
              "by_key":[{"id":"k_...","name":"laptop","user":"alice","prefix":"ocd_sk_ab12…",
                         "model":"...","revoked":false,"requests":40,"errors":1,
                         "rate_limited":0,"bytes_in":0,"bytes_out":0,
                         "tokens_in":0,"tokens_out":0,"last_used_at":"...",
                         "rate":{"used":3,"limit":120,"remaining":117,
                                 "reset_in_seconds":42}}]},
   "sessions":[{"id":"s_...","name":"work","user":"alice","hostname":"laptop",
                "cwd":"...","model":"...","online":true,"viewers":1,
                "connected_at":"...","connected_seconds":300,"last_seen_at":"...",
                "last_seen":"3s ago","terminal_bytes_in":10,"terminal_bytes_out":2000}],
   "hub":{"agents":1,"sessions":1,"viewers":0,"agents_connected_total":4,
          "agents_disconnected_total":3,"channels_opened_total":7,
          "terminal_bytes_in_total":10,"terminal_bytes_out_total":2000}},
 "history":{
   "gateway":[{"t":1762690000,"requests":3,"ok":3,"client_errors":0,
               "server_errors":0,"rate_limited":0,"bytes_in":10,"bytes_out":20,
               "tokens_in":3,"tokens_out":4,"cache_read_tokens":0,
               "cache_creation_tokens":0,"errors":0}],
   "instance":[{"t":1762690000,"sessions_online":1,"sessions_total":3,
                "agents":1,"viewers":0,"rss_bytes":0,"cpu_seconds":0,
                "threads":0,"db_bytes":0}]}}
```

- **Live counters are process-lifetime** and reset on restart;
  `history` is flushed to SQLite once per `metrics.sample_seconds` (default
  60s) and retained for `metrics.retention_days` (default 7).  Sparse history
  is normal: the UI fills missing buckets with zero.
- **Scope:** members of `metrics.admin_groups` (default `["god"]`, injected by
  the auth proxy) get `scope:"admin"` and every user's keys, sessions, and
  usage history.  Everyone else gets `scope:"self"` and only their own keys,
  sessions, and usage.  Process/instance health is always global, like
  `/api/system`.
- **Tokens** are parsed from non-stream JSON `usage` and from streamed
  `message_start` / `message_delta` SSE events.  `count_tokens` is counted as a
  request but has no usage block, so it adds no tokens.
- Key tokens and OpenCode keys are never returned; only key ids, names, and
  masked prefixes.
- Served only by the web listener; the API listener returns 404.

### `GET /api/system`

Version, uptime, URLs, model catalogue, upstream/auth style, hub counts.

### `PUT /api/opencode-key`

```json
{"key":"oc_sk_..."}
```

Encrypts and stores it; returns `{"ok":true,"opencode_key_hint":"...1234"}`.
`DELETE /api/opencode-key` clears it.  Neither ever returns the plaintext.

### `POST /api/opencode-key/test`

`{"key":"oc_sk_..."}` tests an unsaved key; `{}` tests the stored one.  Performs
a minimal one-token upstream request:

```json
{"ok":true,"result":{"ok":true,"status":200,"message":"..."}}
```

A 401/403 means invalid; 429/5xx is reported as reachable-but-busy.

### `PUT /api/settings`

```json
{"default_model":"qwen3.8-flash"}
```

Sets the default for **new** keys.  Existing keys keep their model.

### `GET /api/keys`

```json
{"ok":true,"keys":[{"id":"k_...","name":"laptop","prefix":"ocd_sk_ab12…",
 "model":"deepseek-v4.1-flash","created_at":"...","last_used_at":"...",
 "revoked":false,"revoked_at":null}]}
```

`POST /api/keys` with `{"name":"laptop","model":"qwen3.8-flash"}` creates one and
returns the plaintext token **once**:

```json
{"ok":true,"key":{"id":"k_...","token":"ocd_sk_...","name":"laptop","model":"..."},"notice":"Copy this key now; it is never shown again."}
```

`PATCH /api/keys/<id>` accepts `name`, `model`, `revoked` (bool).
`DELETE /api/keys/<id>` removes the row (revoked keys are usually kept for
audit; delete is available too).

### `GET /api/sessions`

Owner-scoped list, same shape as `/client/sessions`.

### `GET /api/sessions/<id>` / `DELETE /api/sessions/<id>`

Fetch or kill/remove (same semantics as the client DELETE).

### `GET /api/sessions/<id>/ws`

Terminal WebSocket described above.

### `GET /api/sessions/<id>/messages?peer=<id>&limit=50`

The human mailbox view.  Returns messages involving the session (both
directions); `peer` restricts to one conversation.  Both session ids must be
owned by the caller.  The payload matches `/client/mailbox`, plus `unread`.

### `POST /api/sessions/<id>/messages`

Send as the web user (no session sender).  Body `{"body":"..."}`.  Requires the
usual web CSRF header.  Returns 201 with `{"ok":true,"message":...,"unread":n}`.

### `POST /api/sessions/<id>/messages/read`

Body `{"ids":[1,2]}`.  Marks messages addressed to that session read; the web
view never acks implicitly.

### `GET /healthz`

`{"ok":true,"version":"0.4.0","mode":"web"}` (no auth).

## Static assets

`/`, `/index.html`, `/app.js`, `/stats.js`, `/style.css`, `/favicon.svg`,
`/vendor/*`.  Vendored xterm.js; no external CDN at runtime.  CSP is
`default-src 'self'` with `connect-src` permitting same-origin WebSockets.

## Rate limiting

Sliding 60-second window per API key, `gateway.rate_limit_per_minute` (default
120).  On exhaustion:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60
{"type":"error","error":{"type":"rate_limit_error","message":"rate limit exceeded (...)"}}
```

The limit is in-memory per process, adequate for a single-node deployment.

Mailbox sends use a separate per-user sliding window,
`messages.rate_limit_per_minute` (default 60), and return the same 429 shape
with `Retry-After`.  Recipients also have an unread cap
(`messages.max_unread_per_session`, default 500).

## CORS

The API sets no CORS headers.  It is for CLI clients, not browsers.  The web UI
is same-origin only.  This also underpins CSRF defence for the web listener.

## Versioning

`v0.4.0`.  The public API mirrors the Anthropic Messages API and tracks the
upstream's behavior; additive response headers and fields are expected.  The
agent/terminal WebSocket protocols are internal and may change between minor
versions; client and server should be upgraded together.
