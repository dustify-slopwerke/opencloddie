# Troubleshooting

## Server startup

**`config not found`**
Run `opencloddie-server init`, or set `OPENCLODDIE_CONFIG` to the right path.
In Docker the entrypoint creates `/data/config.json` automatically.

**`secrets.master is empty`**
The config was hand-edited or truncated.  Re-run `init --force` (this
invalidates stored OpenCode keys/API keys) or restore the master from backup.

**`Address already in use`**
Another process owns 8787/8788, or an old server is still shutting down.  Check
`ss -ltnp | grep -E '8787|8788'`, wait a moment (the signal handler waits for
`serve_forever` to stop), or change `server.web_port`/`api_port`.

**Docker container restarts immediately**
`docker compose logs opencloddie`.  Common causes: `/data` is not writable by
uid 10001 (bind mount), or the config references an unwritable `state_dir`.

## Web UI and Authelia

**`401 Authentication required` page**
The request did not carry `Remote-User`.  Confirm:
- nginx includes `authelia_location.conf` in the `location /` block;
- Authelia has an access-control rule for the hostname (its default policy is
  deny);
- you are hitting nginx, not the web port directly.

**`403 missing or invalid proxy secret`**
`web.proxy_secret` is set in the config but nginx is not sending
`X-OpenCloddie-Proxy-Secret` (or sends a different value).  Set both or clear
the config value.

**Logged in but the UI shows the wrong user**
nginx is forwarding a stale or client-supplied `Remote-User`.  Make sure the
`auth_request_set` lines from `authelia_location.conf` are present and that the
API locations blank the header.  Clear the Authelia session and retry.

**Terminal tab shows “session offline”**
The client agent is not connected.  On the host, check
`opencloddie ls` and `~/.local/state/opencloddie/agent-<id>.log`; re-run
`opencloddie run --name <name>` or `opencloddie attach <name>` to restart the
agent.  The tmux session itself may still be alive.

**Terminal connects but is blank**
Usually a proxy timeout or buffering.  Use the nginx snippet's
`proxy_read_timeout 3600s` for the web location and keep
`proxy_buffering off`.  Reload the tab; the WS reconnect creates a fresh
`tmux attach` and redraws.

**Web UI logs out / 302 loops**
Authelia session expired; log in again.  Long-lived terminal WebSockets stay
open until closed, but new API calls redirect.

## Gateway / Claude Code

**`no OpenCode API key is configured for this account`**
Open the web UI → Settings → OpenCode key and paste `oc_sk_...`.  The error
comes from the gateway and is also visible in Claude Code.

**`invalid or revoked opencloddie API key`**
Create a new key in **API keys** and run
`opencloddie login --server ... --api-key ...` again.  Check that the key was
copied fully (`ocd_sk_` + 43 chars).

**`could not reach OpenCode upstream`**
The server cannot reach `gateway.upstream`.  Test from the container:

```bash
docker compose exec opencloddie python3 - <<'PY'
import urllib.request
print(urllib.request.urlopen("https://opencode.ai", timeout=10).status)
PY
```

Check DNS/proxy/firewall.  `502` is this path; the upstream message is in the
body.

**Upstream 401/403**
The stored OpenCode key is wrong/expired/revoked.  Use **Test** in Settings; a
401/403 means replace the key.

**Upstream 429 / overloaded**
OpenCode rate-limited the request.  Configure `gateway.fallback_model` to retry
once with another model, lower concurrency, or wait.  The gateway relays the
upstream error body.

