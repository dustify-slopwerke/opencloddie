# Development

Documentation and tests are part of the definition of done.  Any behavior
change should update the relevant doc (`README.md` or `docs/`) and come with a
test that fails without the change.

## Layout

```
opencloddie/            server package (stdlib only)
  config.py             defaults, merge, init, env overrides
  crypto.py             secret encryption, API-key hashing
  db.py                 SQLite schema and queries
  auth.py               Remote-User and API-key identity
  models.py             model catalogue helpers
  gateway.py            Anthropic -> OpenCode Go proxy, SSE, fallback
  websocket.py          RFC 6455 server and client framing
  sessions.py           AgentHub, AgentConnection, terminal Channels
  httpd.py              both listeners, routing, terminal/agent WS bridges
  server.py             init/run/user/key CLI, logging, TLS, watchdog
  web/                  index.html, app.js, style.css, vendored xterm

bin/opencloddie         standalone client (duplicated WS/PTY code by design)
bin/opencloddie-server  launcher for the package
tests/                  unittest suite + fake upstream harness
deploy/                 Dockerfile, compose, nginx, systemd, deploy docs
scripts/                test runner, client installer
docs/                   architecture, API, client, security, ops
```

The client intentionally does not import the server package: it is downloaded
as a single file and must run with only the standard library.  When changing
the agent/terminal protocol, update **both** `opencloddie/websocket.py`
(server) and the embedded client in `bin/opencloddie`, and add an end-to-end
test.

## Running tests

```bash
make test                  # compile + full suite
./scripts/test.sh          # same
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_gateway -v       # one module
python3 -m unittest tests.test_gateway.GatewayTest.test_streaming_relays_sse -v
```

Requirements: Python 3.12+, tmux for the two end-to-end tests (they skip if
tmux is absent).  Everything else is offline: the gateway tests use
`tests/harness.py: FakeUpstream`, an in-process HTTP server that records
headers/body and can emit JSON or SSE.

### What the suite covers

| File | Coverage |
| --- | --- |
| `test_crypto.py` | encrypt/decrypt round trip, nonce uniqueness, tamper/wrong-key rejection, API-key shape/hash/prefix |
| `test_config.py` | defaults, init permissions, load paths/state, missing master, overwrite rules |
| `test_db.py` | user upsert, key lifecycle/revocation, session ownership, offline transitions, stats |
| `test_models.py` | catalogue normalization and model/context lookup |
| `test_auth.py` | token extraction precedence, loopback detection |
| `test_websocket.py` | RFC accept key, masked/unmasked frames, fragmentation, interleaved ping/pong, close, oversize |
| `test_gateway.py` | model forcing, header forwarding/auth swap/session mirroring, SSE relay, count_tokens, missing OpenCode key, fallback, x-api-key style, stable affinity id, rate limit, key test |
| `test_api.py` | web auth/401, static/CSP-ish checks, OpenCode key lifecycle, API-key CRUD + CSRF, default model, proxy secret, system/stats, API auth errors, streaming gateway over HTTP, install endpoints |
| `test_agent_e2e.py` | real tmux + real client agent + browser terminal WS; input/output round trip; kill propagation |
| `test_client.py` | `opencloddie login/config/env/whoami/run/ls/stop` against a live test server with a fake Claude binary |

62 tests.  Each test creates isolated temp configs/databases and ephemeral
ports; no fixed ports, no network, no reliance on the developer's state.

### Adding a test

1. Use `tests/harness.py:TestServer` (starts both listeners on port 0 and
   creates users/keys).  For gateway behavior use `FakeUpstream`.
2. Prefer asserting observable protocol behavior (headers, status, frames)
   over internal attributes.
3. Always clean up tmux sessions/processes in `tearDown`.
4. Keep tests deterministic; avoid sleeps longer than needed (use `wait_for`).

## Manual end-to-end recipe

The tests automate this, but it is useful while developing:

