#!/usr/bin/env bash
# Run the full automated suite.  Requires python3.12+ and tmux for the
# client/agent end-to-end tests (those tests skip themselves if tmux is absent).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== compile =="
python3 -m py_compile opencloddie/*.py bin/opencloddie bin/opencloddie-server

echo "== unit + integration tests =="
exec python3 -m unittest discover -s tests -v "$@"
