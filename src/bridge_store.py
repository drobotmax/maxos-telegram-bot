"""Shared storage and interfaces for the TG<->MAX bridge.

This module is the contract between two bridge components:
  - src/tg_bridge.py   (Telethon userbot: incoming TG -> MAX feed)
  - src/max_bridge_bot.py (second MAX bot: replies in MAX -> outgoing TG)

DB: data/bridge.db (separate from sessions.db to avoid contention).
Both components run in the same asyncio loop inside main.py.

Routing contract:
  - tg_bridge forwards a TG message to MAX, gets back max_message_id (mid),
    then calls save_mapping(mid, tg_chat_id, tg_msg_id).
  - max_bridge_bot, on a MAX update that is a reply to message mid, calls
    get_mapping(mid) -> (tg_chat_id, tg_msg_id) and sends via the
    send_to_tg callback provided by tg_bridge at startup.
  - Non-reply plain text goes to the "active chat" (set via /chat command),
    stored in state key "active_chat" as tg_chat_id.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bridge.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS bridge_map (
                max_msg_id TEXT PRIMARY KEY,
                tg_chat_id INTEGER NOT NULL,
                tg_msg_id  INTEGER NOT NULL,
                tg_chat_title TEXT,
                ts         INTEGER NOT NULL
            )"""
        )
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS bridge_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        _conn.commit()
    return _conn


def save_mapping(max_msg_id: str, tg_chat_id: int, tg_msg_id: int,
                 tg_chat_title: str = "") -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO bridge_map VALUES (?, ?, ?, ?, ?)",
            (str(max_msg_id), tg_chat_id, tg_msg_id, tg_chat_title, int(time.time())),
        )
        conn.commit()


def get_mapping(max_msg_id: str) -> Optional[Tuple[int, int, str]]:
    """Return (tg_chat_id, tg_msg_id, tg_chat_title) or None."""
    with _lock:
        row = _get_conn().execute(
            "SELECT tg_chat_id, tg_msg_id, tg_chat_title FROM bridge_map WHERE max_msg_id = ?",
            (str(max_msg_id),),
        ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def set_state(key: str, value: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("INSERT OR REPLACE INTO bridge_state VALUES (?, ?)", (key, value))
        conn.commit()


def get_state(key: str) -> Optional[str]:
    with _lock:
        row = _get_conn().execute(
            "SELECT value FROM bridge_state WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


def set_active_chat(tg_chat_id: int, title: str) -> None:
    set_state("active_chat", f"{tg_chat_id}|{title}")


def get_active_chat() -> Optional[Tuple[int, str]]:
    raw = get_state("active_chat")
    if not raw:
        return None
    chat_id, _, title = raw.partition("|")
    return int(chat_id), title


def cleanup_old(days: int = 30) -> None:
    cutoff = int(time.time()) - days * 86400
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM bridge_map WHERE ts < ?", (cutoff,))
        conn.commit()