```bash
# terminal 1 - server
export OPENCLODDIE_CONFIG=/tmp/ocd/config.json
mkdir -p /tmp/ocd
bin/opencloddie-server init
python3 - <<'PY'
import json; p='/tmp/ocd/config.json'; c=json.load(open(p))
c['server']['web_port']=18787; c['server']['api_port']=18788
c['database']['path']='/tmp/ocd/ocd.db'; json.dump(c,open(p,'w'))
PY
bin/opencloddie-server run

# terminal 2 - a key and a fake claude
export OPENCLODDIE_CONFIG=/tmp/ocd/config.json
TOKEN=$(bin/opencloddie-server key create --user dev --name dev \
  --opencode-key oc_sk_fake | awk '/^token:/{print $2}')
printf '#!/usr/bin/env bash\nexec bash --noprofile --norc\n' > /tmp/ocd/fake
chmod +x /tmp/ocd/fake
export XDG_CONFIG_HOME=/tmp/ocd/xdg XDG_STATE_HOME=/tmp/ocd/state XDG_CACHE_HOME=/tmp/ocd/cache
OPENCLODDIE_CONFIG=/tmp/ocd/xdg/config.json bin/opencloddie login \
  --server http://127.0.0.1:18788 --api-key "$TOKEN"
python3 - <<'PY'
import json; p='/tmp/ocd/xdg/config.json'; c=json.load(open(p))
c['claude_bin']='/tmp/ocd/fake'; json.dump(c,open(p,'w'))
PY
OPENCLODDIE_CONFIG=/tmp/ocd/xdg/config.json bin/opencloddie --name manual --no-attach
OPENCLODDIE_CONFIG=/tmp/ocd/xdg/config.json bin/opencloddie ls
```

Open `http://127.0.0.1:18787`, click the tab, type.  Stop with
`opencloddie stop manual`.

## Conventions

- **Server:** Python 3.12 standard library only.  Do not add a pip dependency
  without a strong reason; the dependency-free property is a feature.
- **Client:** keep `bin/opencloddie` self-contained.  It may duplicate small
  helpers from the server, but not import the package.
- **Threading:** shared state goes behind a lock (`Database.lock`,
  `AgentConnection.lock`, `Channel._lock`, `Gateway._rate_lock`).  Never block
  while holding a lock if a socket write can be slow.
- **Secrets:** never log or return API keys, OpenCode keys, or terminal data.
  Use `mask_secret`/`human_age` helpers rather than ad-hoc formatting.
- **Errors:** public API errors are JSON with a message that tells the user
  what to do next; gateway errors use the Anthropic envelope.
- **Config:** new knobs get a default in `DEFAULT_CONFIG`, validation if
  needed, documentation in `docs/DEPLOYMENT.md`, and an example in
  `config.example.json`.
- **Models:** adding a model is configuration-only
  (`gateway.models`); no code change.  Set `context_tokens` only when the
  upstream publishes a window.
- **HTML/JS:** no framework, no build.  Use DOM APIs and `textContent`; keep the
  CSP (`script-src 'self'`) working.  Vendor new browser files with a version
  and license note in `opencloddie/web/vendor/README.md`.
- **Docs:** update `README.md` for user-visible changes and the matching
  `docs/*.md` for protocol/config changes.

## Database changes

There is no migration framework in v0.1.  `Database._init_schema` runs
`CREATE TABLE IF NOT EXISTS` and stores `schema_version=1` in `meta`.  If you
add a column:

1. bump `SCHEMA_VERSION`,
2. read the stored version and apply idempotent `ALTER TABLE` statements,
3. add a test that opens an old database,
4. document the backup/rollback implications.

## CI

`.github/workflows/test.yml` runs on push/PR:

1. Python 3.12
2. `apt-get install tmux`
3. compile check
4. full unittest suite
5. `docker build -f deploy/Dockerfile .`

Keep CI offline-compatible.  The tests never call the real OpenCode endpoint.

## Release checklist

- [ ] `make test` passes locally.
- [ ] `docker build -f deploy/Dockerfile -t opencloddie:test .` succeeds.
- [ ] Version bumped in `opencloddie/httpd.py`, `opencloddie/server.py`, and
      `bin/opencloddie` (`VERSION`).
- [ ] README and docs updated for any behavior/config change.
- [ ] Deploy/README commands still match the compose/Dockerfile.
- [ ] No secrets, tokens, or personal paths in the diff.
- [ ] Tag and build the image; keep the previous tag for rollback.

## Debugging

- Server log: `<state_dir>/logs/server.log` (rotating) and stderr.
- Client agent log: `~/.local/state/opencloddie/agent-<id>.log`.
- Raise verbosity: `server.log_level = "debug"` and restart.
- Inspect the database read-only:
  `sqlite3 /data/opencloddie.db 'select id,name,online,agent_id from sessions'`.
- Browser: DevTools → Network → WS frames show terminal control JSON; the
  binary frames are the terminal stream.
- Gateway isolation: point `gateway.upstream` at a local fake server and use
  `tests/test_gateway.py` as a template.

## Known gaps / next steps

- Multi-node agent routing.
- Per-user session quotas and usage accounting.
- A proper AEAD library for at-rest secrets.
- Audit log table and metrics endpoint.
- Optional server-hosted sessions for users without a local machine.
- Rotation tooling for `secrets.master` (currently rotate = re-enter keys).
