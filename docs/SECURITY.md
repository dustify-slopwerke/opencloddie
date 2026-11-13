# Security

This document describes the trust model, the controls in the code, and the
deployment assumptions you must uphold.

## Trust boundaries

```
Internet
  │
  ├── Claude Code / API clients ── API key ──▶ api listener :8788
  │
  └── Browser ── Authelia session ──▶ nginx ── Remote-User ──▶ web listener :8787
                                                │
opencloddie client ── API key ──▶ /agent/ws ────┘
```

- **The API listener is untrusted-facing.** Every route except `/healthz` and
  `/install` requires an API key.  It performs no cookie authentication and no
  web-identity trust.
- **The web listener is trusted-facing.** It is only safe behind nginx +
  Authelia.  It accepts the identity from `Remote-User` (configurable), and
  falls back to a loopback-only dev user when explicitly configured.
- **The agent WebSocket carries terminal bytes.** Treat a client agent as
  arbitrary code running with that user's permission: it can type into the
  Claude session and display whatever the PTY displays.  API keys are the
  authorization boundary.

## Authentication and authorization

### API keys

- Format `ocd_sk_` + 32 bytes of `secrets.token_urlsafe` (43 chars).
- Stored only as `HMAC-SHA256(master-derived key, "opencloddie-api-key-v1" + token)`,
  indexed for lookup.  The plaintext is returned once at creation.
- `revoked_at` immediately invalidates a key (`find_api_key` filters it).
- Keys are owner-scoped: `/client/*`, session deletion, and gateway requests
  can only touch the key owner's rows.
- `last_used_at` is updated at most every 30s to avoid write amplification.

Accepted transports: `Authorization: Bearer`, `Authorization: <token>`,
`x-api-key`, `x-opencloddie-key`, `api-key`, and `?token=`/`?api_key=`.  Query
tokens are supported for compatibility but are discouraged (they can appear in
proxy logs); the access log redacts them.

### Web identity (Authelia)

nginx's `auth_request` calls Authelia and sets `Remote-User`,
`Remote-Groups`, `Remote-Email`, `Remote-Name` from the response.  The app:

1. Optionally requires `X-OpenCloddie-Proxy-Secret` to equal
   `web.proxy_secret` (constant-time compare).
2. Reads `Remote-User`; if absent, allows `web.dev_user` **only from a
   loopback address**.
3. Validates the username (`[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}`) and upserts a
   user row; groups are stored for future authorization use.

The Statistics view uses the forwarded groups for one authorization decision:
members of `metrics.admin_groups` (default `["god"]`) see every user's key
names, session names, and aggregate usage; everyone else sees only their own.
It never returns API-key tokens, OpenCode keys, message content, or terminal
bytes — only counters, names, masked key prefixes, and process health.

It does **not** implement login, passwords, MFA, or sessions.  Authelia owns
those.  In production set `web.dev_user = ""`.

nginx must not let clients set `Remote-User` themselves.  The provided
`authelia_location.conf` overwrites it via `auth_request_set` + `proxy_set_header`.
The API locations in `deploy/nginx/opencloddie.conf` explicitly blank the
header.  If you write your own nginx config, preserve that.

### CSRF

Authelia authenticates the browser with cookies, so a cross-site request could
otherwise carry a valid identity.  All mutating web API calls
(POST/PUT/PATCH/DELETE under `/api/`) require:

- `X-OpenCloddie-Client: web` — a custom header cannot be set by a cross-site
  HTML form and forces a CORS preflight, which the server never approves;
- `Sec-Fetch-Site`, if present, must be `same-origin` or `none`;
- `Origin`, if present, must match `Host`.

WebSockets additionally require `Origin` to match `Host` when an Origin is
present.

The API listener has no cookie auth, so CSRF does not apply there.

## OpenCode key protection

Users paste their `oc_sk_...` key into the web UI.  It is encrypted at rest and
never returned.

Format:

```
v1.<base64url nonce>.<base64url ciphertext>.<base64url tag>
```

Construction (stdlib only, `opencloddie/crypto.py`):

```
master_key = SHA256(secrets.master)
enc_key    = HMAC-SHA256(master_key, "cloddie-enc-v1")
mac_key    = HMAC-SHA256(master_key, "cloddie-mac-v1")
keystream  = HMAC-SHA256(enc_key, nonce || counter)  for counter = 0,1,...
ciphertext = plaintext XOR keystream
tag        = HMAC-SHA256(mac_key, "v1" || nonce || ciphertext)
```

A fresh 128-bit nonce per encryption; encryption is authenticated (encrypt-
then-MAC) and decryption uses `hmac.compare_digest`.  This is a PRF-based
stream cipher plus a strong MAC, not a substitute for a KMS.  If an attacker
has both the database and `secrets.master`, they can recover OpenCode keys.

`secrets.master` is also the API-key hash pepper.  Rotating it invalidates all
stored OpenCode keys and all API keys.  Back it up with the same care as the
database.

## Transport and proxy

- TLS terminates at nginx.  The container listens on plain HTTP on the internal
  `edge` network and publishes no host ports.
- nginx snippets set HSTS, TLS 1.2/1.3, and modern ciphers.
- `/v1/` disables response buffering and uses long read timeouts so SSE is not
  cut off; `/agent/` and the web terminal use long timeouts for WebSockets.
- `client_max_body_size` is global in the edge config; the app independently
  caps request bodies at `gateway.max_body_bytes` (32 MiB default) and
  WebSocket messages at 16 MiB.

If you run without nginx (local development), keep both listeners on loopback
and use `web.dev_user` deliberately.

## Input handling

