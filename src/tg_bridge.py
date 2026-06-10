"""Telegram userbot side of the TG<->MAX bridge.

Listens to incoming messages on Maxim's Telegram account via Telethon and
forwards them to the MAX messenger through a callback provided by the
integration layer (see src/bridge_store.py for the routing contract).

Dependencies (to be added to pyproject.toml by the integration step):
  - telethon>=1.36

Env:
  BRIDGE_API_ID          Telegram api_id (my.telegram.org)
  BRIDGE_API_HASH        Telegram api_hash
  BRIDGE_SESSION_PATH    Telethon session file (default: data/bridge.session),
                         created once by scripts/bridge_login.py
  BRIDGE_GROUP_WHITELIST comma-separated TG chat ids of groups to forward

Filtering rules (MVP):
  - private dialogs: always forwarded (except messages from bots)
  - groups: only if chat_id is in BRIDGE_GROUP_WHITELIST
  - broadcast channels and bots: ignored
  - own outgoing messages: ignored

Anti-flood: if more than FLOOD_THRESHOLD messages arrive from one chat
within FLOOD_WINDOW_SEC, the overflow is batched into a single MAX message.
"""
import asyncio
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import bridge_store

logger = logging.getLogger(__name__)

DEFAULT_SESSION_PATH = "data/bridge.session"
FLOOD_WINDOW_SEC = 10.0
FLOOD_THRESHOLD = 5
# Media larger than this is forwarded as a text placeholder only.
MEDIA_MAX_BYTES = 20 * 1024 * 1024

# Set while run_tg_bridge is active; used by send_to_tg.
_client: Optional[Any] = None

# notify_max contract (implemented by the MAX side / integration):
#   async def notify_max(text: str, reply_metadata: dict,
#                        media: dict | None = None) -> str | int
# reply_metadata = {"tg_chat_id": int, "tg_msg_id": int, "chat_title": str}
# media = {"kind": "image"|"video"|"audio"|"file", "filename": str, "data": bytes}
# Returns the MAX message id of the forwarded message.
NotifyMax = Callable[..., Awaitable[Any]]


def parse_whitelist(raw: str) -> set[int]:
    """Parse BRIDGE_GROUP_WHITELIST: comma-separated chat ids, junk skipped."""
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning(f"tg_bridge: bad whitelist entry skipped: {part!r}")
    return ids


def should_forward(event: Any, sender: Any, whitelist: set[int]) -> bool:
    """Decide whether an incoming Telethon event gets forwarded to MAX.

    `event` needs: out, is_private, is_group, is_channel, chat_id.
    `sender` needs: bot (None sender -> not forwarded for private chats).
    """
    if getattr(event, "out", False):
        return False
    if event.is_private:
        if sender is None or getattr(sender, "bot", False):
            return False
        return True
    if event.is_group:
        return event.chat_id in whitelist
    # Broadcast channels (is_channel without is_group) are ignored.
    return False


def media_placeholder(message: Any) -> Optional[str]:
    """Text stub for media in MVP. None if the message has no media."""
    if getattr(message, "photo", None):
        return "[фото]"
    if getattr(message, "voice", None):
        return "[голосовое]"
    if getattr(message, "document", None):
        name = getattr(getattr(message, "file", None), "name", None)
        return f"[документ: {name}]" if name else "[документ]"
    if getattr(message, "media", None):
        return "[медиа]"
    return None


def classify_media(message: Any) -> Optional[tuple[str, str]]:
    """Map a Telethon message's media to a MAX attachment (kind, filename).

    Returns None for plain text messages. Stickers/geo/etc. fall back to
    "file" only when a downloadable document is present.
    """
    name = getattr(getattr(message, "file", None), "name", None)
    if getattr(message, "photo", None):
        return "image", "photo.jpg"
    if getattr(message, "voice", None):
        return "audio", name or "voice.ogg"
    if getattr(message, "video_note", None):
        return "video", "video_note.mp4"
    if getattr(message, "video", None):
        return "video", name or "video.mp4"
    if getattr(message, "audio", None):
        return "audio", name or "audio.mp3"
    if getattr(message, "document", None):
        return "file", name or "file.bin"
    return None


