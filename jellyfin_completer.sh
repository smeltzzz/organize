#!/usr/bin/env bash
# ==============================================================================
# Jellyfin One-Shot Library Completer — thin compatibility wrapper
# ==============================================================================
# The Python runner (jellyfin_one_shot.py) owns every decision: flag parsing,
# pass loop, pacing, reports, logs and exit codes. This wrapper exists only so
# existing cron / Task-Scheduler call sites keep working; it forwards every
# argument straight through instead of re-parsing the CLI a second time.
#
# The one legacy flag it absorbs is --verbose: the Python runner streams every
# tool's output to the console by default, so the old "verbose" mode is simply
# the default behaviour and the flag is a no-op. Everything else — including
# flags added since this wrapper was written — is handled by Python.
#
# Usage:
#   ./jellyfin_completer.sh --source /path/to/movies --nice
#   ./jellyfin_completer.sh --help      # shows the real, full Python help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

args=()
for arg in "$@"; do
    if [[ "$arg" == "--verbose" ]]; then
        continue
    fi
    args+=("$arg")
done

exec python3 "${SCRIPT_DIR}/jellyfin_one_shot.py" "${args[@]+"${args[@]}"}"
