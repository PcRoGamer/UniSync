#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
export PYTHONPATH="$DIR/backend${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$DIR/.venv/bin/python" ]; then
    exec "$DIR/.venv/bin/python" -m unisync.mcp.server
elif [ -x "$DIR/.venv/bin/python3" ]; then
    exec "$DIR/.venv/bin/python3" -m unisync.mcp.server
elif command -v python3 &> /dev/null; then
    exec python3 -m unisync.mcp.server
else
    exec python -m unisync.mcp.server
fi