def format_message(chat_title: str, text: str, placeholder: Optional[str] = None) -> str:
    """Build the MAX-side text: `[Chat name]` header + body.

    For media messages `text` is the caption (may be empty).
    """
    parts = [f"[{chat_title}]"]
    if placeholder:
        parts.append(placeholder)
    if text:
        parts.append(text)
    return "\n".join(parts)


class FloodBatcher:
    """Per-chat anti-flood: normally entries are sent one by one, but once
    more than `threshold` messages arrive from a chat within `window` seconds,
    subsequent entries are buffered and flushed as a single batch."""

    def __init__(self, send: Callable[[list[dict]], Awaitable[None]],
                 window: float = FLOOD_WINDOW_SEC, threshold: int = FLOOD_THRESHOLD):
        self._send = send
        self._window = window
        self._threshold = threshold
        self._recent: dict[int, list[float]] = defaultdict(list)
        self._pending: dict[int, list[dict]] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def is_flooding(self, chat_id: int, now: float) -> bool:
        cutoff = now - self._window
        self._recent[chat_id] = [t for t in self._recent[chat_id] if t > cutoff]
        return len(self._recent[chat_id]) > self._threshold

    async def submit(self, chat_id: int, entry: dict) -> None:
        now = time.monotonic()
        self._recent[chat_id].append(now)
        if chat_id in self._pending or self.is_flooding(chat_id, now):
            self._pending.setdefault(chat_id, []).append(entry)
            if chat_id not in self._tasks:
                self._tasks[chat_id] = asyncio.create_task(self._flush_later(chat_id))
            return
        await self._send([entry])

    async def _flush_later(self, chat_id: int) -> None:
        await asyncio.sleep(self._window)
        entries = self._pending.pop(chat_id, [])
        self._tasks.pop(chat_id, None)
        if not entries:
            return
        try:
            await self._send(entries)
        except Exception as e:
            logger.exception(f"tg_bridge: batch flush failed for chat {chat_id}: {e}")

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._pending.clear()


def build_batch_text(entries: list[dict]) -> str:
    """Combine batched entries (same chat) into one MAX message."""
    if len(entries) == 1:
        return entries[0]["text"]
    title = entries[0]["chat_title"]
    bodies = []
    for e in entries:
        # Strip the per-message header, keep only the body.
        body = e["text"]
        prefix = f"[{e['chat_title']}]\n"
        if body.startswith(prefix):
            body = body[len(prefix):]
        bodies.append(body)
    header = f"[{title}] ({len(entries)} messages)"
    return "\n".join([header] + [f"- {b}" for b in bodies])


async def send_to_tg(chat_id: int, text: str, reply_to_msg_id: int | None = None) -> Any:
    """Send a message to Telegram via the bridge userbot client.

    Used by the MAX side (max_bridge_bot) to deliver replies from MAX back
    into the original Telegram chat. Returns the sent Telethon message.
    Raises RuntimeError if the bridge is not running.
    """
    if _client is None:
        raise RuntimeError("tg_bridge: client is not running")
    return await _client.send_message(chat_id, text, reply_to=reply_to_msg_id)


async def resolve_chat(query: str) -> Optional[tuple[int, str]]:
    """Find a TG dialog by case-insensitive substring of its title/name.

    Used by max_bridge_bot for the /chat command. Returns (chat_id, title)
    of the first match or None. Returns None if the bridge is not running.
    """
    if _client is None:
        return None
    q = query.strip().lower()
    if not q:
        return None
    async for dialog in _client.iter_dialogs(limit=300):
        name = dialog.name or ""
        if q in name.lower():
            return dialog.id, name
    return None


