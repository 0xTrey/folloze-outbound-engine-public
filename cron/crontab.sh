#!/usr/bin/env bash
# Folloze Outbound Engine — system crontab entries
# Install: crontab -e and paste the lines below (or run: crontab cron/crontab.txt)
#
# Logs go to ~/Projects/folloze-outbound-engine/cron/logs/

ENGINE_DIR="$HOME/Projects/folloze-outbound-engine"
LOG_DIR="$ENGINE_DIR/cron/logs"

mkdir -p "$LOG_DIR"

# Pull Smartlead events (replies, bounces, unsubscribes) — hourly at :17
# 17 * * * * cd "$ENGINE_DIR" && FOLLOZE_TRIGGER_SOURCE=cron python main.py pull_events >> "$LOG_DIR/pull_events.log" 2>&1

# Daily pipeline — Mon-Fri 7:03 AM primary, 10:07 AM fallback
# 3 7 * * 1-5 FOLLOZE_TRIGGER_SOURCE=cron bash "$ENGINE_DIR/scripts/daily_outbound.sh" >> "$ENGINE_DIR/logs/pipeline.log" 2>&1
# 7 10 * * 1-5 FOLLOZE_TRIGGER_SOURCE=cron bash "$ENGINE_DIR/scripts/daily_outbound.sh" >> "$ENGINE_DIR/logs/pipeline.log" 2>&1

# Deliverability sync — daily at 2:11 AM (unsubscribes + bounces → suppressions)
# 11 2 * * * cd "$ENGINE_DIR" && FOLLOZE_TRIGGER_SOURCE=cron python main.py deliverability >> "$LOG_DIR/deliverability.log" 2>&1

# Engine watchdog — every 30 minutes, diagnose stale jobs and safely retry only idempotent jobs
# 25,55 * * * * cd "$ENGINE_DIR" && FOLLOZE_TRIGGER_SOURCE=cron python main.py engine_watchdog --auto-retry-safe >> "$ENGINE_DIR/logs/engine_watchdog.log" 2>&1

echo "To install, run: crontab -e and paste the uncommented lines from cron/crontab.txt"
