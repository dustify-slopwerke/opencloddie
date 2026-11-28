# opencloddie handover — 2026-11-28

This is the working handover for the `opencloddie` session. Read this before
clearing context. It records the current deployment, what was built and fixed,
the traps we hit, and the next steps.

## Current state

- **Production**: https://oc.dustify.com, container `opencloddie-opencloddie-1`,
  image `r.dustify.com/opencloddie:0.6.15`, healthy
  (`/healthz` → `{"ok":true,"version":"0.6.15","mode":"web"}`).
- **Source**: `/home/user/src/opencloddie`, branch `main`, release commit
  `afb6f1d` (`release: opencloddie 0.6.15`), tag `v0.6.15` on both remotes
  (`origin` Gitea and `github`).
- **Release**: GitHub Release `v0.6.15` published with client, wheel and sdist;
  GHCR multi-arch image published; Release, CI and CodeQL green.
- **Compose**: `~/compose/opencloddie/compose.yml` is pinned to
  `r.dustify.com/opencloddie:0.6.15`.
- **Local client**: `~/.local/bin/opencloddie` is 0.6.15. The currently running
  session's runner/agent process was started on an older client; a
  restart/handover picks up 0.6.15 and the session memory cap.
- **Session**: `opencloddie` / `s_6d6e5823821dafeb` on host `orangepi5-plus`,
  cwd `/home/user/src/opencloddie`. Current mode is **chat** (the 19:43 TUI
  handoff was reverted by switching back to Chat); chat history is preserved in
  the hub and permission mode is `auto`.
- **Hub sessions**: `opencloddie` is online. `fa` is **offline** (last event
  seq 74690 at 2026-11-28T00:53:15Z, agent `a-1eb387759511b3d7` on host `sb`);
  its history is intact, but the agent on `sb` must be restarted for it to
  reappear. `admin` and `eve-o-tool` remain absent.

## Important: the “handover” mix-up

“Handover” was interpreted as the project's chat↔TUI mode handoff, and a
detached job ran:

```
POST /api/sessions/s_6d6e5823821dafeb/chat/mode
{"mode":"tui","force":true}
```

That switched the session from chat to the terminal at 19:43 UTC. It was then
switched back to chat. Nothing was lost: the same Claude conversation was
resumed throughout. The intended meaning was this written handover document.
Log: `/tmp/oc-handover-tui.log`. This document was included in the 0.6.13
release commit; this version is the updated copy on `main`.

If you want the web chat back, use the **Chat** toggle in the web UI, or call
the same endpoint with `{"mode":"chat","force":true}` (headers:
`Remote-User: <user>` and `X-OpenCloddie-Client: web`). Do not repeat the
chat→TUI call unless you actually want the terminal.

## What shipped in 0.6.6 – 0.6.15

The work in this session was the native web chat UI and its reliability.

- **0.6.6** — Reload no longer scrolls to an old resolved approval; pending
  badge clears on resolve/expiry; default `chat.permission_timeout_seconds`
  raised 300 → 3600 (hub and client fallback).
- **0.6.7** — The “N pending” chip always opens the approval modal (Allow
  once / Deny, AskUserQuestion and plan forms), instead of only flashing the
  inline card; live permissions reveal their action row.
- **0.6.8** — Refresh rollback fixed. The chat WebSocket `hello` replay was
  sending an older page of events, which the client appended below the newest
  messages. Replay events with `seq <= snapshot.last_seq` are now ignored;
  snapshot rendering catches per-event failures; `loadOlder` prepends via a
  DocumentFragment; auto-paging requires a real reader gesture; reload/scroll
  restoration re-pins. Inline approvals are reconciled over REST. Added the
  “↑ Your last message” jump.
- **0.6.9** — Duplicate/lost streamed text fixed for the CLI's real order
  (stream-level `message_stop` arrives before the durable assistant blocks):
  the stream group stays migratable and only groups with final blocks are
  finalized; interrupted turns keep their partial text. Search/scroll jumps
  set `userScrolled`.
- **0.6.10** — REST pending reconciliation is bounded to ids captured before
  the request, so a live approval created during the fetch is not killed; a
  resolved-id map keeps dead cards from reviving or counting as pending.
