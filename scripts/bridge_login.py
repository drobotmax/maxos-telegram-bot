"""One-time interactive login for the TG bridge userbot.

Creates the Telethon session file used by src/tg_bridge.py. Run manually
on the VPS from the project root:

    python scripts/bridge_login.py

Prompts for phone number, SMS/app code and 2FA password (if enabled).
Env (falls back to interactive input): BRIDGE_API_ID, BRIDGE_API_HASH,
BRIDGE_SESSION_PATH (default: data/bridge.session).
"""
import asyncio
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient

DEFAULT_SESSION_PATH = "data/bridge.session"


async def main() -> int:
    api_id_raw = os.getenv("BRIDGE_API_ID") or input("BRIDGE_API_ID (from my.telegram.org): ").strip()
    api_hash = os.getenv("BRIDGE_API_HASH") or input("BRIDGE_API_HASH: ").strip()
    session_path = os.getenv("BRIDGE_SESSION_PATH", DEFAULT_SESSION_PATH)

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(f"BRIDGE_API_ID must be an integer, got: {api_id_raw!r}")
        return 1

    Path(session_path).parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session_path, api_id, api_hash)
    # client.start() handles phone, code and 2FA password prompts interactively.
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id={me.id})")
    print(f"Session saved to {session_path}")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
