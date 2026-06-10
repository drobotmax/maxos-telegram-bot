#!/usr/bin/env bash
# Healthcheck: verify bot is alive and responsive
# Install as cron: */5 * * * * /home/maxos/maxos-telegram-bot/deploy/healthcheck.sh
set -euo pipefail

HEARTBEAT_FILE="/home/maxos/maxos-telegram-bot/data/heartbeat"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
ADMIN_CHAT_ID="${ADMIN_TELEGRAM_ID:-}"
MAX_STALE_SECONDS=1800  # 30 min — alert if no activity for this long
ALERT_LOCKFILE="/tmp/maxos-bot-alert.lock"
OOM_MARKER="/tmp/maxos-bot-oom-marker"

# Load env if available
if [ -f /home/maxos/maxos-telegram-bot/.env ]; then
    export $(grep -v '^#' /home/maxos/maxos-telegram-bot/.env | xargs)
fi
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$BOT_TOKEN}"
ADMIN_CHAT_ID="${ADMIN_TELEGRAM_ID:-$ADMIN_CHAT_ID}"

send_alert() {
    local msg="$1"
    if [ -n "$BOT_TOKEN" ] && [ -n "$ADMIN_CHAT_ID" ]; then
        curl -sf -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${ADMIN_CHAT_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=Markdown" > /dev/null 2>&1 || true
    fi
}

has_problem=false

# Check 1: systemd service running
if ! systemctl is-active --quiet maxos-telegram-bot; then
    if [ ! -f "$ALERT_LOCKFILE" ]; then
        send_alert "⚠️ *Bot DOWN* — maxos-telegram-bot service is not running. Restarting..."
        sudo systemctl restart maxos-telegram-bot
        touch "$ALERT_LOCKFILE"
    fi
    exit 1
fi

# Check 2: heartbeat freshness — DISABLED
# Heartbeat only updates on user messages / scheduled tasks.
# No activity for 30 min is normal (no messages, no jobs running).
# Systemd check (Check 1) is sufficient to detect a dead bot.

# Check 3: OOM — only detect NEW oom events since last check
# Use a marker file to track last-checked dmesg timestamp
cutoff_epoch=0
if [ -f "$OOM_MARKER" ]; then
    cutoff_epoch=$(cat "$OOM_MARKER" 2>/dev/null || echo 0)
fi
now_epoch=$(date +%s)

# Look for OOM events in dmesg, filter by time
oom_line=$(sudo dmesg --time-format iso 2>/dev/null | grep -i "oom.*kill" | tail -1 || true)
if [ -n "$oom_line" ]; then
    # Extract ISO timestamp from dmesg line (format: 2026-02-10T17:38:11,623507+00:00)
    oom_ts=$(echo "$oom_line" | grep -oP '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}' || true)
    if [ -n "$oom_ts" ]; then
        oom_epoch=$(date -d "$oom_ts" +%s 2>/dev/null || echo 0)
        if [ "$oom_epoch" -gt "$cutoff_epoch" ]; then
            if [ ! -f "$ALERT_LOCKFILE" ]; then
                send_alert "⚠️ *OOM detected* — process killed by kernel: $oom_line"
                touch "$ALERT_LOCKFILE"
            fi
            has_problem=true
        fi
    fi
fi
# Update marker to current time
echo "$now_epoch" > "$OOM_MARKER"

# Only clear alert lock when everything is healthy
if [ "$has_problem" = false ]; then
    rm -f "$ALERT_LOCKFILE"
fi
