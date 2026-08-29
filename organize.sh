#!/usr/bin/env bash
# ==============================================================================
# Organize — Jellyfin Media Management Launcher (Linux & macOS)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find Python 3.11+
PYTHON_BIN=""
for cand in python3 python python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON_BIN="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Error: Python 3.11+ is required but was not found on PATH." >&2
    echo "Please install Python 3.11 or newer:" >&2
    echo "  Debian/Ubuntu: sudo apt install -y python3" >&2
    echo "  macOS:         brew install python" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/organize.py" "$@"
