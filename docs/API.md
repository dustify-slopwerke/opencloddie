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
{"ok": true, "version": "0.1.0", "mode": "api", "uptime": 42}
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
{"ok":true,"sessions":[{
  "id":"s_9f3a...","name":"work","hostname":"laptop",
  "cwd":"/home/alice/src/api","model":"deepseek-v4.1-flash",
  "online":true,"created_at":"2026-11-07T12:00:00Z",
  "last_seen_at":"2026-11-07T12:34:56Z","last_seen":"12s ago","viewers":1
}]}
```

### `GET /client/sessions/<id>`

Single session in the same shape.

### `DELETE /client/sessions/<id>`

Asks the owning agent to kill its tmux session, then removes the registry row.
Returns `{"ok":true}`.  404 if the id is not owned by the key's user.

## Agent WebSocket (`/agent/ws`)

Used by `opencloddie __agent`.  Auth via the same API key (header or query).
Handshake, then a JSON hello:

```json
{"t":"hello","agent_id":"a-...","version":"0.1.0","hostname":"laptop",
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
 "models":[...],"default_model":"deepseek-v4.1-flash",
 "urls":{"web":"https://cloddie.example.com","api":"https://cloddie.example.com"},
 "install":{"command":"...","login":"..."}}
```

### `GET /api/models`

`{"ok":true,"models":[...]}`.

### `GET /api/stats`

`{"ok":true,"stats":{"api_keys":2,"sessions_online":3,"sessions_total":5,"agents":3,"sessions":3}}`.

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

### `GET /healthz`

`{"ok":true,"version":"0.1.0","mode":"web"}` (no auth).

## Static assets

`/`, `/index.html`, `/app.js`, `/style.css`, `/favicon.svg`, `/vendor/*`.
Vendored xterm.js; no external CDN at runtime.  CSP is `default-src 'self'` with
`connect-src` permitting same-origin WebSockets.

## Rate limiting

Sliding 60-second window per API key, `gateway.rate_limit_per_minute` (default
120).  On exhaustion:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60
{"type":"error","error":{"type":"rate_limit_error","message":"rate limit exceeded (...)"}}
```

The limit is in-memory per process, adequate for a single-node deployment.

## CORS

The API sets no CORS headers.  It is for CLI clients, not browsers.  The web UI
is same-origin only.  This also underpins CSRF defence for the web listener.

## Versioning

`v0.1.0`.  The public API mirrors the Anthropic Messages API and tracks the
upstream's behavior; additive response headers and fields are expected.  The
agent/terminal WebSocket protocols are internal and may change between minor
versions; client and server should be upgraded together.
