#!/usr/bin/env bash
# MaxOS Telegram Bot — VPS Setup (run once as root)
set -euo pipefail

echo "=== MaxOS Telegram Bot: VPS Setup ==="

# 1. System packages
apt-get update && apt-get upgrade -y
apt-get install -y curl git rsync unzip

# 2. Create bot user
if ! id -u maxos &>/dev/null; then
    useradd -m -s /bin/bash maxos
    echo "Created user: maxos"
else
    echo "User maxos already exists"
fi

# 3. Install uv (Python package manager)
su - maxos -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# 4. Install Node.js 22 (required for Claude Code CLI)
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

# 5. Create directories
su - maxos -c 'mkdir -p ~/maxos-telegram-bot ~/MaxOS ~/.claude/mcp-servers ~/.google_workspace_mcp/credentials'

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Run deploy.sh from your Mac to sync files"
echo "  2. SSH as maxos user and run: claude login"
echo "  3. Add Google Workspace MCP (see deploy/README.md)"
echo "  4. Enable systemd service"