**Claude Code says the model is unrecognized**
Expected for non-Claude model ids.  Set `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
(the client does this from `/client/me`) or ignore the warning.  The gateway
forces the model configured on the API key regardless of `--model`.

**`MissingSessionID` from upstream**
Your client bypassed the gateway (pointed directly at OpenCode) or stripped
`X-Claude-Code-Session-Id`/`x-opencode-session`.  Point `ANTHROPIC_BASE_URL` at
opencloddie; it mirrors the header.

**Streaming stalls before the first token**
Check nginx `proxy_buffering off`, `proxy_read_timeout`, and that the client is
not behind an intermediary that buffers SSE.  The gateway flushes every chunk.

**`request body too large`**
Raise `gateway.max_body_bytes` (default 32 MiB) and nginx's
`client_max_body_size` if you intentionally send larger contexts.

## Client

**`opencloddie` prompts for login every time**
`OPENCLODDIE_CONFIG` or `XDG_CONFIG_HOME` changes between invocations, or the
config is not writable.  Check `opencloddie config path` and file mode (0600).

**`tmux is required`**
Install tmux.  Server-hosted sessions are not implemented; tmux provides
persistence.

**`could not create tmux session`**
The tmux server may be unhealthy or the session name collides.  Run
`tmux ls`, `tmux kill-server` (if safe), and retry.  The error includes tmux's
stderr.

**`claude` not found in the session**
The runner embeds the absolute path found at start time.  Install Claude Code
for the user running the client, or set `claude_bin` in
`~/.config/opencloddie/config.json` and start a new session.

**Session is online but Claude exits immediately**
Attach locally (`opencloddie attach NAME`) and read the error.  Typical causes:
revoked API key in the runner, missing OpenCode key, or Claude Code asking for
login/trust on first run.

**`opencloddie ls` is empty but a local tmux exists**
The agent has not registered (server unreachable or key invalid).  Check the
agent log; `opencloddie attach NAME` restarts the agent.

**`stop` says no session**
The local mapping was lost; find the tmux name with `tmux ls | grep '^ocd-'`
and `tmux kill-session -t NAME`, then delete the remote row in the web UI.

**Web UI can't reach a session on another machine**
The session is registered by that machine's agent.  If it is offline, the agent
there must reconnect; `stop` from any machine sends `kill` to a live agent.

## Docker / nginx

**502 from nginx**
The container is down or not on the `edge` network.  Check
`docker compose ps`, `docker network inspect edge`, and that the service name
matches `opencloddie` in the nginx upstream variables.

**404 on `/v1/messages`**
nginx is routing the public paths to the web listener.  The `location` blocks for
`/v1/`, `/agent/`, `/client/`, `/install` must come before/alongside `location /`
and proxy to port 8788.  Verify with
`curl -i https://host/v1/models` (should be 401, not 404) and the container
logs.

**WebSocket 400/426**
The proxy is not forwarding `Upgrade`/`Connection`.  `proxy_params.conf` sets
them; make sure the location includes it.

**Authelia prompts for `/v1`**
The API locations wrongly include `authelia_location.conf`.  Remove it; only
`location /` should have `auth_request`.

## Data and recovery

**Lost `secrets.master`**
All stored OpenCode keys become unreadable and all API keys stop matching.
Restore the master from backup, or re-enter each user's OpenCode key and issue
new API keys.

**Database locked**
SQLite waits up to 30s.  If a stray process holds it, stop other opencloddie
instances sharing the same file.  Do not run two servers against one DB.

**Sessions all show offline after restart**
Expected until agents reconnect (seconds).  Agents that were on sleeping
laptops stay offline until their machine wakes and the agent reconnects.

## Useful commands

```bash
# server
docker compose -f deploy/compose.yml ps
docker compose -f deploy/compose.yml logs -f opencloddie
sqlite3 /data/opencloddie.db 'select name,online,last_seen_at from sessions'
curl -s http://127.0.0.1:8788/healthz

# client
opencloddie config path
opencloddie whoami
opencloddie ls
tail -f ~/.local/state/opencloddie/agent-*.log
tmux ls
tmux attach -t <ocd-...>

# gateway smoke test (no Claude)
TOKEN=ocd_sk_...
curl -sS https://host/v1/messages \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"anything","max_tokens":4,"messages":[{"role":"user","content":"hi"}]}'
```
