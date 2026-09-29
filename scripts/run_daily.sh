#!/bin/bash
# Daily pipeline runner for folloze-outbound-engine
#
# Run order:
#   1. deliverability  — sync unsubscribes + bounces BEFORE any new pushes
#   2. enrich_signals  — refresh signals for un-enriched accounts/contacts
#   3. build_payloads  — generate 11-field LLM payloads, QA gate
#   4. assign_push     — push approved contacts to Smartlead (50/day ramp)
#
# Enrollment ramp:
#   --batch 50 means max 50 new contacts enrolled per day.
#   Daily hard cap = 3,000 (enforced inside assign_push against the sends table).
#   At steady state (50 enrollments/day × 5 touches), peak daily volume
#   across all active cohorts stays well under 3,000.
#
# Warmup note:
#   Inboxes cap at 30 sends/day during warmup. With 14 mailboxes, max ~420/day
#   while warming. The 50-contact/day ramp keeps you under that ceiling.
#   Raise --batch once inboxes graduate to full send volume (~6-8 weeks).
#
# To activate: load scripts/launchd.plist into launchd (see that file for instructions).
# Do NOT activate until inbox warmup is complete.

set -euo pipefail

REPO="$HOME/Projects/folloze-outbound-engine"
LOG="$REPO/logs/daily_$(date +%Y-%m-%d).log"
DAILY_BATCH=50       # new contacts to enroll per day
ENRICH_BATCH=50      # accounts to enrich per run
PAYLOAD_BATCH=100    # payload generation batch (larger — LLM can handle more)

mkdir -p "$REPO/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

cd "$REPO"

# Load environment (API keys from Keychain via ~/.zshrc)
source "$HOME/.zshrc" 2>/dev/null || true

log "=== Daily run starting ==="
log "DAILY_BATCH=$DAILY_BATCH | ENRICH_BATCH=$ENRICH_BATCH | PAYLOAD_BATCH=$PAYLOAD_BATCH"

# 1. Sync suppressions FIRST — never re-push someone who unsubscribed or bounced
log "--- Step 1: deliverability sync ---"
python main.py deliverability && log "deliverability OK" || log "deliverability FAILED (non-fatal, continuing)"

# 2. Refresh signals for accounts not enriched in the last 7 days
log "--- Step 2: enrich_signals ---"
python main.py enrich_signals --batch "$ENRICH_BATCH" --stale-days 7 && log "enrich_signals OK" || log "enrich_signals FAILED"

# 3. Generate payloads for contacts that don't have one yet
log "--- Step 3: build_payloads ---"
python main.py build_payloads --batch "$PAYLOAD_BATCH" && log "build_payloads OK" || log "build_payloads FAILED"

# 4. Push approved contacts to Smartlead (enforces daily cap internally)
log "--- Step 4: assign_push ---"
python main.py assign_push --batch "$DAILY_BATCH" && log "assign_push OK" || log "assign_push FAILED"

log "=== Daily run complete ==="
