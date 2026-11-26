# Structured chat protocol (v1)

Status: Phase 0 validated against Claude Code 2.1.332 on 2026-11-25.  This is
the contract between the `__chat` runner, the `__agent` relay, the hub and the
browser chat UI.  Treat the payloads below as additive: unknown fields and
unknown `t` values must be ignored.

## Phase 0 findings

These were observed with the real `claude` binary pointed at a fake Anthropic
upstream (no inference spend):

| Gate | Result |
| --- | --- |
| G1 multi-turn stream input | `claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages` stays alive across turns; one `result` per turn. |
| G2 permission host | `--permission-prompt-tool` receives `Bash` approvals, `AskUserQuestion` (answers round-trip) and `ExitPlanMode`.  The plan text is in the assistant `tool_use` block; the approval request for `ExitPlanMode` carries an empty `input`. |
| G3 session IDs | `--session-id <uuid>` preassigns; `--resume <uuid>` continues the same conversation. |
| G4 interrupt | SIGINT ends the turn but **exits the process**.  Interrupt = SIGINT + respawn with `--resume` (the turn is marked aborted). |
| G5 MCP configs | Multiple `--mcp-config` flags merge; a separate permission server loads alongside the mailbox server. |
| Replay | `--replay-user-messages` emits `user` events with `isReplay: true` and a `uuid`; the first user message of a turn is a file checkpoint. |
| Capabilities | `system/init.capabilities` includes `interrupt_receipt_v1` etc.  Feature-detect, do not parse the version alone. |

Minimum supported: Claude Code **2.1.300**.  If the permission tool cannot be
loaded, chat mode must refuse to start rather than bypass approvals.

### Headless command

```text
<claude> -p
  --input-format stream-json --output-format stream-json --verbose
  --include-partial-messages --forward-subagent-text --replay-user-messages
  --model <key model>
  --session-id <uuid>            # first launch
  --resume <uuid>                # restart / handoff
  --permission-mode <mode>
  --permission-prompt-tool mcp__opencloddie_permissions__approval_prompt
  --mcp-config <opencloddie mailbox config>
  --mcp-config <opencloddie permission config>
  [--mcp-config <user config> ...]
```

Environment: the existing gateway variables from
`opencloddie/client/opencloddie:gateway_env` plus
`CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1`, `OPENCLODDIE_CHAT=1`,
`OPENCLODDIE_CHAT_SOCKET=<path>`, `OPENCLODDIE_CHAT_TOKEN=<random>`.

## Process and files

```
browser ── WS /api/sessions/<id>/chat/ws ── hub (chat.py)
                                              │  SQLite chat_* tables
                                              │  in-memory viewer queues
                                   WS /agent/ws (existing)
                                              │
client:  __agent  ◄── unix socket ──►  __chat (inside tmux)
                                            │ stdin/stdout JSONL
                                            ▼
                                       claude -p stream-json
                                            └── mcp__opencloddie_permissions__approval_prompt
                                                  └── __mcp --permission-only --chat-socket ...
```

Per-session files under `state_dir()`:

| Path | Purpose |
| --- | --- |
| `mode-<sid>` | `chat` or `tui`; written atomically by the agent before a pane respawn. |
| `chat-launch-<sid>.json` | 0600 launch config: Claude UUID, model, permission mode, resume flag, user `claude_args`, MCP config paths, runner version. |
| `chat-<sid>.sock` | Unix socket, 0600. |
| `chat-<sid>.token` | Shared socket token, 0600. |
| `chat-<sid>.jsonl` | Local ring of durable events for agent reconnect (capped). |
| `runner-<sid>.sh` | Mode supervisor bash script. |
| `chat-<sid>.log` | Runner diagnostics; no conversation payloads at info level. |