- **0.6.11** — Internal harness/subagent instructions (for example
  `[handback-send-enforce] ...`, `[subagent-handback]`, `[task-notification]`)
  are filtered out of user bubbles, checkpoints, the last-user target and the
  jump; the client runner also stops persisting them. Real user messages that
  merely quote the prefix are not filtered (only leading prefixes match).
- **0.6.12** — Jump controls moved from the floating layer into the chat
  header's right side, after status/metrics/“Clear history”: **↑ Last msg** and
  **↓ Latest**. They keep conditional visibility and cannot overlap the
  composer.
- **0.6.13** — Duplicate assistant text fixed for the CLI reporting final
  blocks with `block_index: 0` while deltas use the real content index: the
  UI consumes the oldest unconsumed stream of the same kind, and the runner
  tracks per-message block indices. Mobile image attachments fixed by
  snapshotting the selected `FileList` before clearing the input (it was
  live and became empty).
- **0.6.14** — Flaky kill E2E fixed: `Pty.close()` closed the PTY master twice
  and could close an fd reused by the concurrent `tmux kill-session`
  subprocess (EBADF), so the agent exited while tmux survived. Close is now
  idempotent with fd locking, and `tmux_kill` retries and verifies. Duplicate
  assistant text is suppressed on three paths (repeated final block, visible
  duplicate stream, and the “Turn complete” row repeating the final message).
  Empty assistant groups and orphan subagent containers are pruned, which
  removes the thin empty strip left after a result when subagent output's
  parent Agent card is outside the loaded history. Chat code is collapsed by
  default behind an expandable “language · N lines” header.
- **0.6.15** — Per-session memory cap. The generated pane runner sets
  `ulimit -v` (default 16384 MiB, `memory_limit_mb` /
  `OPENCLODDIE_MEMORY_LIMIT_MB`, `0` disables) so Claude and every tool it
  spawns fail with `MemoryError` instead of exhausting the host. Motivated by
  the 2026-11-28 `fa` OOM: a subagent Python `walk()` without a visited set
  grew to ~118 GB and the kernel OOM tore down the whole `tmux-spawn` scope.
  The runner marker is v3; existing sessions upgrade once on the next
  `opencloddie run` (the session is recreated and Claude resumes).

## Key files

- `opencloddie/web/chat.js` — ChatView controller: event application, scroll
  pinning, approvals, pending reconciliation, jump controls, session cache.
- `opencloddie/web/chat-render.js` — Markdown, tool cards, permission cards,
  question forms, diffs.
- `opencloddie/web/app.js` — app shell, tabs, topbar, session list, modals.
- `opencloddie/web/index.html`, `chat.css`, `style.css`.
- `opencloddie/chat.py` — hub chat broker: event seq/dedupe, permissions,
  approvals, mode handoff, retention.
- `opencloddie/httpd.py` — web/API listeners, auth/CSRF, chat routes.
- `opencloddie/client/opencloddie` — tmux runner, `__chat`, `__agent`, mode
  supervisor, permission MCP shim, internal-message filter.
- `opencloddie/db.py` — schema v4 (`chat_events`, `chat_permissions`,
  `chat_state`, `chat_inputs`, ...).
- `tests/` — `test_chat_db.py`, `test_chat_broker.py`, `test_chat_web.py`,
  `test_chat_client_e2e.py`, `test_agent_e2e.py`, `fake_chat_claude.py`,
  `harness.py`.

## How to run / verify

```
cd /home/user/src/opencloddie
./scripts/test.sh              # 143 tests, 2 skipped; ~95s
# known flaky: test_terminal_roundtrip_and_kill (tmux teardown); rerun first
```

Production deploy (web change):

```
cd /home/user/src/opencloddie
docker build -f deploy/Dockerfile \
  -t r.dustify.com/opencloddie:<version> .
# bump ~/compose/opencloddie/compose.yml image tag
docker compose -f ~/compose/opencloddie/compose.yml up -d --force-recreate
curl -s http://<container-ip>:8787/healthz
curl -fsSL -H 'Remote-User: aj' \
  http://<container-ip>:8787/install/opencloddie \
  -o ~/.local/bin/opencloddie && chmod +x ~/.local/bin/opencloddie
```

