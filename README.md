# MaxOS Telegram Bot

Personal AI-assistant bot: Telegram + MAX messenger frontends, Claude as the brain, plus a userbot bridge that mirrors your Telegram inbox into MAX.

## Features

- **Telegram bot** (python-telegram-bot): chat with Claude, per-chat rules (auto-respond in DMs, @mention-only in groups), user whitelist, rate limiting
- **MAX messenger bot** (dev.max.ru API, long-poll): same Claude flow on a second messenger
- **TG <-> MAX bridge** (Telethon userbot): incoming Telegram messages (DMs + whitelisted groups) are forwarded into MAX with media attachments; replying in MAX sends the answer back to the original Telegram chat. See [BRIDGE-GUIDE.md](BRIDGE-GUIDE.md) for the full architecture
- **Scheduler** (APScheduler): morning check-in, daily report, email/calendar scans, HN/Lobsters digest, content digest
- **Claude worker**: runs Claude Code CLI sessions with memory and per-chat context

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and the [Claude Code CLI](https://claude.com/claude-code).

```bash
uv sync
cp .env.example .env   # fill in tokens
uv run python -m src.main
```

For the bridge, additionally:

```bash
uv run python scripts/bridge_login.py   # one-time Telethon login, creates data/bridge.session
```

Per-chat rules (group ids, contexts) live in `data/chat-rules.json` - see the format in `src/config.py`.

## Deploy

```bash
VPS_HOST=your-server-ip bash deploy/deploy.sh   # rsync + systemd restart
```

`deploy/maxos-telegram-bot.service` is a systemd unit template; `deploy/setup-vps.sh` bootstraps a fresh server.

## Security notes

- `.env`, `data/` (session files, SQLite DBs, logs) are gitignored - never commit them
- The bridge session file (`data/bridge.session`) is full access to your Telegram account; one session = one client, never connect to it from two machines at once
- Bot access is whitelist-only (`ADMIN_TELEGRAM_ID` + `ALLOWED_USER_IDS`)

## Tests

```bash
uv run pytest
```
