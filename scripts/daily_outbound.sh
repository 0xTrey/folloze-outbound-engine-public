#!/usr/bin/env bash
# daily_outbound.sh
#
# Runs the full outbound pipeline once per business day.
# Targets ~50 new enrollments per run:
#   1. sync_accounts   — pull fresh ICP accounts + contacts from Apollo
#   2. reveal_emails   — bulk-reveal email addresses via Apollo bulk_match
#   3. enrich_research — Perplexity + Brave research (only contacts with confirmed email)
#   4. build_payloads  — LLM personalization payloads
#   5. assign_push     — push approved contacts to Smartlead
#
# Scheduled via LaunchAgent: Mon–Fri 7:03 AM primary, 10:07 AM fallback
# To run manually: bash ~/Projects/folloze-outbound-engine/scripts/daily_outbound.sh

set -euo pipefail

# Ensure Homebrew Python and binaries are in PATH (cron has a minimal PATH)
export PATH="/opt/homebrew/opt/python@3.13/libexec/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO="$HOME/Projects/folloze-outbound-engine"
LOG_DIR="$REPO/logs"
LOG_FILE="$LOG_DIR/pipeline_$(date '+%Y-%m-%d').log"
PYTHON_BIN="$REPO/.venv/bin/python3"

mkdir -p "$LOG_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="python3"
fi

PAUSE_MESSAGE=$(
    FOLLOZE_ALLOW_WHEN_PAUSED="${FOLLOZE_ALLOW_WHEN_PAUSED:-}" \
    FOLLOZE_REPO="$REPO" \
    "$PYTHON_BIN" - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["FOLLOZE_REPO"])

from runtime_config import should_skip_for_pause

skip, message = should_skip_for_pause("assign_push")
if skip:
    print(message)
PY
)
if [[ -n "$PAUSE_MESSAGE" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $PAUSE_MESSAGE" >&2
    exit 0
fi

send_notification() {
    local subject="$1"
    local body="$2"
    NOTIFY_SUBJECT="$subject" \
    NOTIFY_BODY="$body" \
    FOLLOZE_REPO="$REPO" \
    "$PYTHON_BIN" - <<'PY' || true
import os
import sys

sys.path.insert(0, os.environ["FOLLOZE_REPO"])

from runtime_config import notify

notify(
    os.environ["NOTIFY_SUBJECT"],
    os.environ["NOTIFY_BODY"],
)
PY
}

# Fallback-run guard: only skip the 10:07 fallback if the earlier run actually
# completed. If a log exists without the completion marker, treat it as a stale
# or failed attempt and allow the fallback to proceed.
if [[ -f "$LOG_FILE" ]] && grep -q "daily_outbound: done" "$LOG_FILE" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] daily_outbound: completed log already exists for today, skipping fallback run." >&2
    exit 0
fi

# Redirect all output to dated log file (stdout + stderr)
exec > >(tee -a "$LOG_FILE") 2>&1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

CURRENT_STEP="startup"

on_failure() {
    local exit_code=$?
    local msg="Step '$CURRENT_STEP' failed (exit $exit_code). Check $LOG_FILE for details."
    log "ERROR: $msg"
    send_notification "Pipeline failure" "$msg"
    "$PYTHON_BIN" "$REPO/main.py" engine_watchdog --job daily_outbound || true
}

trap on_failure ERR

log "========================================="
log "daily_outbound: starting"

CURRENT_STEP="load_env"
# shellcheck disable=SC1091
source "$REPO/scripts/load_env.sh"

cd "$REPO"

CURRENT_STEP="sync_accounts"
log "Step 1: sync_accounts (4 pages = ~100 accounts)"
"$PYTHON_BIN" main.py sync_accounts --pages 4

CURRENT_STEP="reveal_emails"
log "Step 2: reveal_emails"
"$PYTHON_BIN" main.py reveal_emails --batch 200

CURRENT_STEP="enrich_research"
log "Step 3: enrich_research (batch=50)"
"$PYTHON_BIN" main.py enrich_research --batch 50

CURRENT_STEP="build_payloads"
log "Step 4: build_payloads (batch=100)"
"$PYTHON_BIN" main.py build_payloads --batch 100

CURRENT_STEP="assign_push"
log "Step 5: assign_push (batch=100)"
"$PYTHON_BIN" main.py assign_push --batch 100

# Zero-enrollment check: warn if no contacts were enrolled
CURRENT_STEP="zero_enrollment_check"
if grep -Eq "\[assign_push\].*Done\. pushed=0 failed=0\b" "$LOG_FILE" 2>/dev/null; then
    msg="Zero enrollments detected today. Pipeline ran but no contacts were pushed to Smartlead."
    log "WARNING: $msg"
    send_notification "Zero enrollments" "$msg"
fi

log "daily_outbound: done"
log "========================================="