- **SQL** uses parameterized statements throughout.
- **Static files**: path components are restricted to a safe character class,
  URL-decoded, resolved, and checked to remain inside `opencloddie/web`.
- **No shell interpolation of user data**: tmux/PTY arguments are lists; the
  generated runner script uses `shlex.quote`.
- **Session IDs** must match `[A-Za-z0-9_-]{1,64}`.  An ID already owned by
  another user is rejected, preventing hijack via registration.
- **Agent metadata** (name, hostname, cwd) is length-limited and NUL-stripped
  before storage; it is rendered with `textContent` in the UI, not HTML.
- **Mailbox messages** are length-limited (`messages.max_body_bytes`),
  NUL-stripped, and scoped to the sender's `user_id`.  The web UI renders them
  with `textContent`; the MCP tool wraps them in explicit delimiters and tells
  Claude to treat them as data, not instructions.  Messages are stored in
  plaintext and should not be used for secrets.
- **Request bodies** are size-limited and must be JSON objects.
- **Upstream URL** is fixed by operator config; no user-controlled URL is
  fetched (no SSRF surface).
- **Browser UI** has a strict CSP (`script-src 'self'`, no inline scripts,
  `frame-ancestors 'none'`), uses `textContent` for dynamic strings, and stores
  only the active tab id in `localStorage`.

## Logging and secrets

- API keys, OpenCode keys, and terminal bytes are never logged.
- Access logs redact `token`, `api_key`, `key`, and `access_token` query values.
- The gateway logs username, key name, model, status, stream flag, and request
  id.  It does not log message content.
- FastAPI-style tracebacks are logged server-side; error responses to clients
  are generic (`internal server error`).
- `config.json` (master secret) is written 0600.  The runner script contains
  the API key and is 0700.

## Rate limiting and resource limits

- Per-API-key sliding window, 120 requests/minute by default, 429 with
  `Retry-After: 60`.
- Per-user mailbox send window, 60 messages/minute by default, 429 with
  `Retry-After`.  Each recipient also has an unread cap
  (`messages.max_unread_per_session`, default 500) so a looping sender cannot
  grow the database without bound.
- Per-message WebSocket limit 16 MiB; per-channel queue bounded at 2048
  messages (a slow viewer is disconnected rather than growing memory).
- HTTP request body 32 MiB; upstream JSON response read cap 64 MiB.
- Upstream connect timeout 30s, read timeout 900s.
- Agent heartbeat timeout 120s; stale agents are closed by a watchdog.
- SQLite WAL + busy timeout 30s.

There is no per-user session quota in v0.2; add one at the API layer if you
expose this to an untrusted multi-tenant audience.  The single-process design
also means the rate limiter is per instance, not distributed.

## Threat model

| Scenario | Control |
| --- | --- |
| Stolen API key | Revoke in UI; per-key model limits blast radius; rate limit; `last_used_at` visibility |
| Key guessing | 256-bit random tokens, hashed at rest |
| Spoof `Remote-User` directly to :8787 | Listener is internal-only; optional proxy secret; dev fallback loopback only |
| Spoof `Remote-User` through nginx | Authelia snippet overwrites it; API locations blank it |
| CSRF from a malicious site | Required custom header + Origin/Sec-Fetch checks; no CORS |
| Cross-site WebSocket hijack | Origin check against Host; Authelia session required |
| Session id squatting | Ownership check on registration; random ids |
| Path traversal / static leak | Whitelist + realpath containment |
| Database theft | OpenCode keys encrypted; API keys hashed; master stored separately |
| Malicious client agent | Can only control its own user's sessions; cannot read other users' data |
| Mailbox prompt injection | Same-account only; messages are data-wrapped in the MCP tool and never auto-executed; rate/unread caps bound loops |
| Mailbox data leak | Every query is scoped by `user_id`; route handlers re-check session ownership |
| Terminal escape injection to browser | xterm.js renders in a canvas/DOM terminal, not arbitrary HTML; CSP |
| XSS via session name/cwd | UI uses `textContent`; CSP blocks inline scripts |
| Upstream SSRF | Fixed operator-configured URL |
| Log leakage | Redaction and no content logging |

## Known limitations

- The at-rest encryption is a custom construction, not a vetted AEAD library.
  It is sound under standard PRF assumptions but should be replaced with
  AES-GCM/ChaCha20-Poly1305 (e.g. `cryptography`) if your threat model includes
  a local attacker with the master secret.
- No audit trail beyond `last_used_at` and server logs.
- Terminal input/output is not end-to-end encrypted beyond TLS; the server sees
  it (it must, to relay).
- A compromised server can read OpenCode keys in memory and terminal streams.
- Authelia is a hard dependency for the web UI.  Do not expose the web listener
  without it.
- The API's `?token=` compatibility path can leak via third-party logs.
- Mailbox messages are stored in plaintext in SQLite and are visible to anyone
  who can read the database (or the master secret's host).  They are
  same-account only and retained for `messages.retention_days` (default 30);
  do not send credentials through the mailbox.

## Hardening checklist

- [ ] Publish only nginx `:443`; never `8787`.
- [ ] `web.dev_user = ""` in production.
- [ ] `web.proxy_secret` set and the matching nginx header enabled.
- [ ] Authelia policy for the host (default deny is in effect elsewhere).
- [ ] TLS certificate valid; HSTS enabled.
- [ ] Back up `secrets.master` separately from the database.
- [ ] Revoke unused API keys; rotate on staff changes.
- [ ] Keep the container rootless/read-only (`deploy/compose.yml` does this).
- [ ] Monitor `server.log` for 401/429 spikes and upstream 5xx.
- [ ] Restrict the `edge` network to trusted containers.
- [ ] Keep Python and the base image updated.
