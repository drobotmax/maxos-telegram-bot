import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_session_id(chat_id: int) -> str | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def save_session_id(chat_id: int, session_id: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO sessions (chat_id, session_id, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(chat_id)
               DO UPDATE SET session_id = excluded.session_id,
                             updated_at = CURRENT_TIMESTAMP""",
            (chat_id, session_id),
        )
        await db.commit()


async def clear_session(chat_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        await db.commit()
