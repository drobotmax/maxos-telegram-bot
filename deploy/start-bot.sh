#!/usr/bin/env bash
# Wrapper that starts dbus + gnome-keyring before the bot
# Required for Claude Code CLI to access credentials on headless Linux
set -euo pipefail

# Unlock gnome-keyring with empty password
eval $(echo "" | gnome-keyring-daemon --unlock --components=secrets 2>/dev/null) || true
export GNOME_KEYRING_CONTROL
export SSH_AUTH_SOCK

cd /home/maxos/maxos-telegram-bot
exec /home/maxos/.local/bin/uv run python -m src.main
