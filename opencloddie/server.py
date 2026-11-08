"""opencloddie server entry point: init, run, and small admin commands."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import ssl
import sys
import threading
from pathlib import Path
from typing import Any

from .config import (
    APP_NAME,
    default_config_path,
    init_config,
    load_config,
    write_config,
)
from .crypto import encrypt_secret
from .models import normalize_model
from .util import ensure_dir, mask_secret

VERSION = "0.1.0"


def _configure_logging(cfg: dict[str, Any]) -> None:
    level_name = str(cfg["server"].get("log_level") or "info").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    try:
        log_dir = Path(cfg["_state_dir"]) / "logs"
        ensure_dir(log_dir)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "server.log", maxBytes=5 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass
    logging.getLogger("opencloddie.access").setLevel(logging.INFO)


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else default_config_path()
    try:
        written = init_config(
            path,
            force=args.force,
            dev=not args.docker,
            public_web_url=args.public_web_url,
            public_api_url=args.public_api_url,
        )
    except FileExistsError as exc:
        print(f"opencloddie: {exc}", file=sys.stderr)
        return 2
    cfg = load_config(written)
    if args.docker:
        cfg["server"]["web_host"] = "0.0.0.0"
        cfg["server"]["api_host"] = "0.0.0.0"
        cfg["server"]["behind_proxy"] = True
        cfg["web"]["dev_user"] = ""
        # Keep mutable state on the mounted volume, next to the config.
        cfg["server"]["state_dir"] = str(path.parent)
        cfg["database"]["path"] = str(path.parent / "opencloddie.db")
        write_config(written, cfg)
    print(f"created config: {written}")
    print(f"state dir:      {cfg['_state_dir']}")
    print(f"database:       {cfg['database']['path']}")
    print()
    print("Next:")
    print(f"  {APP_NAME}-server run --config {written}")
    print("  Add the web port to nginx behind Authelia; publish the API port for Claude Code.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .httpd import App, make_server

    config_path = Path(args.config) if args.config else default_config_path()
    if not config_path.exists() and args.init_if_missing:
        print(f"opencloddie: no config at {config_path}; creating one", file=sys.stderr)
        init_config(config_path, dev=not args.docker)
        cfg = load_config(config_path)
        if args.docker:
            cfg["server"]["web_host"] = "0.0.0.0"
            cfg["server"]["api_host"] = "0.0.0.0"
            cfg["server"]["behind_proxy"] = True
            cfg["web"]["dev_user"] = ""
            cfg["server"]["state_dir"] = str(config_path.parent)
            cfg["database"]["path"] = str(config_path.parent / "opencloddie.db")
            write_config(config_path, cfg)

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"opencloddie: {exc}", file=sys.stderr)
        return 2

    if args.web_host:
        cfg["server"]["web_host"] = args.web_host
    if args.web_port:
        cfg["server"]["web_port"] = args.web_port
    if args.api_host:
        cfg["server"]["api_host"] = args.api_host
    if args.api_port:
        cfg["server"]["api_port"] = args.api_port
    if args.dev_user is not None:
        cfg["web"]["dev_user"] = args.dev_user
    if args.behind_proxy is not None:
        cfg["server"]["behind_proxy"] = args.behind_proxy

    _configure_logging(cfg)
    log = logging.getLogger("opencloddie.server")

    app = App(cfg)
    web_server = make_server(app, "web")
    api_server = make_server(app, "api")

    tls_cert = str(cfg["server"].get("tls_cert") or "")
    tls_key = str(cfg["server"].get("tls_key") or "")
    if tls_cert and tls_key:
        _wrap_tls(web_server, tls_cert, tls_key)
        _wrap_tls(api_server, tls_cert, tls_key)

    threads = [
        threading.Thread(target=web_server.serve_forever, name="web-listener", daemon=True),
        threading.Thread(target=api_server.serve_forever, name="api-listener", daemon=True),
    ]
    for thread in threads:
        thread.start()

    stop = threading.Event()

    def watchdog() -> None:
        while not stop.wait(15):
            try:
                closed = app.hub.watchdog()
                if closed:
                    log.info("closed %d stale agent connection(s)", closed)
            except Exception:  # noqa: BLE001
                log.exception("watchdog error")

    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()

    web_scheme = "https" if tls_cert else "http"
    web_url = f"{web_scheme}://{cfg['server']['web_host']}:{cfg['server']['web_port']}"
    api_url = f"{web_scheme}://{cfg['server']['api_host']}:{cfg['server']['api_port']}"
    print(f"{APP_NAME} {VERSION}")
    print(f"  web (internal, Authelia): {web_url}")
    print(f"  api (public, API keys):   {api_url}")
    print(f"  gateway upstream:         {cfg['gateway']['upstream']}")
    print(f"  models:                   {', '.join(m['id'] for m in cfg['gateway']['models'])}")
    print(f"  database:                 {cfg['database']['path']}")
    if cfg["web"].get("dev_user"):
        print(
            f"  dev auth user:            {cfg['web']['dev_user']} "
            "(header-less requests from loopback)"
        )
    if not cfg["server"].get("behind_proxy") and str(cfg["server"]["web_host"]) not in (
        "127.0.0.1",
        "::1",
        "localhost",
    ):
        print("  WARNING: web listener is not loopback and behind_proxy is false")
    sys.stdout.flush()

    def handle_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %s; shutting down", signum)
        stop.set()
        web_server.shutdown()
        api_server.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)
    finally:
        stop.set()
        try:
            web_server.server_close()
            api_server.server_close()
        finally:
            app.db.close()
    return 0


def _wrap_tls(server: Any, cert: str, key: str) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = context.wrap_socket(server.socket, server_side=True)


def _open_db(cfg: dict[str, Any]):
    from .db import Database

    return Database(cfg["database"]["path"], cfg["secrets"]["master"])


def cmd_user(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db = _open_db(cfg)
    try:
        if args.user_command == "list":
            rows = db.list_users()
            if not rows:
                print("no users yet (users are created on first web login)")
            for row in rows:
                print(
                    f"{row['username']:24} keys={len(db.list_api_keys(row['id'])):3} "
                    f"model={row.get('default_model') or '-':24} "
                    f"opencode_key={'yes' if row.get('opencode_key_enc') else 'no'}"
                )
            return 0
    finally:
        db.close()
    return 2


def cmd_key(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db = _open_db(cfg)
    try:
        models = cfg["gateway"]["models"]
        if args.key_command == "create":
            user = db.user_by_username(args.user)
            if not user:
                user = db.ensure_user(
                    args.user,
                    default_model=normalize_model(models, args.model, cfg["gateway"]["default_model"]),
                )
            if args.opencode_key:
                encrypted = encrypt_secret(args.opencode_key, cfg["secrets"]["master"])
                db.set_opencode_key(user["id"], encrypted, mask_secret(args.opencode_key, 4))
            model = normalize_model(
                models, args.model or user.get("default_model"), cfg["gateway"]["default_model"]
            )
            row, token = db.create_api_key(user["id"], args.name or "CLI key", model)
            print(f"user:   {user['username']}")
            print(f"key id: {row['id']}  model: {row['model']}")
            print(f"token:  {token}")
            print("Store this token now; only its hash is kept.")
            return 0
        if args.key_command == "list":
            user = db.user_by_username(args.user)
            if not user:
                print(f"no such user: {args.user}", file=sys.stderr)
                return 2
            for row in db.list_api_keys(user["id"]):
                state = "revoked" if row.get("revoked_at") else "active"
                print(
                    f"{row['id']:20} {row['name'][:24]:24} {row['prefix']:24} "
                    f"{row['model']:26} {state:8} last_used={row.get('last_used_at') or '-'}"
                )
            return 0
        if args.key_command == "revoke":
            user = db.user_by_username(args.user)
            if not user:
                print(f"no such user: {args.user}", file=sys.stderr)
                return 2
            row = db.update_api_key(user["id"], args.id, revoked=True)
            if not row:
                print("key not found", file=sys.stderr)
                return 2
            print(f"revoked {row['id']}")
            return 0
    finally:
        db.close()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{APP_NAME}-server", description="opencloddie server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="create a config file")
    init.add_argument("--config", help="config path")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    init.add_argument("--docker", action="store_true", help="container defaults (0.0.0.0, behind proxy)")
    init.add_argument("--public-web-url", default="")
    init.add_argument("--public-api-url", default="")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="run the web and API listeners")
    run.add_argument("--config", help="config path")
    run.add_argument("--init-if-missing", action="store_true")
    run.add_argument("--docker", action="store_true", help="container defaults if initialising")
    run.add_argument("--web-host")
    run.add_argument("--web-port", type=int)
    run.add_argument("--api-host")
    run.add_argument("--api-port", type=int)
    run.add_argument("--dev-user", help="header-less loopback identity (local development)")
    run.add_argument(
        "--behind-proxy",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="derive public URLs from X-Forwarded-Proto/Host",
    )
    run.set_defaults(func=cmd_run)

    user = sub.add_parser("user", help="inspect users")
    user.add_argument("--config", help="config path")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_sub.add_parser("list", help="list users")
    user.set_defaults(func=cmd_user)

    key = sub.add_parser("key", help="manage API keys (headless bootstrap)")
    key.add_argument("--config", help="config path")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    create = key_sub.add_parser("create")
    create.add_argument("--user", required=True)
    create.add_argument("--name", default="CLI key")
    create.add_argument("--model")
    create.add_argument("--opencode-key", help="also store this OpenCode key for the user")
    listing = key_sub.add_parser("list")
    listing.add_argument("--user", required=True)
    revoke = key_sub.add_parser("revoke")
    revoke.add_argument("--user", required=True)
    revoke.add_argument("--id", required=True)
    key.set_defaults(func=cmd_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Accept --config anywhere (before or after the subcommand) without making
    # every nested subparser repeat the option.
    config_arg = None
    cleaned: list[str] = []
    index = 0
    while index < len(raw):
        item = raw[index]
        if item == "--config":
            if index + 1 >= len(raw):
                print("opencloddie-server: --config needs a value", file=sys.stderr)
                return 2
            config_arg = raw[index + 1]
            index += 2
            continue
        if item.startswith("--config="):
            config_arg = item.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(item)
        index += 1

    parser = build_parser()
    args = parser.parse_args(cleaned)
    if config_arg:
        args.config = config_arg
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
