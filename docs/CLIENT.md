# Client reference

The client is one standalone Python 3.12 file: `opencloddie/client/opencloddie`
(`bin/opencloddie` is a symlink).  It is served
by the API at `/install/opencloddie`, so a machine needs only `curl` and
`python3` to install it (plus tmux and Claude Code to run sessions).

## Install

```bash
mkdir -p ~/.local/bin
curl -fsSL https://cloddie.example.com/install/opencloddie \
  -o ~/.local/bin/opencloddie
chmod +x ~/.local/bin/opencloddie
```

Or use the helper: `scripts/install-client.sh https://cloddie.example.com`.

Check prerequisites:

```bash
command -v python3 claude tmux curl
```

The client itself only uses the standard library.  `tmux` and `claude` are
required when starting a session; `claude` may stay unused if you only use `ls`
or `web`.

## First run

With no config, the first run prompts:

```bash
$ opencloddie
First run: connect opencloddie to your server.
Server URL (e.g. https://cloddie.example.com): https://cloddie.example.com
API key (ocd_sk_...): 
connected to https://cloddie.example.com
  user:   alice
  model:  deepseek-v4.1-flash
  web:    https://cloddie.example.com
```

Non-interactive setup:

```bash
opencloddie login --server https://cloddie.example.com --api-key ocd_sk_...
```

The key is validated with `GET /client/me` before it is saved.  If the account
has no OpenCode key yet, the client warns and prints the web URL where it can be
added; the session still starts, but gateway requests will fail until it is set.

The server URL may be the same origin as the web UI when nginx routes `/client/`
and `/agent/` to the API listener.  Locally, use the API port
(`http://127.0.0.1:8788`).

## Commands

### `opencloddie [run]`

Start or re-attach a session.

```
-n, --name NAME     display name (default: current directory basename)
-C, --cwd DIR       working directory (default: cwd)
    --new           create a new session even if name+cwd match an existing one
    --no-attach     start tmux + agent and return
    -- [claude args] passed to claude on every restart
```

Behaviour:

1. Fetches the account's selected model and context window.
2. Reuses a mapping with the same name and cwd, or creates `s_<hex>`.
3. Creates the tmux session if it is not running, using a 0700 runner script.
4. Starts the agent if its PID is not alive.
5. Waits up to 10s for the server to mark the session online.
6. Attaches unless stdout/stdin is not a TTY or `--no-attach` is given.

### Re-attaching vs resuming

- **Same folder, same name, tmux alive** — the default name is the folder
  basename.  `opencloddie` reuses the local mapping, sees the tmux session is
  running, restarts the agent if needed, and attaches.  It does not create a
  duplicate session.
- **Named session** — if you started with `--name work`, plain `opencloddie`
  in that folder uses the folder-derived name instead.  Re-attach with
  `opencloddie attach work` (or `opencloddie --name work`).
- **tmux gone (reboot, killed, stopped)** — the opencloddie session id is
  reused, so the web session is recreated, but Claude starts a **fresh**
  conversation.  To resume Claude's most recent conversation in that folder:
  `opencloddie -- --continue`.  To resume a specific one:
  `opencloddie --name NAME -- --resume <session-id>`.  Extra Claude arguments
  only take effect when the tmux session is created.
- **Claude exited, tmux at the restart prompt** — attaching shows
  `[claude exited - press Enter to restart]`; Enter starts a fresh
  conversation.  To resume instead, stop and recreate with `--continue`.
- Claude Code's conversation history is per working directory, not per
  opencloddie session name.  If you run several named opencloddie sessions in
  one folder, `--continue` resumes the most recent Claude conversation in that
  folder, which may not be the named session you expected.

`Ctrl-b d` detaches.  The tmux session and agent remain; `opencloddie attach`
returns.  Closing the terminal window without detaching also leaves tmux
running (the client process exits, tmux does not).

### `opencloddie attach [NAME]`

Attach to a local session.  With one session and no name, attaches to it; with
several, prints the list and asks for a name.  Ensures the agent is running.

### `opencloddie ls [--json]`

Lists **all** sessions registered to the API key's account, from every machine:

```
NAME                     ID                   ONLINE  UNREAD  MODEL                      CWD                              HOST
work                     s_9f3a…               yes     2       deepseek-v4.1-flash        /home/alice/project              laptop
```

A `*local` marker identifies sessions in this machine's `sessions.json`.
`UNREAD` is the number of unread mailbox messages addressed to that session.

### `opencloddie msg TARGET TEXT...`

Send a message to another session on the same account.  `TARGET` may be an exact
session id, an exact name, or `name@hostname` when the name is ambiguous.  Inside
a running session the sender is `OPENCLODDIE_SESSION_ID`; outside, the message is
attributed to your account.

```bash
opencloddie msg eve-o-tool "please work up the /v2 endpoint; contract is in docs/api.md"
opencloddie msg eve-o-tool@buildbox "same name, different host"
```

`--json` prints the server response.  An offline target still receives the
message when it next checks its mailbox.

### `opencloddie inbox [--all] [--limit N] [--peek] [--json] [--session ID]`

Read this session's mailbox (unread by default) and mark the returned messages
read unless `--peek`.  `--all` includes read messages.  `--session` overrides
`OPENCLODDIE_SESSION_ID`; without either, the command looks for a local session
matching the current directory.

### Session messaging from Claude

New sessions get an MCP server named `opencloddie` with three tools:

- `list_sessions` — resolve a name, see hosts and unread counts.
- `send_session_message` — send by name/id/`name@hostname`.
- `check_mailbox` — read and acknowledge messages addressed to this session.

