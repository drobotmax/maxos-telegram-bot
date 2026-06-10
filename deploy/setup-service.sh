#!/usr/bin/env bash
# Install and enable systemd service (run on VPS as root)
set -euo pipefail

echo "=== Installing maxos-telegram-bot service ==="

# Copy service file
cp /home/maxos/maxos-telegram-bot/deploy/maxos-telegram-bot.service \
   /etc/systemd/system/maxos-telegram-bot.service

# Reload and enable
systemctl daemon-reload
systemctl enable maxos-telegram-bot
systemctl start maxos-telegram-bot

echo "Service installed and started."
echo ""
echo "Useful commands:"
echo "  systemctl status maxos-telegram-bot"
echo "  journalctl -u maxos-telegram-bot -f"
echo "  systemctl restart maxos-telegram-bot"
