import aiosqlite
import logging
import math
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent.parent / "data" / "memory.db"
logger = logging.getLogger(__name__)

# Decay parameters
DECAY_RATE = 0.95          # multiplier per day (5% decay/day)
DECAY_THRESHOLD = 0.05     # records below this relevance get deleted
DECAY_MAX_AGE_DAYS = 90    # hard cap — delete anything older than this
FREQ_BOOST_PER_ACCESS = 0.1   # +0.1 to score per retrieval
FREQ_BOOST_MAX = 2.0          # cap frequency boost at 2.0


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                session_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_conv_chat ON conversations(chat_id);
            CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversations(timestamp);

            CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts
                USING fts5(content, content='conversations', content_rowid='id');

            CREATE TRIGGER IF NOT EXISTS conversations_ai AFTER INSERT ON conversations BEGIN
                INSERT INTO conversations_fts(rowid, content) VALUES (new.id, new.content);
            END;

            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                msg_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        # Add columns if missing (for decay scoring + frequency boost)
        for col, coltype in [("last_accessed", "TEXT"), ("access_count", "INTEGER DEFAULT 0")]:
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # column already exists
        await db.commit()


def _decay_score(timestamp_str: str, last_accessed_str: str | None = None,
                  access_count: int = 0) -> float:
    """Calculate relevance score: exponential decay + frequency boost.

    Formula (inspired by Park et al. "Generative Agents"):
        score = decay_component + frequency_boost

    Decay component:
    - relevance = DECAY_RATE ^ days_since_last_access
    - After 14 days unused: 0.95^14 = 0.49
    - After 60 days unused: 0.95^60 = 0.05 (threshold)

    Frequency boost (new):
    - +0.1 per retrieval, capped at 2.0
    - A record accessed 10 times gets +1.0 to score
    - This means frequently-used memories resist decay longer
    - Example: 10 accesses + 30 days old = 0.21 + 1.0 = 1.21 (alive)
    -          0 accesses + 30 days old = 0.21 + 0.0 = 0.21 (dying)
    """
    now = datetime.utcnow()
    ref_str = last_accessed_str or timestamp_str
    try:
        ref = datetime.fromisoformat(ref_str)
    except (ValueError, TypeError):
        ref = now
    days = max(0, (now - ref).total_seconds() / 86400)
    decay = math.pow(DECAY_RATE, days)
    freq_boost = min(access_count * FREQ_BOOST_PER_ACCESS, FREQ_BOOST_MAX)
    return decay + freq_boost


async def save_exchange(chat_id: int, user_msg: str, bot_response: str, session_id: str | None = None):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO conversations (chat_id, role, content, timestamp, session_id, last_accessed) VALUES (?, 'user', ?, ?, ?, ?)",
            (chat_id, user_msg, now, session_id, now),
        )
        await db.execute(
            "INSERT INTO conversations (chat_id, role, content, timestamp, session_id, last_accessed) VALUES (?, 'assistant', ?, ?, ?, ?)",
            (chat_id, bot_response, now, session_id, now),
        )
        await db.commit()


async def search_relevant(query: str, limit: int = 3) -> list[dict]:
    """FTS5 search with decay-aware ranking.

    Results are ranked by FTS5 relevance * decay_score, so recent
    and frequently-accessed memories rank higher than stale ones.
    Accessed records get their last_accessed timestamp refreshed.
    """
    safe_query = " OR ".join(
        f'"{w}"' for w in query.split() if len(w) > 2
    )
    if not safe_query:
        return []
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Fetch more than needed, then re-rank with decay + frequency
        rows = await db.execute_fetchall(
            """SELECT c.id, c.chat_id, c.role, c.content, c.timestamp,
                      c.last_accessed, c.access_count
               FROM conversations_fts f
               JOIN conversations c ON f.rowid = c.id
               WHERE conversations_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (safe_query, limit * 3),
        )
        results = []
        for r in rows:
            d = dict(r)
            ac = d.get("access_count") or 0
            d["decay_score"] = _decay_score(
                d["timestamp"], d.get("last_accessed"), ac
            )
            results.append(d)

        # Re-rank by combined score (higher = more relevant)
        results.sort(key=lambda x: x["decay_score"], reverse=True)
        results = results[:limit]

        # Touch last_accessed + increment access_count for returned records
        now = datetime.utcnow().isoformat()
        for r in results:
            await db.execute(
                "UPDATE conversations SET last_accessed = ?, access_count = COALESCE(access_count, 0) + 1 WHERE id = ?",
                (now, r["id"]),
            )
        await db.commit()

        return results


async def get_recent(chat_id: int, limit: int = 5) -> list[dict]:
    """Get last N exchanges for a specific chat."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT role, content, timestamp FROM conversations
               WHERE chat_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (chat_id, limit * 2),
        )
        return [dict(r) for r in reversed(rows)]


