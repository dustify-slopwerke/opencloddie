# Deployment guide (beyond the Docker + nginx path)

The canonical production path is **[deploy/README.md](../deploy/README.md)**
(Docker, edge nginx, Authelia).  This page covers local runs, systemd, direct
TLS, and operational details that apply to every install.

## Local development

```bash
python3 bin/opencloddie-server init          # ~/.config/opencloddie/config.json
python3 bin/opencloddie-server run
```

Defaults:

| Listener | Address | Auth |
| --- | --- | --- |
| web | `127.0.0.1:8787` | loopback dev user (`dev`) |
| api | `127.0.0.1:8788` | API keys |

Useful flags:

```bash
opencloddie-server run --web-port 9000 --api-port 9001
opencloddie-server run --dev-user alice
opencloddie-server run --no-behind-proxy
OPENCLODDIE_CONFIG=/path/config.json opencloddie-server run
```

`--dev-user` only applies to loopback clients.  Set `web.dev_user` to `""` to
disable the fallback entirely.

Create a first key without the UI:

```bash
opencloddie-server key create \
  --user alice --name laptop \
  --model deepseek-v4.1-flash \
  --opencode-key "$(cat ~/.config/opencode-go/api-key)"
```

Check state:

```bash
opencloddie-server user list
opencloddie-server key list --user alice
opencloddie-server key revoke --user alice --id k_...
```

## systemd

1. Install the code and the client binary:

   ```bash
   sudo install -d /opt/opencloddie /etc/opencloddie /var/lib/opencloddie
   sudo cp -a opencloddie bin /opt/opencloddie/
   sudo ln -sf /opt/opencloddie/bin/opencloddie-server /usr/local/bin/opencloddie-server
   ```

2. Create the config as the service user, then move it into place:

   ```bash
   sudo useradd --system --home /var/lib/opencloddie --shell /usr/sbin/nologin opencloddie
   sudo -u opencloddie OPENCLODDIE_CONFIG=/etc/opencloddie/config.json \
     opencloddie-server init
   sudo chown -R opencloddie:opencloddie /etc/opencloddie /var/lib/opencloddie
   ```

   Set `server.state_dir` to `/var/lib/opencloddie` and `database.path` to
   `/var/lib/opencloddie/opencloddie.db` in the config.

3. Install the unit:

   ```bash
   sudo cp deploy/systemd/opencloddie.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now opencloddie
   sudo journalctl -u opencloddie -f
   ```

4. Put nginx in front.  Use the same location split as
   `deploy/nginx/opencloddie.conf`: `/v1/`, `/agent/`, `/client/`, `/install/`
   to `127.0.0.1:8788` without auth; everything else to `127.0.0.1:8787` behind
   Authelia.

The unit uses `ProtectSystem=strict` and only allows writes to
`/var/lib/opencloddie`.

## Direct TLS (no proxy)

For a small single-host install you can terminate TLS in the app:

```json
"server": {
  "web_host": "0.0.0.0",
  "web_port": 8787,
  "api_host": "0.0.0.0",
  "api_port": 8788,
  "tls_cert": "/etc/opencloddie/fullchain.pem",
  "tls_key": "/etc/opencloddie/privkey.pem",
  "behind_proxy": false
}
```

Both listeners use the certificate.  You still need something to authenticate
the web UI: direct TLS does **not** replace Authelia.  Either keep 8787 on
loopback behind an authenticating proxy, or do not expose it.

## Configuration

The server merges, in order:

1. built-in defaults (`opencloddie/config.py`)
2. the config file (`OPENCLODDIE_CONFIG` or `~/.config/opencloddie/config.json`)
3. environment overrides
4. `run` CLI flags (`--web-host`, `--web-port`, `--api-host`, `--api-port`,
   `--dev-user`, `--behind-proxy`)

Fields not covered in deploy/README:

| Key | Default | Notes |
| --- | --- | --- |
| `server.log_level` | `info` | `debug` for verbose upstream logs |
| `server.tls_cert` / `tls_key` | empty | direct TLS |
| `web.auth_portal_url` | empty | link shown on the 401 page |
| `web.proxy_secret_header` | `X-OpenCloddie-Proxy-Secret` | |
| `gateway.connect_timeout` | 30 | seconds |
| `gateway.read_timeout` | 900 | upstream SSE idle/read timeout |
| `gateway.max_body_bytes` | 32 MiB | request cap |
| `gateway.allow_client_model_override` | false | honor known model ids from the client |
| `gateway.user_agent` | `opencloddie-gateway/0.1` | used when the client sent none |

Environment overrides: `OPENCLODDIE_MASTER_SECRET`, `OPENCLODDIE_WEB_HOST`,
`OPENCLODDIE_WEB_PORT`, `OPENCLODDIE_API_HOST`, `OPENCLODDIE_API_PORT`,
`OPENCLODDIE_DEV_USER`, `OPENCLODDIE_PUBLIC_WEB_URL`,
`OPENCLODDIE_PUBLIC_API_URL`, `OPENCLODDIE_STATE_DIR`, `OPENCLODDIE_CONFIG`.

## Ports and firewall

| Port | Exposure |
| --- | --- |
| 8787 | **internal only** — nginx or loopback |
| 8788 | public (API key auth) |
| 443 | public, nginx |

Never publish 8787.  The web listener trusts an identity header.  If you must
bind it to a non-loopback address, set `web.proxy_secret` and have the proxy
send the matching header.

## State and backups

| Path | Contents | Backup? |
| --- | --- | --- |
| `config.json` | master secret, proxy secret, settings | yes, securely |
| `opencloddie.db` | users, key hashes, session registry | yes |
| `logs/server.log` | operational log | optional |

Losing `secrets.master` makes all stored OpenCode keys undecryptable and
all API keys invalid.  Keep it out of the database backup if your policy
requires split custody; if you use `OPENCLODDIE_MASTER_SECRET`, the config's
copy is ignored.

SQLite backup while running:

```bash
sqlite3 /var/lib/opencloddie/opencloddie.db ".backup '/backup/opencloddie.db'"
```

## Upgrades

1. Back up config + database.
2. Stop the service (sessions are client-side; tmux keeps Claude alive).
3. Replace the code, run tests (`make test`), start.
4. Agents reconnect within seconds; sessions flip back online.

A server restart does not kill Claude sessions.  An agent restart is harmless;
its tmux session persists.

## Monitoring

- `GET /healthz` on both listeners (the Docker healthcheck does this).
- `server.log`: `gateway user=... model=... status=... stream=...` lines are the
  primary traffic signal.  Look for 401 spikes (bad/revoked keys), 429 (rate
  limits), 502 (upstream connectivity), and 5xx.
- The UI's Settings → Server panel shows version, uptime, upstream, and agent
  counts.
- SQLite `PRAGMA integrity_check` in periodic maintenance.

There are no metrics endpoints in v0.1.  Parse the log or add a metrics
exporter if needed.

## Scaling

Single node only: the agent hub is in-memory.  Options if you outgrow it:

- Increase `gateway.rate_limit_per_minute` and the container's file descriptor
  limits.
- Run separate instances per team, each with its own hostname and volume.
- For a true multi-node setup, replace `AgentHub` with a message bus and move
  session state to Postgres; the web terminal would route to the node holding
  the agent.  That is a deliberate future project, not a configuration toggle.

## Rollback

Keep the previous image/tag and a copy of the database.  The schema is additive
in v0.1; a rollback needs a matching older binary and a database backup taken
before the upgrade.
