"""MAX side of the TG<->MAX bridge, embedded into the main MAX bot.

There is no second bot: creating one is currently not possible, so the
bridge shares the main bot (MAX_BOT_TOKEN). Since /updates of a bot has a
single consumer, this module does NOT poll; instead src/max_bot.py calls
handle_bridge_update() from its long-poll loop for every incoming message.
The handler returns True when the update belongs to the bridge (consumed)
and False when it should fall through to the normal Claude flow.

Shared contract with src/tg_bridge.py lives in src/bridge_store.py.

What the bridge consumes (admin = MAX_ADMIN_USER_ID only):
  - reply to a bridged message (mid found in bridge_map) ->
    send_to_tg(tg_chat_id, text, tg_msg_id)
  - /chat <query> -> resolve_chat(query) -> bridge_store.set_active_chat
  - /send <text>  -> active chat (plain text stays with Claude by design)
  - /status       -> active chat + bridge_map records over the last 24h
Replies to non-bridged messages and plain text are NOT consumed - they go
to Claude as before.

Wiring (integration stage, main.py):
  max_bridge_bot.configure(send_to_tg, resolve_chat)  # from tg_bridge
where
  send_to_tg(tg_chat_id: int, text: str, reply_to_msg_id: int | None) - async
  resolve_chat(query: str) -> (tg_chat_id, title) | None - async, substring
    search by TG chat title. Both implemented by tg_bridge (Telethon).

notify_max(text, reply_metadata) -> mid is called by tg_bridge to deliver
forwarded TG messages into Maxim's dialog (send by user_id, no chat_id env
needed).

No new dependencies (httpx already in pyproject).
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, Tuple

from . import bridge_store
from .config import MAX_ADMIN_USER_ID, MAX_BOT_TOKEN
from .max_api import MaxAPI, MaxAPIError

logger = logging.getLogger(__name__)

# send_to_tg(tg_chat_id, text, reply_to_msg_id) - provided by tg_bridge (Telethon).
SendToTg = Callable[[int, str, Optional[int]], Awaitable[None]]
# resolve_chat(query) -> (tg_chat_id, title) | None - provided by tg_bridge.
ResolveChat = Callable[[str], Awaitable[Optional[Tuple[int, str]]]]

_MAX_CHUNK = 4000

_send_to_tg: Optional[SendToTg] = None
_resolve_chat: Optional[ResolveChat] = None

# Dedicated client for notify_max (sending is multi-consumer safe; only
# /updates is exclusive, and that stays with max_bot.py).
_api: Optional[MaxAPI] = None


def configure(send_to_tg: SendToTg, resolve_chat: ResolveChat) -> None:
    """Register Telethon-side callbacks. Called from main.py at startup.

    Until configured, handle_bridge_update() consumes nothing, so the main
    bot keeps working standalone.
    """
    global _send_to_tg, _resolve_chat
    _send_to_tg = send_to_tg
    _resolve_chat = resolve_chat
    logger.info("max_bridge: configured with TG callbacks")


def _get_api() -> MaxAPI:
    global _api
    if _api is None:
        if not MAX_BOT_TOKEN:
            raise MaxAPIError("MAX_BOT_TOKEN not set")
        _api = MaxAPI(MAX_BOT_TOKEN)
    return _api


async def aclose() -> None:
    """Release the notify client. Called on shutdown from main.py."""
    global _api
    if _api is not None:
        await _api.aclose()
        _api = None


async def _send_with_retry(api: MaxAPI, text: str,
                           attachments: Optional[list[dict]]) -> dict:
    """Send to Maxim's dialog; freshly uploaded attachments may not be
    processed yet (MAX returns attachment.not.ready) - retry briefly."""
    last_err: Optional[MaxAPIError] = None
    for attempt in range(6):
        try:
            return await api.send_message(
                user_id=MAX_ADMIN_USER_ID, text=text, attachments=attachments
            )
        except MaxAPIError as e:
            if attachments and "not.ready" in str(e):
                last_err = e
                await asyncio.sleep(1.0 + attempt)
                continue
            raise
    raise last_err  # type: ignore[misc]


async def notify_max(text: str, reply_metadata: dict,
                     media: Optional[dict] = None) -> str:
    """Send a forwarded TG message into Maxim's MAX dialog.

    Called by tg_bridge (Stream A) for each incoming TG message. Returns the
    MAX message id (mid); the caller stores it via bridge_store.save_mapping
    so that a later reply in MAX can be routed back.

    reply_metadata is the TG-side context dict (e.g. chat title, sender);
    currently used for logging only - the caller formats `text` itself.
    media (optional): {"kind": "image"|"video"|"audio"|"file",
    "filename": str, "data": bytes} - uploaded to MAX and attached to the
    first chunk. On upload failure the message is sent as text only.
    If the text exceeds the MAX per-message limit it is split; the mid of the
    LAST chunk is returned (that is the message Maxim will reply to).
    """
    if not MAX_ADMIN_USER_ID:
        raise MaxAPIError("MAX_ADMIN_USER_ID not set")
    api = _get_api()

    attachments: Optional[list[dict]] = None
    if media:
        try:
            attachments = [await api.upload_attachment(
                media["kind"], media["filename"], media["data"]
            )]
        except (MaxAPIError, KeyError) as e:
            logger.error(f"max_bridge: media upload failed, sending text only: {e}")

    mid = ""
    chunks = [text[i:i + _MAX_CHUNK] for i in range(0, len(text), _MAX_CHUNK)] or [""]
    for n, chunk in enumerate(chunks):
        result = await _send_with_retry(api, chunk, attachments if n == 0 else None)
        mid = str(((result.get("message") or {}).get("body") or {}).get("mid", ""))
    logger.debug(f"max_bridge: notify_max delivered mid={mid} meta={reply_metadata}")
    return mid


def _extract_reply_mid(msg: dict) -> Optional[str]:
    """Return mid of the message being replied to, or None for non-replies."""
    link = msg.get("link") or {}
    if link.get("type") != "reply":
        return None
    linked = link.get("message") or {}
    mid = linked.get("mid") or (linked.get("body") or {}).get("mid")
    return str(mid) if mid else None


def _count_mappings_last_day() -> int:
    # bridge_store does not expose counts and must not be modified (shared
    # contract), so read its connection under its own lock.
    cutoff = int(time.time()) - 86400
    with bridge_store._lock:
        row = bridge_store._get_conn().execute(
            "SELECT COUNT(*) FROM bridge_map WHERE ts > ?", (cutoff,)
        ).fetchone()
    return row[0] if row else 0


async def _reply(api: MaxAPI, chat_id: int, text: str) -> None:
    try:
        await api.send_message(chat_id=chat_id, text=text)
    except MaxAPIError as e:
        logger.error(f"max_bridge: send_message failed: {e}")


async def _deliver(api: MaxAPI, chat_id: int, tg_chat_id: int, text: str,
                   reply_to: Optional[int], title: str) -> None:
    """Send to TG via the registered callback and confirm back in MAX."""
    try:
        await _send_to_tg(tg_chat_id, text, reply_to)
    except Exception as e:
        logger.error(f"max_bridge: send_to_tg failed: {e}")
        await _reply(api, chat_id, f"Ошибка отправки в Telegram: {e}")
        return
    # MaxAPI has no reactions endpoint, so confirm with a short text reply.
    await _reply(api, chat_id, f"-> отправлено в {title or tg_chat_id}")


async def _handle_command(api: MaxAPI, chat_id: int, head: str, query: str) -> bool:
    """Bridge commands. Returns False for commands the bridge does not own."""
    if head == "chat":
        if not query:
            await _reply(api, chat_id, "Использование: /chat <название чата>")
            return True
        found = await _resolve_chat(query)
        if not found:
            await _reply(api, chat_id, f"Чат по запросу '{query}' не найден.")
            return True
        tg_chat_id, title = found
        bridge_store.set_active_chat(tg_chat_id, title)
        await _reply(api, chat_id, f"Активный чат: {title}")
        return True

    if head == "send":
        if not query:
            await _reply(api, chat_id, "Использование: /send <текст>")
            return True
        active = bridge_store.get_active_chat()
        if active is None:
            await _reply(api, chat_id,
                         "Активный чат не задан. Используй /chat <название>.")
            return True
        tg_chat_id, title = active
        await _deliver(api, chat_id, tg_chat_id, query, None, title)
        return True

    if head == "status":
        active = bridge_store.get_active_chat()
        active_line = f"Активный чат: {active[1]}" if active else "Активный чат не задан."
        count = _count_mappings_last_day()
        await _reply(api, chat_id, f"{active_line}\nСообщений в мосте за сутки: {count}")
        return True

    return False  # /clear, /help etc. belong to max_bot.py


async def handle_bridge_update(api: MaxAPI, update: dict) -> bool:
    """Try to consume a message_created update as bridge traffic.

    Called by max_bot.py before its own command/Claude handling. Returns
    True if the update was consumed by the bridge, False to fall through.
    """
    if _send_to_tg is None or _resolve_chat is None:
        return False  # not wired yet - bridge inactive

    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    recipient = msg.get("recipient") or {}
    body = msg.get("body") or {}
    text = (body.get("text") or "").strip()

    user_id = sender.get("user_id")
    chat_id = recipient.get("chat_id") or user_id

    if not user_id or not chat_id or not text:
        return False
    if user_id != MAX_ADMIN_USER_ID:
        return False  # bridge is admin-only; others go through normal flow

    reply_mid = _extract_reply_mid(msg)
    if reply_mid:
        mapping = bridge_store.get_mapping(reply_mid)
        if mapping is None:
            return False  # reply to a non-bridged message - Claude's business
        tg_chat_id, tg_msg_id, title = mapping
        await _deliver(api, chat_id, tg_chat_id, text, tg_msg_id, title)
        return True

    if text.startswith("/"):
        head, _, rest = text.partition(" ")
        head = head.lower().lstrip("/").split("@", 1)[0]
        return await _handle_command(api, chat_id, head, rest.strip())

    return False  # plain text stays with Claude (use /send for the bridge)
