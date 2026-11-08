#!/usr/bin/env bash
# Install the opencloddie client on a machine that already runs Claude Code.
#
#   ./install-client.sh https://cloddie.example.com
#   curl -fsSL https://cloddie.example.com/install/opencloddie | bash -s -- https://cloddie.example.com
set -euo pipefail

SERVER="${1:-${OPENCLODDIE_SERVER:-}}"
if [[ -z "$SERVER" ]]; then
  echo "usage: $0 https://your.opencloddie.host" >&2
  exit 2
fi
SERVER="${SERVER%/}"

for cmd in python3 curl claude tmux; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "warning: '$cmd' not found on PATH" >&2
  fi
done

mkdir -p "$HOME/.local/bin"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$SERVER/install/opencloddie" -o "$tmp"
install -m 0755 "$tmp" "$HOME/.local/bin/opencloddie"
echo "installed $HOME/.local/bin/opencloddie"
echo
exec "$HOME/.local/bin/opencloddie" login --server "$SERVER"
