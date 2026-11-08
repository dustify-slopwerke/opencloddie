# Contributing to opencloddie

Thanks for taking the time to contribute.  This project is a small,
standard-library-only Python service plus a standalone client; keeping that
shape is part of the design.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Requirements: Python 3.12+, tmux (for the client and end-to-end tests), and
optionally Docker.

```bash
git clone https://github.com/OWNER/opencloddie.git
cd opencloddie

python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[dev]"

./scripts/test.sh
```

The server needs no third-party runtime packages.  The client is a single file
at `opencloddie/client/opencloddie`; `bin/opencloddie` is a symlink to it so the
repository layout and the installed client stay identical.

Run the server locally:

```bash
opencloddie-server init
opencloddie-server run
```

## Tests

```bash
./scripts/test.sh                 # compile + full suite
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_gateway -v
```

- All tests must pass offline; the gateway tests use an in-process fake
  upstream (`tests/harness.py`).
- Tests that need tmux skip themselves when tmux is unavailable, but CI installs
  it and exercises them.
- New behaviour needs a test.  Bug fixes need a regression test.
- Tests create isolated temp configs, databases, and ephemeral ports; never
  depend on a developer's running server or real OpenCode key.

## Documentation

Documentation is part of the definition of done.  Update the relevant file:

| Change | Update |
| --- | --- |
| user-visible behaviour | `README.md` and/or `docs/CLIENT.md` |
| HTTP/WebSocket protocol | `docs/API.md` |
| architecture / data flow | `docs/ARCHITECTURE.md` |
| security / trust boundary | `docs/SECURITY.md` |
| configuration / deployment | `docs/DEPLOYMENT.md`, `deploy/README.md` |
| dependencies, build, release | `docs/DEVELOPMENT.md`, `docs/RELEASE.md` |
| notable fix/feature | `CHANGELOG.md` |

## Conventions

- **Python standard library only** on the server.  Discuss a new dependency
  before adding one.
- The client must remain standalone: no imports from the `opencloddie` package.
- Never log or return API keys, OpenCode keys, session cookies, or terminal
  content beyond the intended relay.
- Use `textContent`/DOM APIs in the web UI; keep the CSP (`script-src 'self'`)
  working.
- Shared mutable state needs a lock; see `Database`, `AgentHub`, and `Channel`.
- Keep the Docker image runnable as a non-root user with a read-only root
  filesystem and a `/data` volume.

## Pull requests

1. Fork and branch from `main`.
2. Keep the change focused; include tests and docs.
3. Run `./scripts/test.sh` and, for container changes,
   `docker compose up -d --build`.
4. Describe what changed, why, and how you verified it.
5. Do not commit secrets or private hostnames.  Use `cloddie.example.com`,
   `registry.example.com`, and placeholder tokens in examples.

## Security

Do **not** open a public issue for a vulnerability.  Follow
[SECURITY.md](SECURITY.md).

## Releases

Release steps and artifact details are in [docs/RELEASE.md](docs/RELEASE.md).
