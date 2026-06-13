#!/bin/bash
# setup.sh – install dependencies and start Spooler

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"

echo "=== Spooler Setup ==="

# Create venv if not present
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment…"
    python3 -m venv "$VENV"
fi

echo "Installing dependencies…"
"$VENV/bin/pip" install --quiet websockets bcrypt

echo ""
echo "=== Starting Spooler ==="
echo "Open http://localhost:8080 in your browser"
echo ""
exec "$VENV/bin/python3" "$DIR/server.py"