async def save_summary(chat_id: int, summary: str, msg_count: int = 0):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO summaries (chat_id, summary, msg_count, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, summary, msg_count, now),
        )
        await db.commit()


async def get_summaries(chat_id: int, limit: int = 2) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT summary, msg_count, created_at FROM summaries
               WHERE chat_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (chat_id, limit),
        )
        return [dict(r) for r in reversed(rows)]


async def count_since_last_summary(chat_id: int) -> int:
    """Count messages since last summary (for compaction trigger)."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        row = await db.execute_fetchall(
            "SELECT MAX(created_at) FROM summaries WHERE chat_id = ?",
            (chat_id,),
        )
        last_summary_ts = row[0][0] if row and row[0][0] else "1970-01-01"
        count_row = await db.execute_fetchall(
            "SELECT COUNT(*) FROM conversations WHERE chat_id = ? AND timestamp > ?",
            (chat_id, last_summary_ts),
        )
        return count_row[0][0] if count_row else 0


async def garbage_collect() -> dict:
    """Remove stale memories based on decay scoring.

    Called periodically (e.g., daily at 03:00).
    Returns stats about what was cleaned.
    """
    now = datetime.utcnow()
    hard_cutoff = (now - timedelta(days=DECAY_MAX_AGE_DAYS)).isoformat()
    stats = {"hard_deleted": 0, "decay_deleted": 0, "total_remaining": 0}

    async with aiosqlite.connect(str(DB_PATH)) as db:
        # 1. Hard delete: anything older than 90 days
        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations WHERE timestamp < ?",
            (hard_cutoff,),
        )
        row = await cursor.fetchone()
        stats["hard_deleted"] = row[0] if row else 0

        if stats["hard_deleted"] > 0:
            # Remove from FTS first
            ids = await db.execute_fetchall(
                "SELECT id FROM conversations WHERE timestamp < ?",
                (hard_cutoff,),
            )
            for (rid,) in ids:
                await db.execute(
                    "INSERT INTO conversations_fts(conversations_fts, rowid, content) VALUES('delete', ?, '')",
                    (rid,),
                )
            await db.execute(
                "DELETE FROM conversations WHERE timestamp < ?",
                (hard_cutoff,),
            )

        # 2. Decay delete: scan records older than 14 days, check score
        decay_cutoff = (now - timedelta(days=14)).isoformat()
        rows = await db.execute_fetchall(
            "SELECT id, timestamp, last_accessed, access_count FROM conversations WHERE timestamp < ?",
            (decay_cutoff,),
        )
        decay_ids = []
        for rid, ts, la, ac in rows:
            score = _decay_score(ts, la, ac or 0)
            if score < DECAY_THRESHOLD:
                decay_ids.append(rid)

        stats["decay_deleted"] = len(decay_ids)
        for rid in decay_ids:
            await db.execute(
                "INSERT INTO conversations_fts(conversations_fts, rowid, content) VALUES('delete', ?, '')",
                (rid,),
            )
        if decay_ids:
            placeholders = ",".join("?" * len(decay_ids))
            await db.execute(
                f"DELETE FROM conversations WHERE id IN ({placeholders})",
                decay_ids,
            )

        await db.commit()

        # 3. Count remaining
        cursor = await db.execute("SELECT COUNT(*) FROM conversations")
        row = await cursor.fetchone()
        stats["total_remaining"] = row[0] if row else 0

    logger.info(
        f"Memory GC: hard_deleted={stats['hard_deleted']} "
        f"decay_deleted={stats['decay_deleted']} "
        f"remaining={stats['total_remaining']}"
    )
    return stats
