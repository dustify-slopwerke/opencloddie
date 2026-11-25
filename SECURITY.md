# Security policy

## Supported versions

Security fixes are provided for the latest `0.2.x` release.  Older releases
may be asked to upgrade before a fix is backported.

## Reporting a vulnerability

**Do not open a public issue.**

Use GitHub's private vulnerability reporting:

<https://github.com/dustify-slopwerke/opencloddie/security/advisories/new>

Please include:

- affected version/commit and deployment shape (Docker, source, wheel)
- a description of the issue and its security impact
- reproduction steps or a proof of concept
- any relevant logs, with secrets removed

Do not include real API keys, OpenCode keys, Authelia cookies, or other users'
data.  Use disposable test values.

We aim to acknowledge reports within a few days and to coordinate a fix and
disclosure timeline with you.  Credit is given in the release notes unless you
prefer to remain anonymous.

## Scope

The most security-sensitive areas are:

- gateway authentication and model/auth forwarding (`opencloddie/gateway.py`)
- API-key storage/hashing and OpenCode-key encryption (`crypto.py`, `db.py`)
- trusting the nginx/Authelia identity header (`auth.py`, `httpd.py`)
- CSRF and WebSocket origin checks
- agent/terminal isolation and session ownership (`sessions.py`)
- chat event storage, permission decisions and the local chat socket (`chat.py`)
- path handling and static file serving

Structured chat is a deliberate new data-at-rest surface: conversation events,
tool inputs/outputs and approval decisions are stored in the same plaintext
SQLite database (unlike terminal output, which is never persisted).  Access is
owner-only; `chat.retention_days`, `chat.persist`-style caps and the
delete-history action are the controls.  The permission prompt fails closed: if
the approval MCP tool cannot be loaded, chat mode refuses to start rather than
bypassing approvals.  The `__chat` Unix socket and token are mode 0600 and
local-only.

The project's threat model and hardening checklist are in
[docs/SECURITY.md](docs/SECURITY.md).  Deployment guidance is in
[deploy/README.md](deploy/README.md) and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Out of scope

- Vulnerabilities in OpenCode Go, Claude Code, nginx, Authelia, tmux, or the
  browser; report those upstream.
- Misconfiguration where the internal web listener is published directly or
  `web.dev_user` is left enabled in production.
- Denial of service from an authenticated user exceeding intentionally
  configurable rate/session limits.
- Social engineering or physical access.
