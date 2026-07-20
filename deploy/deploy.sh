#!/usr/bin/env bash
# MaxOS Telegram Bot — Deploy to VPS (run from Mac)
set -euo pipefail

# --- Configuration ---
VPS_HOST="${VPS_HOST:-}"
VPS_USER="${VPS_USER:-maxos}"
VPS_KEY="${VPS_KEY:-~/.ssh/id_ed25519}"

if [ -z "$VPS_HOST" ]; then
    echo "Usage: VPS_HOST=your-server-ip ./deploy.sh"
    echo "  Optional: VPS_USER=maxos VPS_KEY=~/.ssh/id_ed25519"
    exit 1
fi

SSH_OPTS="-i $VPS_KEY -o StrictHostKeyChecking=accept-new"
SSH_CMD="ssh $SSH_OPTS $VPS_USER@$VPS_HOST"
RSYNC_OPTS="-avz --delete -e \"ssh $SSH_OPTS\""

echo "=== Deploying to $VPS_USER@$VPS_HOST ==="

# 1. Sync bot code
#
# data/ is server-owned runtime state, not code — the bot creates and writes it
# while running, and none of it is in git. Syncing it used to delete bridge.db
# (the TG<->MAX message mapping, absent on the Mac) and overwrite bridge.session
# and memory.db with whatever stale copies the Mac happened to have. Worse, it
# swapped those SQLite files out from under the live process, which is where the
# "disk I/O error" / "readonly database" crashes came from.
#
# So: exclude data/ wholesale. Config that genuinely belongs to the Mac is
# pushed explicitly in step 1b — an allowlist, so a new state file added later
# can't silently end up back under --delete.
echo "[1/5] Syncing bot code..."
rsync -avz --delete \
    --exclude '.venv' \
    --exclude 'data/' \
    --exclude '__pycache__' \
    --exclude '.git' \
    --exclude '.env.bak-*' \
    -e "ssh $SSH_OPTS" \
    ~/maxos-telegram-bot/ \
    "$VPS_USER@$VPS_HOST:~/maxos-telegram-bot/"

# 1b. Config files that are authored on the Mac and belong on the server.
# No --delete here: this pushes named files, it never prunes the directory.
echo "[1b/5] Syncing bot config (chat-rules)..."
rsync -avz \
    -e "ssh $SSH_OPTS" \
    ~/maxos-telegram-bot/data/chat-rules.json \
    "$VPS_USER@$VPS_HOST:~/maxos-telegram-bot/data/chat-rules.json"

# 2. MaxOS is now a git clone on VPS, synced via cron `git pull` every 15min.
# Do NOT rsync — would clobber bot-inbox/ writes from VPS side.
echo "[2/5] Skipping MaxOS rsync (managed by git on VPS)"

# 3. Sync Google Workspace MCP server
echo "[3/5] Syncing Google Workspace MCP..."
rsync -avz --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.git' \
    -e "ssh $SSH_OPTS" \
    ~/.claude/mcp-servers/google-workspace-mcp/ \
    "$VPS_USER@$VPS_HOST:~/.claude/mcp-servers/google-workspace-mcp/"

# 4. Sync Google Workspace credentials
echo "[4/5] Syncing Google credentials..."
rsync -avz \
    -e "ssh $SSH_OPTS" \
    ~/.google_workspace_mcp/credentials/ \
    "$VPS_USER@$VPS_HOST:~/.google_workspace_mcp/credentials/"

# 5. Sync Claude config (CLAUDE.md, settings)
echo "[5/5] Syncing Claude config..."
rsync -avz \
    -e "ssh $SSH_OPTS" \
    ~/.claude/CLAUDE.md \
    "$VPS_USER@$VPS_HOST:~/.claude/CLAUDE.md"

# Install dependencies on VPS
echo "[+] Installing Python dependencies..."
$SSH_CMD "cd ~/maxos-telegram-bot && ~/.local/bin/uv sync"

# Update systemd service file + restart
echo "[+] Updating systemd service and restarting..."
$SSH_CMD "sudo cp ~/maxos-telegram-bot/deploy/maxos-telegram-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart maxos-telegram-bot" 2>/dev/null || \
    echo "  (service not yet installed — run setup-service.sh on VPS)"

echo ""
echo "=== Deploy complete ==="
