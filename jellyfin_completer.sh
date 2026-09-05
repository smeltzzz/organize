#!/usr/bin/env bash
# ==============================================================================
# Jellyfin One-Shot Library Completer — compatibility wrapper
# ==============================================================================
# One line of behaviour: hand the arguments to the Python runner. It owns every
# decision - flag parsing, the pass loop, pacing, reports, logs and exit codes -
# and the legacy --verbose flag is now absorbed there as an explicit no-op, so
# this file has nothing left to decide. It exists only so existing cron and Task
# Scheduler call sites keep working.
#
# Usage:
#   ./jellyfin_completer.sh --source /path/to/movies --nice
#   ./jellyfin_completer.sh --help      # the real, full Python help

set -euo pipefail

exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/jellyfin_one_shot.py" "$@"