So you can say *"talk to the eve-o-tool session to work up X"* in one session,
then *"check your mailbox"* in the other.  Messages are durable and pull-only:
nothing interrupts a working session.  The runner writes the MCP config when the
tmux session is created, so sessions started before this feature need
`opencloddie stop NAME` and `opencloddie run --name NAME -- --continue` to gain
the tools.  The `msg`/`inbox` commands still work inside older sessions through
Claude's Bash tool.

Claude Code may ask you to approve the `mcp__opencloddie__*` tools the first
time they are used; approvals are per session.

### `opencloddie stop TARGET`

Stops a session.  If it is local, kills the tmux session and the agent, removes
the local mapping, then unregisters it.  If it is remote (registered by another
machine under the same account), asks the server to send `kill` to that agent.
`TARGET` can be a name or a session id.

### `opencloddie web [--no-open]`

Prints the web URL and, in an interactive terminal, opens the browser.  The web
UI is behind Authelia; you log in there normally.

### `opencloddie env`

Prints gateway environment variables for the current API key:

```bash
export ANTHROPIC_BASE_URL=https://cloddie.example.com
export ANTHROPIC_AUTH_TOKEN=ocd_sk_...
export ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4.1-flash
export ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4.1-flash
export ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4.1-flash
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
```

Use with `eval "$(opencloddie env)"`.  This exposes the API key to the shell;
prefer `opencloddie exec` or the tmux runner when possible.

### `opencloddie exec [--] [claude args]`

Execs `claude` in the current terminal with the gateway environment, without
tmux and without registering a web session.  Useful for one-off `-p` runs and
for checking that the gateway works:

```bash
opencloddie exec -- -p "summarize this repo"
```

### `opencloddie login` / `logout` / `whoami`

- `login` validates and saves credentials.
- `logout` removes the local config; server-side keys remain until revoked in
  the UI.  Running sessions keep their old API key until restarted.
- `whoami` prints the server, user, key name/id, model, web URL, and whether an
  OpenCode key is configured.

### `opencloddie config [show|path]`

`show` prints the config with the API key redacted; `path` prints the config
path.

### `opencloddie version`

Prints the client version.

## Files and environment

| Path | Purpose |
| --- | --- |
| `~/.config/opencloddie/config.json` | server, API key, web URL (mode 0600) |
| `~/.config/opencloddie/sessions.json` | local name/id/tmux/cwd mappings |
| `~/.cache/opencloddie/runner-<id>.sh` | generated tmux runner (contains the API key, 0700) |
| `~/.local/state/opencloddie/agent-<id>.pid` | agent PID |
| `~/.local/state/opencloddie/agent-<id>.log` | agent log (truncated past 5 MiB) |

`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME` are respected.

Environment overrides:

| Variable | Meaning |
| --- | --- |
| `OPENCLODDIE_CONFIG` | path to config.json; local session bookkeeping goes in the same directory |
| `OPENCLODDIE_SERVER` | server URL override |
| `OPENCLODDIE_API_KEY` | API key override |

## The agent

`opencloddie run` spawns `opencloddie __agent` detached for each session.  It is
an internal command but useful to understand:

- Connects to `<api>/agent/ws` with `Authorization: Bearer <api key>`.
- Sends a hello containing agent id, hostname, platform, and session metadata.
- Opens one `tmux attach-session` PTY per `open` message (one per web viewer),
  forwarding bytes as base64 JSON.
- Handles `in`, `resize`, `close`, `kill`, and `ping/pong`.
- Reconnects with exponential backoff while tmux exists; backs off 60s on auth
  errors.
- Exits when its tmux session has been gone for ~30s, on `kill`, or on SIGTERM.

The agent never receives the OpenCode key; it only carries the opencloddie API
key.  It is safe to kill: the tmux session survives, and the next
`opencloddie run` or `attach` starts a new agent.

## Multiple sessions

Each `--name` creates a separate tmux session, runner, and agent.  Examples:

```bash
cd ~/src/api && opencloddie --name api
cd ~/src/web && opencloddie --name web
opencloddie --name scratch -C /tmp          # in another directory
opencloddie --new --name api                # second session, same name/cwd
opencloddie ls
```

The web UI shows one tab per online session and keeps each terminal connected
while you switch.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `not configured` | run `opencloddie login ...` or set `OPENCLODDIE_SERVER`/`OPENCLODDIE_API_KEY` |
| `cannot reach ...` | server URL wrong, API port not published, nginx not routing `/client` |
| `invalid or revoked opencloddie API key` | create a new key in the UI and `opencloddie login` again |
| `tmux is required` | install tmux (`apt install tmux`, `brew install tmux`) |
| `"claude" not found on PATH` | install Claude Code or set `claude_bin` in config |
| session shows online but no output | attach locally (`opencloddie attach NAME`) and check `agent-<id>.log` |
| agent log says 401 | the key was revoked; log in with a new key |
| web terminal says offline | the client agent is not connected; re-run `opencloddie run --name NAME` on that machine |
| Claude answers with an auth/OpenCode error | add/replace the OpenCode key in **Settings** |
| model in Claude differs from UI | expected: the gateway forces the key's model; `--model` is ignored |
| long output stops after 5 minutes | proxy read timeout; use the provided nginx snippet (900s / 3600s) |

Agent logs:

```bash
tail -f ~/.local/state/opencloddie/agent-*.log
```

## Uninstall

```bash
opencloddie stop <name>            # for each local session
rm -f ~/.local/bin/opencloddie
rm -rf ~/.config/opencloddie ~/.cache/opencloddie ~/.local/state/opencloddie
tmux ls | grep '^ocd-'             # any leftovers can be killed with tmux kill-session
```

Revoke the API key in the web UI as well.
