#!/usr/bin/env bash
# Container entrypoint: create the config on the mounted volume once, then run.
set -euo pipefail

CONFIG="${OPENCLODDIE_CONFIG:-/data/config.json}"
mkdir -p "$(dirname "$CONFIG")"

if [[ ! -f "$CONFIG" ]]; then
  echo "opencloddie: no config at $CONFIG; initialising"
  opencloddie-server init --docker --config "$CONFIG"
fi

exec opencloddie-server "$@"
