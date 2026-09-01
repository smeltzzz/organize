#!/usr/bin/env bash
# ==============================================================================
# Jellyfin One-Shot Library Completer — Bash Orchestrator
# ==============================================================================
# This script orchestrates the Python one-shot completer with additional
# bash-level logic for process management, monitoring, and recovery.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/jellyfin_one_shot.py"

# Default values
LIBRARY=""
NICE=false
DRY_RUN=false
MAX_PASSES=0
LOG_DIR="${SCRIPT_DIR}/logs"
VERBOSE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            LIBRARY="$2"
            shift 2
            ;;
        --nice)
            NICE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --max-passes)
            MAX_PASSES="$2"
            shift 2
            ;;
        --log-dir)
            LOG_DIR="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --source PATH          Path to Jellyfin movie library (required)"
            echo "  --nice                 Lower process priority"
            echo "  --dry-run              Preview mode, no changes written"
            echo "  --max-passes N         Maximum number of passes (0=unlimited)"
            echo "  --log-dir PATH         Directory for logs (default: ./logs)"
            echo "  --verbose              Verbose output"
            echo "  --help                 Show this help"
            echo ""
            echo "Environment variables:"
            echo "  OPENSUBTITLES_API_KEY  OpenSubtitles API key"
            echo "  SUBDL_API_KEY          SubDL API key"
            echo "  MOVIE_STD_TARGET       Library path (alternative to --source)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "$LIBRARY" ]]; then
    echo "ERROR: --source is required"
    echo "Usage: $0 --source /path/to/library [options]"
    exit 1
fi

if [[ ! -d "$LIBRARY" ]]; then
    echo "ERROR: Library directory does not exist: $LIBRARY"
    exit 1
fi

mkdir -p "$LOG_DIR"

echo "=============================================="
echo "Jellyfin One-Shot Library Completer"
echo "=============================================="
echo "Library:   $LIBRARY"
echo "Nice:      $NICE"
echo "Dry Run:   $DRY_RUN"
echo "Max Passes: ${MAX_PASSES:-unlimited}"
echo "Log Dir:   $LOG_DIR"
echo "=============================================="
echo ""

# Build base command
CMD=(python3 "$PYTHON_SCRIPT" --source "$LIBRARY")

if $NICE; then
    CMD+=(--nice)
fi

if $DRY_RUN; then
    CMD+=(--dry-run)
fi

if [[ "$MAX_PASSES" -gt 0 ]]; then
    CMD+=(--max-passes "$MAX_PASSES")
fi

CMD+=(--log-dir "$LOG_DIR")

# Run the Python script with tee logging
echo "Starting one-shot completer..."
echo ""

if $VERBOSE; then
    python3 "${CMD[@]}" 2>&1 | tee -a "${LOG_DIR}/verbose.log"
    EXIT_CODE=${PIPESTATUS[0]}
else
    python3 "${CMD[@]}" 2>&1 | tee -a "${LOG_DIR}/runtime.log"
    EXIT_CODE=${PIPESTATUS[0]}
fi

echo ""
echo "=============================================="
if [[ $EXIT_CODE -eq 0 ]]; then
    echo "SUCCESS: Library completed successfully"
elif [[ $EXIT_CODE -eq 1 ]]; then
    echo "PARTIAL: Library partially completed (see logs)"
elif [[ $EXIT_CODE -eq 130 ]]; then
    echo "INTERRUPTED: Run was interrupted by user"
else
    echo "FAILED: Unexpected exit code $EXIT_CODE"
fi
echo "=============================================="

exit $EXIT_CODE
