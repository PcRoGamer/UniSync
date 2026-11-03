#!/usr/bin/env bash
set -euo pipefail

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

export PYTHONPATH="$DIR/backend${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$DIR/.venv/bin/python" ]; then
    PYTHON_CMD="$DIR/.venv/bin/python"
elif [ -x "$DIR/.venv/bin/python3" ]; then
    PYTHON_CMD="$DIR/.venv/bin/python3"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    PYTHON_CMD="python"
fi

"$PYTHON_CMD" clean_disk.py "$@"
