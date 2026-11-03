#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "========================================================"
echo "  UniSync - Academic LMS & Course Companion"
echo "========================================================"
echo ""

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 could not be found. Please install Python 3.10+."
    exit 1
fi

export PYTHONPATH="$DIR/backend"

if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment (.venv)..."
    python3 -m venv .venv
    source .venv/bin/activate
    echo "[*] Installing dependencies..."
    pip install -r requirements.txt
else
    source .venv/bin/activate
fi

echo "[*] Starting UniSync Local Backend on http://127.0.0.1:8000..."
(sleep 1.5 && {
    if command -v open &> /dev/null; then
        open http://127.0.0.1:8000/
    elif command -v xdg-open &> /dev/null; then
        xdg-open http://127.0.0.1:8000/
    fi
}) &

python3 -m uvicorn unisync.api.main:app --host 127.0.0.1 --port 8000
