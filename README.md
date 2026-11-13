# opencloddie

[![CI](https://github.com/dustify-slopwerke/opencloddie/actions/workflows/test.yml/badge.svg)](https://github.com/dustify-slopwerke/opencloddie/actions/workflows/test.yml)
[![Release](https://github.com/dustify-slopwerke/opencloddie/actions/workflows/release.yml/badge.svg)](https://github.com/dustify-slopwerke/opencloddie/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Run Claude Code on any machine, point it at one gateway, and drive every
session from a tabbed web UI.

opencloddie is three things in one small, dependency-free service:

1. **An Anthropic-compatible gateway** (`/v1/messages`) that Claude Code can be
   pointed at.  It authenticates with opencloddie API keys, swaps in the
   account's own OpenCode Go key, and forces the model selected in the web UI.
2. **A session relay**.  The `opencloddie` client starts Claude Code inside a
   local tmux session and ships its terminal to the server, one channel per
   viewer.  Detach with `Ctrl-b d`; the session keeps running and stays in the
   web UI.
3. **A tabbed/tiled web app** (behind nginx + Authelia) to switch between all
   your connected sessions, generate API keys, choose models, manage your
   OpenCode key, and monitor gateway usage, tokens, sessions, and process
   health in a Statistics view (with persisted 1h/6h/24h/7d history).

Sessions can message each other through a durable, same-account mailbox.  Say
*"talk to the eve-o-tool session to work up X"* in one session, then *"check your
mailbox"* in the other; the client gives Claude `send_session_message` and
`check_mailbox` tools, and humans get `opencloddie msg` / `opencloddie inbox`
plus unread badges in the web UI.  Delivery is pull-only, so nothing interrupts
a working session.

No peak/off-peak heuristics: you pick the model, per API key, in the UI.

```
 browser ──HTTPS──▶ nginx ──Authelia──▶ web listener :8787 ─┐
                       │                                    ├─ SQLite + agent hub
 Claude Code ──API key▶│──────▶ api listener :8788 ─────────┘
                       │
 opencloddie client ──WS──▶ agent endpoint (api :8788)
   └─ tmux + Claude Code (local machine)      ▲
                                              └─ web terminal WS (web :8787)
```

## Quick start (local, no Docker)

Requires Python 3.12+, tmux (for the client), and Claude Code.

```bash
# 1. Server
./bin/opencloddie-server init              # writes ~/.config/opencloddie/config.json
./bin/opencloddie-server run               # web :8787, api :8788

# 2. Create an API key for a user (headless bootstrap; the UI can do this too)
TOKEN=$(./bin/opencloddie-server key create \
  --user dev --name local \
  --opencode-key "$(cat ~/.config/opencode-go/api-key)" \
  | awk '/^token:/{print $2}')

# 3. Client
./bin/opencloddie login --server http://127.0.0.1:8788 --api-key "$TOKEN"
./bin/opencloddie                          # starts Claude Code in tmux and attaches

# 4. Web UI
open http://127.0.0.1:8787                 # local dev auto-uses the "dev" identity
```

In local dev the web listener trusts header-less loopback requests as
`web.dev_user` (default `dev`).  Set it to `""` to require a `Remote-User`
header, as in production.

The first web visit should be **Settings → OpenCode key** if you did not pass
`--opencode-key` above, then **API keys → Create key**.

## Deploy with Docker, nginx, and Authelia

See **[deploy/README.md](deploy/README.md)**.  Short version:

```bash
docker compose -f deploy/compose.yml up -d --build
```

Then copy `deploy/nginx/opencloddie.conf` into the edge nginx and add the
Authelia access-control rule.  No host ports are published.  `/v1/`, `/agent/`,
`/client/`, and `/install/` are public (API-key auth); everything else goes
through Authelia, which injects `Remote-User`.

## Install from a release

Each tagged release publishes a multi-arch container image, a Python wheel and
sdist, and the standalone client script.

```bash
# Container (amd64/arm64), loopback-only quick start
docker run -d --name opencloddie \
  -p 127.0.0.1:8787:8787 -p 127.0.0.1:8788:8788 \
  -v opencloddie-data:/data \
  ghcr.io/dustify-slopwerke/opencloddie:0.5.0

# Or build the checked-out source
docker compose up -d --build
```

`compose.yaml` uses a local build.  Put nginx + Authelia in front for anything
public; see [deploy/README.md](deploy/README.md).

Wheel (server; the client is downloaded from a running server):

```bash
python3 -m pip install \
  https://github.com/dustify-slopwerke/opencloddie/releases/download/v0.5.0/opencloddie-0.5.0-py3-none-any.whl
opencloddie-server init
opencloddie-server run
```

GHCR packages are private by default; make the package public in its GitHub
settings before sharing the image.  Release and verification details:
**[docs/RELEASE.md](docs/RELEASE.md)**.

## Client

Install from your server (no package manager, one Python file):

```bash
mkdir -p ~/.local/bin
curl -fsSL https://cloddie.example.com/install/opencloddie \
  -o ~/.local/bin/opencloddie
chmod +x ~/.local/bin/opencloddie
opencloddie login --server https://cloddie.example.com --api-key ocd_sk_...
```

First run with no config prompts for the server URL and API key.

Common commands:

```bash
opencloddie                     # new/reuse a session for the current directory
opencloddie --name work         # named session
opencloddie --no-attach         # start in the background
opencloddie ls                  # sessions registered on the server
opencloddie attach work         # re-attach locally after Ctrl-b d
opencloddie stop work           # kill the tmux session and unregister it
opencloddie web                 # open the web UI
opencloddie env                 # print gateway env for a bare claude
opencloddie exec -- --resume    # run claude in this terminal via the gateway
```

`Ctrl-b d` detaches the local terminal.  The tmux session, Claude Code, and the
agent keep running; the web UI can still type into it.  The agent reconnects
automatically after a server restart.

Full command reference: **[docs/CLIENT.md](docs/CLIENT.md)**.

## Point a bare Claude Code at the gateway

The gateway is first and foremost an Anthropic Messages endpoint:

```bash
export ANTHROPIC_BASE_URL=https://cloddie.example.com
export ANTHROPIC_AUTH_TOKEN=ocd_sk_...      # your opencloddie API key
export ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4.1-flash
export ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4.1-flash
export ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4.1-flash
claude
```

Claude Code appends `/v1/messages` itself, so the base URL has no path.  The
model the gateway actually uses is the one selected for the API key in the web
UI; client-side `--model` is ignored unless
`gateway.allow_client_model_override` is enabled.

`opencloddie env` prints exactly these variables for the current key.

## Models

Models are data in `gateway.models` and are shown in the UI.  Defaults:

| Model | Context | Notes |
| --- | --- | --- |
| `deepseek-v4.1-flash` | 1M | fast, cheap |
| `qwen3.8-flash` | not published | strong coding model |
| `glm-5.3-flash` | 1M | long context |
| `claude-haiku-5-5` | 1M | small, fast |
| `minimax-m3` | not published | alternative |

Each API key pins one model.  Changing the model on a key takes effect on the
next gateway request, no client restart needed.  Context tokens are sent to
the client via `/client/me` and become `CLAUDE_CODE_MAX_CONTEXT_TOKENS`.

## Security model

- **Web listener** (`:8787`) is internal.  It never sees the internet directly;
  nginx + Authelia inject `Remote-User`.  It is not usable without that header
  (outside the loopback dev fallback).
- **API listener** (`:8788`) is public and authenticates `ocd_sk_...` keys.
  Keys are stored as HMAC-SHA256 hashes and shown once.
- **OpenCode keys** are supplied per user in the UI and encrypted at rest with
  a master secret.  The gateway decrypts one only for the request's owner.
- **CSRF**: web mutations require `X-OpenCloddie-Client: web` plus same-origin
  checks; WebSockets validate `Origin`.
- **Rate limiting**: per API key, default 120 requests/minute.
- Secrets never appear in logs; tokens in query strings are redacted.

Details and the threat model: **[docs/SECURITY.md](docs/SECURITY.md)**.

## Testing

```bash
make test          # or ./scripts/test.sh
```

80 tests cover crypto, config, DB, model routing, WebSocket framing, gateway
header/stream behavior, the HTTP APIs, CSRF/auth, and two real end-to-end
flows: client → tmux → agent → server → browser terminal, and the full
`opencloddie` CLI.  Tests run offline against a fake OpenCode upstream; tmux
tests skip themselves if tmux is absent.

See **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** for what each test covers
and how to add more.

## Repository layout

```
bin/opencloddie              symlink to the standalone client
bin/opencloddie-server       server launcher
opencloddie/
  client/opencloddie         standalone client (first-run setup, tmux, agent)
  config.py  crypto.py  db.py  auth.py  models.py
  gateway.py                 Anthropic -> OpenCode Go proxy
  metrics.py                 gateway/session/process counters + history flush
  websocket.py               stdlib RFC 6455 server/client
  sessions.py                agent hub + terminal channels
  httpd.py                   web + API listeners, terminal/agent WebSockets
  server.py                  init/run/admin CLI
  web/                       UI (vanilla JS + vendored xterm.js)
compose.yaml                 local quick-start stack
.github/                     CI, release, CodeQL, Dependabot, issue templates
tests/                       unittest suite + fake upstream harness
deploy/                      Dockerfile, compose, nginx, Authelia notes, systemd
docs/                        architecture, API, client, security, dev, ops
```

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data flow, protocols, schema |
| [deploy/README.md](deploy/README.md) | Docker + nginx + Authelia, operations |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | local/systemd/TLS, config reference |
| [docs/CI-CD.md](docs/CI-CD.md) | daily.sh build/push and edge deployment |
| [docs/CLIENT.md](docs/CLIENT.md) | every client command and config file |
| [docs/API.md](docs/API.md) | HTTP + WebSocket API reference |
| [docs/SECURITY.md](docs/SECURITY.md) | trust boundaries, encryption, hardening |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | tests, CI, release process |
| [docs/RELEASE.md](docs/RELEASE.md) | versioning, GHCR/wheel releases, rollback |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | symptom → cause → fix |

## Status

v0.5.0.  The server is pure Python standard library; the only runtime
requirements are Python 3.12 (server and client) and tmux/Claude Code on the
client.  The API is a streaming pass-through — it does not translate message
formats, so any model OpenCode Go exposes through its Anthropic endpoint works
without code changes.

## Contributing

Contributions are welcome.  See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup, test requirements, conventions, and pull-request checklist.
Please read the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Do not open a public issue for a vulnerability.  Use the private reporting
process in [SECURITY.md](SECURITY.md); the threat model and hardening checklist
are in [docs/SECURITY.md](docs/SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).  The web UI vendors xterm.js under MIT; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

opencloddie is an independent project and is not affiliated with or endorsed by
Anthropic, OpenCode, or the maintainers of Claude Code, OpenCode Go, or tmux.