async def run_tg_bridge(notify_max: NotifyMax) -> None:
    """Start the Telethon userbot and forward incoming TG messages to MAX.

    notify_max: async callable(text: str, reply_metadata: dict) -> max_message_id.
    Exits quietly (transport disabled) if the session file is missing or
    not authorized — mirrors the MAX_BOT_TOKEN behavior in max_bot.py.
    """
    global _client

    api_id_raw = os.getenv("BRIDGE_API_ID", "")
    api_hash = os.getenv("BRIDGE_API_HASH", "")
    session_path = os.getenv("BRIDGE_SESSION_PATH", DEFAULT_SESSION_PATH)

    if not api_id_raw or not api_hash:
        logger.warning("tg_bridge: BRIDGE_API_ID/BRIDGE_API_HASH not set — TG bridge disabled")
        return
    try:
        api_id = int(api_id_raw)
    except ValueError:
        logger.warning(f"tg_bridge: BRIDGE_API_ID is not an int: {api_id_raw!r} — TG bridge disabled")
        return

    session_file = Path(session_path)
    if not session_file.suffix == ".session":
        session_file = session_file.with_suffix(session_file.suffix + ".session")
    if not session_file.exists():
        logger.warning(f"tg_bridge: session file {session_file} not found — "
                       "run scripts/bridge_login.py first; TG bridge disabled")
        return

    whitelist = parse_whitelist(os.getenv("BRIDGE_GROUP_WHITELIST", ""))

    # Imported lazily so the module (filters, formatting) stays importable
    # without telethon installed.
    from telethon import TelegramClient, events

    client = TelegramClient(session_path, api_id, api_hash)

    async def _send_entries(entries: list[dict]) -> None:
        last = entries[-1]
        text = build_batch_text(entries)
        meta = {
            "tg_chat_id": last["tg_chat_id"],
            "tg_msg_id": last["tg_msg_id"],
            "chat_title": last["chat_title"],
        }
        # Media is only ever delivered as a single-entry batch (see handler).
        media = entries[0].get("media") if len(entries) == 1 else None
        try:
            max_msg_id = await notify_max(text, meta, media)
        except Exception as e:
            logger.error(f"tg_bridge: notify_max failed: {e}")
            return
        if max_msg_id is not None:
            try:
                bridge_store.save_mapping(
                    str(max_msg_id), last["tg_chat_id"], last["tg_msg_id"],
                    last["chat_title"],
                )
            except Exception as e:
                logger.error(f"tg_bridge: save_mapping failed: {e}")

    batcher = FloodBatcher(_send_entries)

    async def _on_new_message(event: Any) -> None:
        try:
            sender = await event.get_sender()
            if not should_forward(event, sender, whitelist):
                return
            chat = await event.get_chat()
            chat_title = (getattr(chat, "title", None)
                          or " ".join(filter(None, [getattr(chat, "first_name", None),
                                                    getattr(chat, "last_name", None)]))
                          or str(event.chat_id))
            placeholder = media_placeholder(event.message)
            body = event.message.message or ""
            if not body and not placeholder:
                return

            media: Optional[dict] = None
            kind = classify_media(event.message)
            size = getattr(getattr(event.message, "file", None), "size", None) or 0
            if kind and size <= MEDIA_MAX_BYTES:
                try:
                    data = await event.message.download_media(file=bytes)
                    if data:
                        media = {"kind": kind[0], "filename": kind[1], "data": data}
                except Exception as e:
                    logger.warning(f"tg_bridge: media download failed, "
                                   f"falling back to placeholder: {e}")

            # With real media attached the text placeholder is redundant.
            text = format_message(chat_title, body, None if media else placeholder)
            entry = {
                "text": text,
                "tg_chat_id": event.chat_id,
                "tg_msg_id": event.message.id,
                "chat_title": chat_title,
            }
            if media:
                # Media bypasses the flood batcher: attachments cannot be
                # merged into one MAX message.
                entry["media"] = media
                await _send_entries([entry])
            else:
                await batcher.submit(event.chat_id, entry)
        except Exception as e:
            logger.exception(f"tg_bridge: error handling message: {e}")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("tg_bridge: session is not authorized — "
                           "run scripts/bridge_login.py; TG bridge disabled")
            await client.disconnect()
            return

        me = await client.get_me()
        logger.info(f"tg_bridge: connected as {me.first_name} (id={me.id}), "
                    f"group whitelist: {sorted(whitelist) or 'empty'}")

        client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))
        _client = client
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        logger.info("tg_bridge: cancellation received, shutting down")
        raise
    except Exception as e:
        logger.error(f"tg_bridge: fatal error, transport stopped: {e}")
    finally:
        _client = None
        await batcher.aclose()
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