Release: bump `opencloddie/__init__.py` and the client `VERSION`, add a
CHANGELOG entry, run the tests, commit, tag `vX.Y.Z`, push to both remotes. The
GitHub Release workflow runs on the tag and publishes GHCR + assets. See
`docs/RELEASE.md`.

## Known issues / next steps

- **Client version**: the running session's runner/agent predates 0.6.15 even
  though the installed binary and web UI are 0.6.15. Restart the session (or
  hand off) to get the client-side fixes and the memory cap. The web UI fixes
  are already live.
- **Session memory cap**: default 16384 MiB, set `memory_limit_mb` in the
  client config or `OPENCLODDIE_MEMORY_LIMIT_MB`, `0` disables. Existing
  sessions pick it up on the next `opencloddie run` (runner v3 upgrade recreates
  the session and resumes Claude). `opencloddie exec` is not capped.
- **Host OOM hardening not applied**: the 2026-11-28 `fa` OOM was caused by a
  subagent Python script (no visited set) inside a `tmux-spawn-*.scope` with
  systemd's default `OOMPolicy=stop`. opencloddie was collateral. Setting
  `DefaultOOMPolicy=continue`/`MemoryMax` on the host scope was explicitly
  deferred; see `docs/TROUBLESHOOTING.md` § Host memory / runaway sessions.
- **Kill E2E**: the former flake (`test_terminal_roundtrip_and_kill`, tmux
  surviving the API kill) is fixed in 0.6.14: `Pty.close()` is idempotent and
  `tmux_kill` verifies with a retry. Re-run the loop if it ever flakes again.
- **Missing/offline sessions**: `admin` and `eve-o-tool` are not registered in
  the hub. `fa` is registered but **offline** (last event 2026-11-28T00:53:15Z,
  agent on host `sb`); its history is intact. Restart the agent on `sb` to
  bring it back.
- **Old internal message rows**: the UI hides internal instructions, but rows
  such as seq 43121 remain in the DB. Clearing chat history (in chat mode) or
  the `/clear` boundary removes them from the visible transcript.
- **Mode**: the session is currently **chat**. To use the TUI, use the
  Terminal toggle or `POST /chat/mode` with `{"mode":"tui","force":true}`.
- **Permissions**: chat is in `auto` mode. The TUI may use its own Claude
  permission settings.

## Where else to look

- `/home/user/.claude/projects/-home-user-src-opencloddie/memory/MEMORY.md`
- `/home/user/.claude/projects/-home-user-src-opencloddie/memory/opencloddie-production.md`
  (deploy history, release tags, current production notes)
- `CHANGELOG.md` (0.6.0–0.6.15)
- `docs/ARCHITECTURE.md`, `docs/API.md`, `docs/CLIENT.md`,
  `docs/CHAT-PROTOCOL.md`, `docs/TROUBLESHOOTING.md`, `docs/RELEASE.md`

## Verification snapshot

- Production `0.6.15` healthy (`/healthz` → `{"ok":true,"version":"0.6.15",
  "mode":"web"}`); served `chat.js` contains `pruneGroup` and
  `resultRepeatsAssistant`, `chat-render.js` contains `chat-code-collapse`, and
  `/install/opencloddie` serves a 0.6.15 client containing
  `DEFAULT_SESSION_MEMORY_LIMIT_MB` and `opencloddie-runner-v3`.
- Kill E2E: `test_terminal_roundtrip_and_kill` passed 60 consecutive local
  runs after the 0.6.14 fix (it used to fail within 2–6).
- Memory cap: generated runner is `bash -n` clean and contains
  `ulimit -v 16777216`; a 3 GB allocation under a 2 GB cap fails with
  `MemoryError`; Claude Code 2.1.332 runs under the 16 GB default.
- Full suite: 147 passed, 2 skipped; Release, CI and CodeQL green for
  `v0.6.15`; GitHub Release has client, wheel and sdist; GHCR multi-arch
  image published.
