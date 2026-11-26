#!/usr/bin/env python3
"""Minimal stand-in for the real `claude` CLI, for chat client E2E tests.

This is intentionally not wired into the unittest suite.  It implements the
flags opencloddie passes for headless chat (`-p`, stream-json input/output,
session ids, partial messages) and echoes each user turn back as an assistant
message plus a result.  It never contacts an inference service.

Usage:
    echo '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}' \
        | python3 tests/fake_chat_claude.py -p --input-format stream-json \
              --output-format stream-json --session-id 00000000-0000-0000-0000-000000000001
"""

import argparse
import json
import os
import subprocess
import sys
import uuid


def emit(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _content_text(message):
    body = message.get("message") or message
    content = body.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _permission_server(configs):
    for path in configs or []:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        server = (data.get("mcpServers") or {}).get("opencloddie_permissions")
        if isinstance(server, dict):
            return server
    return None


def call_permission(args, tool_name, tool_input):
    """Drive the real permission MCP shim; block until the web user decides."""
    server = _permission_server(getattr(args, "mcp_config", None))
    if not server:
        return {"behavior": "deny", "message": "no permission MCP config"}
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (server.get("env") or {}).items()})
    cmd = [str(server.get("command") or sys.executable)]
    cmd += [str(arg) for arg in (server.get("args") or [])]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None

        def send(payload):
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        def read_id(want):
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("permission MCP closed")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == want:
                    return message

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "fake-claude", "version": "1"},
                },
            }
        )
        read_id(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        read_id(2)
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "approval_prompt",
                    "arguments": {
                        "tool_name": tool_name,
                        "input": tool_input,
                        "tool_use_id": "toolu_fake",
                    },
                },
            }
        )
        response = read_id(3)
        for block in (response.get("result") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block.get("text") or "{}")
        return {"behavior": "deny", "message": "empty permission result"}
    except Exception as exc:  # noqa: BLE001 - the fake must fail closed
        return {"behavior": "deny", "message": f"permission bridge error: {exc}"}
    finally:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="claude",
        description="fake claude stream-json endpoint for opencloddie tests",
    )
    parser.add_argument("--version", action="version", version="2.1.332 (fake)")
    parser.add_argument("-p", "--print", action="store_true", dest="print_mode")
    parser.add_argument("--input-format", default="text")
    parser.add_argument("--output-format", default="text")
    parser.add_argument("--session-id")
    parser.add_argument("--resume")
    parser.add_argument("--model", default="fake-model")
    parser.add_argument("--permission-mode", default="default")
    parser.add_argument("--include-partial-messages", action="store_true")
    parser.add_argument("--replay-user-messages", action="store_true")
    parser.add_argument("--forward-subagent-text", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--permission-prompt-tool", action="append", default=[])
    parser.add_argument("--mcp-config", action="append", default=[])
    parser.add_argument("--rewind-files")
    return parser


def main(argv=None):
    args, _unknown = _build_parser().parse_known_args(argv)
    if args.rewind_files:
        print(f"Files rewound to state at message {args.rewind_files}")
        return 0
    session_id = args.session_id or args.resume or str(uuid.uuid4())

    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": args.model,
            "tools": [],
            "mcp_servers": [],
            "permissionMode": args.permission_mode,
            "capabilities": {"interrupt_receipt_v1": True},
        }
    )

    permission_done = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        text = line
        message = None
        if args.input_format == "stream-json":
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                emit(
                    {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "result": "invalid stream-json input",
                        "session_id": session_id,
                    }
                )
                continue
            text = _content_text(message)

        command = text.strip().split(" ", 1)[0] if text.strip() else ""
        if command in ("/clear", "/reset", "/new"):
            # Mirror the real CLI: /clear starts a new session with empty
            # context and reports the reset in the stream.
            previous = session_id
            session_id = str(uuid.uuid4())
            emit(
                {
                    "type": "conversation_reset",
                    "new_conversation_id": session_id,
                    "uuid": session_id,
                    "trigger": "clear",
                    "user_message_uuid": str(uuid.uuid4()),
                    "session_id": previous,
                }
            )
            emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": session_id,
                    "model": args.model,
                    "tools": [],
                    "mcp_servers": [],
                    "permissionMode": args.permission_mode,
                    "capabilities": {"interrupt_receipt_v1": True},
                }
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "",
                    "session_id": session_id,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "total_cost_usd": 0.0,
                    "permission_denials": [],
                    "terminal_reason": None,
                    "local_command": "clear",
                }
            )
            continue

        if (
            message is not None
            and not permission_done
            and (
                os.environ.get("OPENCLODDIE_FAKE_PERMISSION") == "1"
                or os.path.exists(os.path.join(os.getcwd(), ".fake-permission"))
            )
        ):
            permission_done = True
            tool_input = {"command": "echo fake-permission", "description": "fake approval"}
            decision = call_permission(args, "Bash", tool_input)
            if not isinstance(decision, dict) or decision.get("behavior") != "allow":
                emit(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "result": "permission denied: "
                        + str((decision or {}).get("message") or ""),
                        "session_id": session_id,
                    }
                )
                continue
            tool_usage = {"input_tokens": 1, "output_tokens": 1}
            emit(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "message": {
                        "id": "msg_permission",
                        "type": "message",
                        "role": "assistant",
                        "model": args.model,
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_fake",
                                "name": "Bash",
                                "input": tool_input,
                            }
                        ],
                        "stop_reason": "tool_use",
                        "usage": tool_usage,
                    },
                }
            )
            emit(
                {
                    "type": "user",
                    "session_id": session_id,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_fake",
                                "content": "fake tool output",
                                "is_error": False,
                            }
                        ],
                    },
                }
            )

        if args.replay_user_messages and message is not None:
            emit(
                {
                    "type": "user",
                    "message": message.get("message") or message,
                    "isReplay": True,
                    "uuid": str(uuid.uuid4()),
                    "session_id": session_id,
                }
            )

        message_id = "msg_" + uuid.uuid4().hex[:12]
        usage = {"input_tokens": len(text), "output_tokens": len(text)}
        if args.include_partial_messages:
            emit(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "model": args.model,
                            "usage": usage,
                        },
                    },
                }
            )
            emit(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                }
            )
            emit(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {"type": "message_stop"},
                }
            )

        emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": args.model,
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "usage": usage,
                },
            }
        )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": text,
                "session_id": session_id,
                "usage": usage,
                "total_cost_usd": 0.0,
                "permission_denials": [],
                "terminal_reason": "end_turn",
            }
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