The tmux pane runs `bash <runner-<sid>.sh>`.  The runner reads `mode-<sid>` in a
loop: `chat` → `python3 <client> __chat ...`; `tui` → the existing
`claude --model ...` loop (with `--session-id` on first launch and `--resume`
thereafter).  Handoff is: write the mode file, then
`tmux respawn-pane -k -t =<tmux> -c <cwd> bash <runner>`.  The tmux session
(and `__agent`'s `_monitor`) survives.

## Socket protocol (`__chat` ↔ `__agent` / MCP shim)

Newline-delimited JSON.  First line from every client:

```json
{"t":"hello","role":"agent|mcp","token":"<chat token>"}
```

The runner replies `{"t":"hello_ok","run_id":"cr_...","last_local_seq":N,"pending":[...]}`.

Agent → runner:

| `t` | Payload |
| --- | --- |
| `sync` | `{epoch, after}` — replay durable events after runner-local seq. |
| `input` | `{client_msg_id, content}` — write one CLI user message. |
| `interrupt` | `{request_id}` — SIGINT, then respawn with `--resume`. |
| `permission_result` | `{permission_id, result}` — resolve a blocked MCP call. |
| `config` | `{permission_mode, model?, force}` — restart+resume. |
| `ack` | `{up_to_local_seq}` — optional. |

Runner → agent:

| `t` | Payload |
| --- | --- |
| `event` | `{event_id, local_seq, kind, turn_id, payload, created_at}` |
| `delta` | `{turn_id, parent_tool_use_id, message_id, block_index, delta}` (transient) |
| `state` | `{status, model, permission_mode, cost_usd, usage, context_tokens, capabilities, claude_session_id, turn_id}` |
| `permission` | `{permission_id, turn_id, request_id, tool_name, input, suggestions, expires_at}` |
| `permission_resolved` | `{permission_id, status, decision, resolved_by}` |
| `mode` | `{mode, status, message}` |
| `exit` | `{code, reason}` |
| `error` | `{message, retryable}` |

MCP client → runner: `{"t":"permission","token":...,"permission_id":...,"tool_name":...,
"input":...,"suggestions":...}`; runner → MCP client:
`{"t":"permission_result","permission_id":...,"result":{...}}`.

## Agent ↔ hub frames

Sent over the existing `/agent/ws` connection (API key auth).  The server
whitelists `mouse`, `chat` and `chat_version` in `hello.features`.

Agent → hub:

| `t` | Payload |
| --- | --- |
| `chat_hello` | `{run_id, claude_session_id, model, permission_mode, status, claude_version, capabilities, pending:[{permission_id, tool_name, input, suggestions, expires_at}], last_local_seq}` |
| `chat_event` | `{event_id, kind, turn_id, payload, created_at}` |
| `chat_delta` | `{turn_id, parent_tool_use_id, message_id, block_index, delta}` |
| `chat_state` | `{status, turn_id, model, permission_mode, claude_session_id, cost_usd, usage:{input_tokens,output_tokens}, context_tokens, capabilities}` |
| `chat_permission_request` | `{permission_id, turn_id, request_id, tool_name, input, suggestions, expires_at}` |
| `chat_permission_resolved` | `{permission_id, status, decision, resolved_by}` |
| `chat_mode` | `{mode:"chat"\|"tui"\|"handoff", status, message}` |
| `chat_exit` | `{code, reason, claude_session_id}` |
| `chat_error` | `{message, retryable}` |

Hub → agent:

| `t` | Payload |
| --- | --- |
| `chat_input` | `{request_id, client_msg_id, content}` |
| `chat_permission_result` | `{permission_id, result}` |
| `chat_interrupt` | `{request_id}` |
| `chat_handoff` | `{to:"chat"\|"tui", force}` |
| `chat_config` | `{permission_mode, model?, force}` |
| `chat_resync` | `{since_local_seq}` |
| `chat_ack` | `{up_to_local_seq}` |

## Browser ↔ hub

REST (web listener, `Remote-User`, owner-only; mutations need
`X-OpenCloddie-Client: web`):

| Method + path | Purpose |
| --- | --- |
| `GET /api/sessions/<id>/chat?limit=200&after_seq=N&before_seq=N` | State, pending permissions, events (ascending).  `before_seq` pages backwards through stored history; `has_more` reports whether an older visible event remains. |
| `POST /api/sessions/<id>/chat/input` | `{content, client_msg_id}` |
| `POST /api/sessions/<id>/chat/interrupt` | Stop the current turn. |
| `POST /api/sessions/<id>/chat/mode` | `{mode:"chat"\|"tui", force}` |
| `POST /api/sessions/<id>/chat/read` | `{last_seq}` |
| `POST /api/sessions/<id>/chat/permissions/<pid>` | `{behavior:"allow"\|"deny", updated_input?, updated_permissions?, message?}` |
| `DELETE /api/sessions/<id>/chat/history` | Purge opencloddie history (not Claude's transcript). |
| `WS /api/sessions/<id>/chat/ws?after_seq=N` | Replay + live events; strict Origin check. |

WS server → browser:

| `t` | Payload |
| --- | --- |
| `hello` | `{session, chat:{mode,status,run_id,claude_session_id,model,permission_mode,turn_id,last_seq,cost_usd,tokens_in,tokens_out,context_tokens,pending_count,capabilities,unread}, pending:[...], events:[...], last_seq}` |
| `event` | `{seq, session_id, event_id, turn_id, kind, payload, created_at}` |
| `delta` | `{turn_id, parent_tool_use_id, message_id, block_index, delta}` |
| `state` | `{chat:{...}}` |
| `permission` | `{permission:{id,tool_name,input,suggestions,status,created_at,expires_at}}` |
| `permission_resolved` | `{id, status, decision}` |
| `mode` | `{mode, status, message}` |
| `input` | `{client_msg_id, content}` (transient optimistic echo) |
| `error` | `{message, code}` |
| `pong` | `{}` |

WS browser → server:

| `t` | Payload |
| --- | --- |
| `input` | `{client_msg_id, content}` |
| `permission` | `{permission_id, behavior, updated_input?, updated_permissions?, message?}` |
| `interrupt` | `{request_id}` |
| `handoff` | `{to:"chat"\|"tui", force}` |
| `resync` | `{after_seq}` |
| `read` | `{last_seq}` |
| `ping` | `{}` |

## Normalized event kinds

Stored in `chat_events(kind, payload)`; `event_id` dedupes replay.

| `kind` | Payload |
| --- | --- |
| `system_init` | `{session_id, model, tools, mcp_servers, capabilities, permission_mode}` |
| `status` | `{status}` (e.g. compacting, api retry) |
| `message_start` | `{message_id, model, usage}` |
| `assistant_block` | `{message_id, block_index, block:{type:"text"\|"thinking"\|"tool_use",...}, parent_tool_use_id, uuid}` |
| `message_stop` | `{message_id, stop_reason, usage}` |
| `user_message` | `{content, uuid, is_replay, checkpoint}` |
| `tool_result` | `{tool_use_id, content, is_error, parent_tool_use_id, uuid}` (capped) |
| `result` | `{subtype, is_error, result, session_id, usage, total_cost_usd, permission_denials, terminal_reason}` |
| `permission` | `{permission_id, tool_name, input, suggestions, status:"pending"}` |
| `permission_resolved` | `{permission_id, status, decision}` |
| `error` | `{message, retryable}` |
| `handoff` | `{from, to, message}` |
| `conversation_reset` | `{trigger, conversation_id, session_id}` — `/clear` boundary; events at or before it are hidden from reads |
| `checkpoint` | `{uuid, label}` |
| `rate_limit` | the CLI rate-limit info object |
| `compact_boundary` | `{trigger, pre_tokens}` |
| `prompt_suggestion` | `{suggestion}` |
| `rewind` | `{checkpoint_uuid, ok, message}` |

`stream_event` deltas are never stored; they are forwarded as `delta` frames
and reconciled by the next `assistant_block`.

## Phase 2 additions

### Images

`POST /api/sessions/<id>/chat/input` and the WS `input` frame accept a
content-block array as well as text.  Images use the Anthropic block shape:

```json
{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<base64>"}}
```

Limits (server-side, configurable): `chat.max_image_bytes` per image (default
5 MiB decoded), `chat.max_input_images` (4), and the existing input cap.  The
hub forwards the blocks unchanged to the agent, which writes them to Claude's
stdin.  The normalized `user_message` event keeps images up to
`chat.max_inline_event_image_bytes` (default 1 MiB) inline so the transcript can
render them; larger images are replaced by
`{"type":"image","placeholder":true,"media_type":...,"bytes":N}`.  The browser
renders inline images as `data:` URLs (CSP already allows `img-src 'self' data:`)
and shows a chip for placeholders.

### Search

`GET /api/sessions/<id>/chat/search?q=<text>&limit=50` (owner-only).  Returns
`{ok, query, events:[...], has_more}` with the same public event shape, matching
case-insensitively against stored payload text.  Plain `LIKE` is sufficient at
this scale; no FTS table.

### Checkpoints and rewind

The runner emits a `checkpoint` event for the first user message of each turn
(the `--replay-user-messages` UUID).  The hub also records it in
`chat_checkpoints(session_id, checkpoint_uuid, event_seq)`.

`POST /api/sessions/<id>/chat/rewind` body `{checkpoint_uuid}` → hub sends
`{"t":"chat_rewind","checkpoint_uuid":...}` to the agent.  The runner pauses
the live child (the session cannot be open twice), runs
`claude -p --resume <claude_session_id> --rewind-files <uuid>` with
`CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1`, emits a `rewind` event
(`{checkpoint_uuid, ok, message}`), then respawns the chat child with `--resume`.
Files are restored; the conversation is not rewound.  Refuse while a turn is
running unless `force` is set (then interrupt first).

## Implementation notes (client 0.6)

- The supervisor script is written to `cache_dir()/runner-<sid>.sh` and mirrored
  to `state_dir()/runner-<sid>.sh` for older clients.  The agent passes the
  absolute launch/config paths so the pane does not depend on the tmux server's
  environment.
- `permission` / `permission_resolved` travel as runner socket frames and
  `chat_permission_*` agent frames; the hub is what persists them as
  `perm:<id>:request/resolved` events, so the UI sees one card per approval.
- `hello_ok` carries additive `model`, `permission_mode`, `status`,
  `capabilities`, `claude_session_id`, `claude_version` and `resolved`
  (recently resolved permissions).  A runner that reconnects after being
  offline reports pending approvals, and the agent replays its bounded local
  ring; the hub dedupes by `event_id`.
- The mode file is written by both the runner (at handoff) and the agent to
  avoid a supervisor race.  `tmux respawn-pane` targets
  `=<tmux>:0.0`; tmux rejects a bare `=<tmux>` target.
- `opencloddie run --chat` prints `<web>/#session=<id>`.  The web UI opens that
  session (and single layout) on boot and on `hashchange`.
- `chat_resync` is accepted by the agent but the hub currently relies on the
  runner's full-ring replay after `hello`; narrowing it is optional.
- Interrupt and user-triggered config restarts bypass the 3-in-5-min crash
  cap; only unexpected exits count toward it.
