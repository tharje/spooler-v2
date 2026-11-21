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
# requirements.txt, not a hand-picked list: a hand-picked list silently loses
# imports the code actually needs (aiomqtt for CC2, pywebpush for browser
# push), so those features quietly don't work without Docker.
"$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo ""
echo "=== Starting Spooler ==="
echo "Open http://localhost:8080 in your browser"
echo ""
exec "$VENV/bin/python3" "$DIR/server.py"
