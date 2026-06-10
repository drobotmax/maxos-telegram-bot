"""MAX messenger transport for MaxOS bot.

Long-polls /updates, dispatches text messages to claude_worker.process,
sends responses back. Reuses the same session DB as the Telegram transport
by namespacing chat ids with MAX_CHAT_ID_OFFSET.
"""
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime

from . import sessions
from .claude_worker import process, generate_summary
from .config import (
    MAX_BOT_TOKEN,
    MAX_ALLOWED_USERS,
    MAX_ADMIN_USERS,
    MAX_CHAT_ID_OFFSET,
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_WINDOW_SEC,
)
from .max_api import MaxAPI, MaxAPIError
from . import max_bridge_bot

logger = logging.getLogger(__name__)

_rate_log: dict[int, list[float]] = defaultdict(list)


def _check_rate_limit(user_id: int) -> bool:
    now = datetime.now().timestamp()
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    _rate_log[user_id] = [t for t in _rate_log[user_id] if t > cutoff]
    if len(_rate_log[user_id]) >= RATE_LIMIT_MESSAGES:
        return False
    _rate_log[user_id].append(now)
    return True


def _ns(chat_id: int) -> int:
    """Namespace Max chat ids to avoid collision with Telegram chat ids."""
    return chat_id + MAX_CHAT_ID_OFFSET


async def _keep_typing(api: MaxAPI, chat_id: int, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await api.send_action(chat_id, "typing_on")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
            break
        except asyncio.TimeoutError:
            pass


async def _send_long(api: MaxAPI, chat_id: int, text: str):
    """Split long messages (MAX limit ~4000 chars per message)."""
    if not text:
        return
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        await api.send_message(chat_id=chat_id, text=text[i:i + chunk_size])


async def _handle_message(api: MaxAPI, update: dict):
    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    recipient = msg.get("recipient") or {}
    body = msg.get("body") or {}
    text = body.get("text") or ""

    user_id = sender.get("user_id")
    chat_id = recipient.get("chat_id") or user_id  # dialog: chat_id may equal user_id
    chat_type = recipient.get("chat_type", "dialog")

    if not user_id or not chat_id:
        logger.debug(f"max: skipping update without user/chat: {update}")
        return

    # Whitelist
    if user_id not in MAX_ALLOWED_USERS:
        logger.warning(f"max: unauthorized user_id={user_id} name={sender.get('name')}")
        return

    if not text.strip():
        return

    # TG<->MAX bridge: replies to bridged messages and /chat, /send, /status
    # are consumed here; everything else falls through to the Claude flow.
    if await max_bridge_bot.handle_bridge_update(api, update):
        return

    # Rate limit
    if not _check_rate_limit(user_id):
        try:
            await api.send_message(chat_id=chat_id, text="Слишком много запросов. Подожди минуту.")
        except MaxAPIError:
            pass
        return

    # Slash commands
    cmd_text = text.strip()
    if cmd_text.startswith("/"):
        head = cmd_text.split()[0].lower().lstrip("/")
        # Strip bot suffix like /help@bot
        head = head.split("@", 1)[0]

        if head in ("clear", "reset"):
            try:
                summary = await generate_summary(_ns(chat_id))
                await sessions.clear_session(_ns(chat_id))
            except Exception as e:
                logger.warning(f"max: clear failed: {e}")
                summary = None
            reply = "История чата очищена. Следующее сообщение начнёт новую сессию."
            if summary:
                reply += f"\n\nСохранённое резюме:\n{summary[:500]}"
            await _send_long(api, chat_id, reply)
            return

        if head == "help":
            await _send_long(api, chat_id, (
                "Команды:\n"
                "/clear - сбросить сессию\n"
                "/help - справка\n\n"
                "Просто пиши - отвечу. Имею тот же доступ, что TG-бот."
            ))
            return

    # Default: pass to Claude
    chat_config = {
        "lang": "ru",
        "auto_respond": True,
        "context": (
            "Ты - личный AI-ассистент Максима. Полный доступ ко всем проектам и инструментам. "
            "Канал общения - мессенджер MAX (не Telegram)."
        ),
    }

    stop = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(api, chat_id, stop))
    try:
        response = await process(
            chat_id=_ns(chat_id),
            message=text,
            chat_config=chat_config,
        )
    finally:
        stop.set()
        try:
            await typing_task
        except Exception:
            pass

    try:
        await _send_long(api, chat_id, response)
    except MaxAPIError as e:
        logger.error(f"max: send_message failed: {e}")


async def run_max_bot():
    """Main long-polling loop. Cancellation-safe."""
    if not MAX_BOT_TOKEN:
        logger.warning("MAX_BOT_TOKEN not set — Max transport disabled")
        return

    api = MaxAPI(MAX_BOT_TOKEN)

    try:
        me = await api.get_me()
        logger.info(f"Max bot connected: {me.get('name')} (@{me.get('username')}, id={me.get('user_id')})")
    except Exception as e:
        logger.error(f"Max bot get_me failed, disabling transport: {e}")
        await api.aclose()
        return

    marker: int | None = None
    backoff = 1.0
    update_types = ["message_created", "message_callback", "bot_started", "bot_added"]

    try:
        while True:
            try:
                result = await api.get_updates(marker=marker, timeout=30, limit=100, types=update_types)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"max: get_updates failed: {e}; sleeping {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            updates = result.get("updates", []) or []
            new_marker = result.get("marker")
            if new_marker is not None:
                marker = new_marker

            for upd in updates:
                utype = upd.get("update_type")
                try:
                    if utype == "message_created":
                        await _handle_message(api, upd)
                    elif utype in ("bot_started", "bot_added"):
                        chat_id = upd.get("chat_id") or (upd.get("user") or {}).get("user_id")
                        user = upd.get("user") or {}
                        if chat_id and user.get("user_id") in MAX_ADMIN_USERS:
                            try:
                                await api.send_message(
                                    chat_id=chat_id,
                                    text="MaxOS на связи. Пиши - отвечу.",
                                )
                            except MaxAPIError:
                                pass
                    # message_callback handling can be added later
                except Exception as e:
                    logger.exception(f"max: error processing update: {e}")
    except asyncio.CancelledError:
        logger.info("max: cancellation received, shutting down")
        raise
    finally:
        await api.aclose()
