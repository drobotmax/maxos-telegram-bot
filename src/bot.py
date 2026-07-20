import asyncio
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from collections import defaultdict
from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from . import sessions
from .config import (
    BOT_TOKEN,
    ALLOWED_USERS,
    ADMIN_USERS,
    ADMIN_TELEGRAM_ID,
    MAXOS_DIR,
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_WINDOW_SEC,
    MAX_FILE_SIZE_BYTES,
    get_chat_config,
)
from .claude_worker import process, generate_summary
from .scheduler import build_morning_briefing, build_daily_report, build_inbox_check
from .hn_digest import build_hn_digest
from .reddit_digest import build_reddit_digest, record_feedback

logger = logging.getLogger(__name__)

# Rate limiter: user_id -> list of timestamps
_rate_log: dict[int, list[float]] = defaultdict(list)


async def _keep_typing(chat, stop_event: asyncio.Event):
    """Send 'typing' action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await chat.send_action("typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
            break
        except asyncio.TimeoutError:
            pass


def _check_rate_limit(user_id: int) -> bool:
    now = datetime.now().timestamp()
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    _rate_log[user_id] = [t for t in _rate_log[user_id] if t > cutoff]
    if len(_rate_log[user_id]) >= RATE_LIMIT_MESSAGES:
        return False
    _rate_log[user_id].append(now)
    return True


async def _download_telegram_file(file_obj, bot) -> tuple[str | None, str | None]:
    """Download a Telegram file to /tmp/. Returns (file_path, error_msg)."""
    try:
        tg_file = await bot.get_file(file_obj.file_id)
        if tg_file.file_size and tg_file.file_size > MAX_FILE_SIZE_BYTES:
            return None, "Файл слишком большой (макс 5MB)."
        ext = os.path.splitext(tg_file.file_path or "")[1] or ""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp")
        tmp.close()
        await tg_file.download_to_drive(tmp.name)
        return tmp.name, None
    except Exception as e:
        return None, f"Не удалось загрузить файл: {e}"


# Track consecutive network errors for backoff
_consecutive_network_errors: int = 0
_MAX_BACKOFF_SEC: int = 60

# A reply to a comment-digest block (from tg-comment-digest.py) is Maxim's final
# comment text, NOT a query for the bot. The mac-side poller (tg-comment-poster.py)
# reads those replies via the user session and posts them as comments. The bot must
# NOT route them to Claude. Detection mirrors the poller: parent sent by the bot,
# contains a "ЧЕРНОВИК" marker and a t.me/<ch>/<id> link.
_COMMENT_LINK_RE = re.compile(r"https?://t\.me/[A-Za-z0-9_]+/\d+")


def _is_comment_draft_reply(message, bot_id: int) -> bool:
    parent = getattr(message, "reply_to_message", None)
    if not parent:
        return False
    if not parent.from_user or parent.from_user.id != bot_id:
        return False
    ptext = parent.text or parent.caption or ""
    return "ЧЕРНОВИК" in ptext and bool(_COMMENT_LINK_RE.search(ptext))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler. Retries on transient errors instead of crashing."""
    global _consecutive_network_errors
    error = context.error

    if isinstance(error, Conflict):
        logger.warning("Conflict: another getUpdates instance running. Continuing...")
        _consecutive_network_errors = 0
        return

    if isinstance(error, RetryAfter):
        logger.warning(f"Rate limited by Telegram. Sleeping {error.retry_after}s")
        await asyncio.sleep(error.retry_after)
        _consecutive_network_errors = 0
        return

    if isinstance(error, (NetworkError, TimedOut)):
        _consecutive_network_errors += 1
        backoff = min(2 ** (_consecutive_network_errors - 1), _MAX_BACKOFF_SEC)
        logger.warning(
            f"NetworkError (#{_consecutive_network_errors}): {error}. "
            f"Sleeping {backoff}s before retry..."
        )
        await asyncio.sleep(backoff)
        return

    _consecutive_network_errors = 0
    logger.error(f"Unhandled exception: {error}", exc_info=context.error)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    # Accept text, photos, and documents; skip voice/video/stickers (RAM constraint)
    if not message.text and not message.photo and not message.document:
        return

    # Whitelist
    if user.id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized: user_id={user.id} username={user.username}")
        return

    # Rate limit
    if not _check_rate_limit(user.id):
        await message.reply_text("Слишком много запросов. Подожди минуту.")
        return

    is_private = chat.type == "private"
    chat_config = get_chat_config(chat.id, is_private)

    # In groups: only respond to @mentions (unless auto_respond)
    if not is_private and not chat_config.get("auto_respond", False):
        bot_username = context.bot.username or ""
        raw = message.text or message.caption or ""
        if f"@{bot_username}" not in raw:
            return

    bot_mention = f"@{context.bot.username}"
    text = ""
    file_path = None
    cleanup_file = False

    if message.text:
        text = message.text.replace(bot_mention, "").strip()

    # Handle photo
    if message.photo:
        photo = message.photo[-1]  # highest resolution
        file_path, err = await _download_telegram_file(photo, context.bot)
        if err:
            await message.reply_text(err)
            return
        cleanup_file = True
        caption = (message.caption or "").replace(bot_mention, "").strip()
        text = f"{caption}\n\n[Пользователь отправил фото. Файл: {file_path}]" if caption else f"[Пользователь отправил фото. Файл: {file_path}]"

    # Handle document
    elif message.document:
        doc = message.document
        if doc.file_size and doc.file_size > MAX_FILE_SIZE_BYTES:
            await message.reply_text("Файл слишком большой (макс 5MB).")
            return
        file_path, err = await _download_telegram_file(doc, context.bot)
        if err:
            await message.reply_text(err)
            return
        cleanup_file = True
        caption = (message.caption or "").replace(bot_mention, "").strip()
        mime = doc.mime_type or ""
        fname = doc.file_name or "file"
        if mime.startswith("image/"):
            desc = f"изображение ({fname})"
        elif mime == "application/pdf":
            desc = f"PDF документ ({fname})"
        else:
            desc = f"файл ({fname}, {mime})"
        text = f"{caption}\n\n[Пользователь отправил {desc}. Файл: {file_path}]" if caption else f"[Пользователь отправил {desc}. Файл: {file_path}]"

    if not text:
        return

    # Reply to a comment-digest block = a comment to post, not a query.
    # Skip Claude; the mac-side poller posts it and confirms with "✅ Запостил".
    if _is_comment_draft_reply(message, context.bot.id):
        await message.reply_text("📝 Принял, запощу комментом в течение пары минут.")
        return

    # Persistent typing indicator (refreshes every 4s)
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))

    try:
        response = await process(
            chat_id=chat.id,
            message=text,
            chat_config=chat_config,
        )
    finally:
        stop_typing.set()
        await typing_task
        if cleanup_file and file_path:
            try:
                os.unlink(file_path)
            except OSError:
                pass

    # Split long messages (Telegram limit 4096)
    for i in range(0, len(response), 4096):
        await message.reply_text(response[i : i + 4096])


# --- Commands ---

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        response = await process(
            chat_id=chat.id,
            message="Покажи дашборд всех проектов. Прочитай projects/_index.md и выведи таблицу с проектами, доходом, статусом и ключевыми TODO.",
            chat_config={"context": "Command: /status"},
        )
    finally:
        stop_typing.set()
        await typing_task
    for i in range(0, len(response), 4096):
        await update.effective_message.reply_text(response[i : i + 4096])


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        response = await build_morning_briefing(chat_id=chat.id)
    finally:
        stop_typing.set()
        await typing_task
    for i in range(0, len(response), 4096):
        await update.effective_message.reply_text(response[i : i + 4096])


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        response = await build_daily_report(chat_id=chat.id)
    finally:
        stop_typing.set()
        await typing_task
    for i in range(0, len(response), 4096):
        await update.effective_message.reply_text(response[i : i + 4096])


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        response = await build_hn_digest()
    finally:
        stop_typing.set()
        await typing_task
    for i in range(0, len(response), 4096):
        await update.effective_message.reply_text(response[i : i + 4096])


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        summary = await generate_summary(chat.id)
        await sessions.clear_session(chat.id)
    finally:
        stop_typing.set()
        await typing_task
    msg = "История чата очищена. Следующее сообщение начнёт новую сессию."
    if summary:
        msg += f"\n\nСохранённое резюме:\n{summary[:500]}"
    await update.effective_message.reply_text(msg)


async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        response = await build_inbox_check(chat_id=chat.id)
    finally:
        stop_typing.set()
        await typing_task
    for i in range(0, len(response), 4096):
        await update.effective_message.reply_text(response[i : i + 4096])


async def cmd_reddit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return
    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))
    try:
        text, keyboard = await build_reddit_digest()
    finally:
        stop_typing.set()
        await typing_task
    if len(text) <= 4096:
        await update.effective_message.reply_text(text, reply_markup=keyboard)
    else:
        for i in range(0, len(text), 4096):
            chunk = text[i : i + 4096]
            markup = keyboard if i + 4096 >= len(text) else None
            await update.effective_message.reply_text(chunk, reply_markup=markup)


async def handle_reddit_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Reddit digest feedback buttons."""
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id not in ADMIN_USERS:
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data or ""
    if data == "rd:like":
        record_feedback("like")
        await query.answer("👍 Записал! Спасибо за фидбэк")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    elif data == "rd:dislike":
        record_feedback("dislike")
        await query.answer("👎 Записал. Что не так? Напиши в чат – запомню")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a Todoist task. Usage: /task Buy groceries tomorrow #Label"""
    if update.effective_user.id not in ALLOWED_USERS:
        return
    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.effective_message.reply_text(
            "Использование: /task <название задачи>\n"
            "Примеры:\n"
            "/task Позвонить клиенту завтра\n"
            "/task Подготовить КП до пятницы #Консалтинг"
        )
        return

    from .todoist import create_task

    # Parse labels from #hashtags
    labels = re.findall(r"#(\w+)", args)
    clean_content = re.sub(r"\s*#\w+", "", args).strip()

    task = await create_task(
        content=clean_content,
        labels=labels or ["MaxOS"],
    )
    if task:
        url = task.get("url", "")
        await update.effective_message.reply_text(
            f"✅ Задача создана: {clean_content}"
            + (f"\n🔗 {url}" if url else "")
        )
    else:
        await update.effective_message.reply_text("❌ Не удалось создать задачу. Проверь TODOIST_API_TOKEN.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    await update.effective_message.reply_text(
        "Команды:\n"
        "/status – дашборд проектов\n"
        "/checkin – утренний брифинг\n"
        "/report – вечерний отчёт\n"
        "/inbox – проверка входящих + черновики ответов\n"
        "/research – AI-дайджест (HN + Lobsters)\n"
        "/reddit – Reddit-дайджест\n"
        "/task <текст> – создать задачу в Todoist\n"
        "/clear – сбросить сессию\n"
        "/help – справка\n\n"
        "Медиа: фото, PDF, документы\n\n"
        "Авто:\n"
        "- 07:00 – утренний брифинг\n"
        "- 18:00 – вечерний отчёт\n"
        "- Пн+Чт 06:15 – AI research\n"
        "- Сб 12:00 – Reddit"
    )


# --- Post-call inline button callbacks ---

async def handle_postcall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses from post-call notifications."""
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id not in ADMIN_USERS:
        await query.answer("Нет доступа", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) < 3 or parts[0] != "pc":
        await query.answer("Неизвестная команда", show_alert=True)
        return

    action = parts[1]
    meeting_hash = parts[2]
    original_text = query.message.text if query.message else ""

    chat = update.effective_chat
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop_typing))

    try:
        # Load postcall JSON for richer context
        postcall_context = _load_postcall_data(meeting_hash)

        if action == "send":
            response = await _handle_send_followup(postcall_context, original_text)
        elif action == "post":
            response = await _handle_draft_post(postcall_context, original_text)
        elif action == "cal":
            response = await _handle_create_event(postcall_context, original_text)
        else:
            response = f"Неизвестное действие: {action}"
    finally:
        stop_typing.set()
        await typing_task

    for i in range(0, len(response), 4096):
        await query.message.reply_text(response[i : i + 4096])

    # Mark action done: edit original message, remove keyboard
    try:
        action_labels = {"send": "Follow-up отправлен", "post": "Пост подготовлен", "cal": "Событие создано"}
        label = action_labels.get(action, "Выполнено")
        updated = original_text + f"\n\n\u2705 {label}"
        await query.message.edit_text(updated[:4096])
    except Exception:
        pass


def _load_postcall_data(meeting_hash: str) -> dict:
    """Try to load postcall JSON file matching the meeting hash."""
    import os
    postcall_dir = os.path.join(MAXOS_DIR, "store", "postcall")
    if not os.path.isdir(postcall_dir):
        return {}
    for fname in os.listdir(postcall_dir):
        if not fname.endswith(".json"):
            continue
        # Match: filename starts with something similar to the hash
        safe_name = fname.replace(".json", "").replace("-", "_").replace(" ", "_")
        if meeting_hash in safe_name or safe_name.startswith(meeting_hash[:10]):
            import json
            try:
                with open(os.path.join(postcall_dir, fname)) as f:
                    return json.load(f)
            except Exception:
                return {}
    return {}


async def _handle_send_followup(postcall_data: dict, original_text: str) -> str:
    """Show follow-up draft for approval. Never auto-send."""
    followup = postcall_data.get("client_followup", {})
    extra = ""
    if followup and followup.get("draft"):
        extra = (
            f"\n\nИзвлечённые данные:\n"
            f"Получатель: {followup.get('recipient', '?')}\n"
            f"Канал: {followup.get('channel', '?')}\n"
            f"Черновик: {followup.get('draft', '')}\n"
        )

    prompt = (
        f"В уведомлении о встрече был подготовлен черновик follow-up.\n\n"
        f"Текст уведомления:\n{original_text[:1500]}\n"
        f"{extra}\n"
        f"ЗАДАЧА: Покажи финальный текст follow-up для одобрения Максимом.\n"
        f"НЕ отправляй сообщение - только покажи черновик.\n"
        f"Формат ответа:\n"
        f"Получатель: [имя]\n"
        f"Канал: [WhatsApp/Email]\n"
        f"Текст:\n[черновик]\n\n"
        f"Если Максим ответит 'отправь' - тогда отправь через WhatsApp MCP (send_message)."
    )
    return await process(
        chat_id=ADMIN_TELEGRAM_ID,
        message=prompt,
        chat_config={"context": "Post-call follow-up draft. Show draft, do NOT send until Maxim confirms."},
    )


async def _handle_draft_post(postcall_data: dict, original_text: str) -> str:
    """Generate a Telegram channel post from meeting content ideas."""
    ideas = postcall_data.get("content_ideas", [])
    extra = ""
    if ideas:
        extra = "\n\nИзвлечённые идеи:\n"
        for idea in ideas:
            extra += f"- {idea.get('topic', '')}: {idea.get('angle', '')} (hook: {idea.get('hook', '')})\n"

    prompt = (
        f"Из встречи были извлечены идеи для контента.\n\n"
        f"Текст уведомления:\n{original_text[:1500]}\n"
        f"{extra}\n"
        f"ЗАДАЧА: Напиши полноценный пост для Telegram-канала на основе самой сильной идеи.\n"
        f"Формат: hook (первая строка, цепляет) \u2192 основная мысль (3-5 предложений) \u2192 вывод/CTA.\n"
        f"Стиль: экспертный, конкретный, с цифрами. Как пишет практик, не теоретик.\n"
        f"Длина: 500-800 символов. Только короткое тире (\u2013).\n"
        f"Язык: русский."
    )
    return await process(
        chat_id=ADMIN_TELEGRAM_ID,
        message=prompt,
        chat_config={"context": "Content creation from meeting insights."},
    )


async def _handle_create_event(postcall_data: dict, original_text: str) -> str:
    """Create calendar event from next meeting info."""
    next_mtg = postcall_data.get("next_meeting", {})
    extra = ""
    if next_mtg:
        extra = (
            f"\n\nИзвлечённые данные:\n"
            f"С кем: {next_mtg.get('with_whom', '?')}\n"
            f"Когда: {next_mtg.get('when', '?')}\n"
            f"Тема: {next_mtg.get('topic', '?')}\n"
        )

    prompt = (
        f"В уведомлении о встрече упоминалась следующая встреча.\n\n"
        f"Текст уведомления:\n{original_text[:1500]}\n"
        f"{extra}\n"
        f"ЗАДАЧА: Создай событие в Google Calendar.\n"
        f"Если точное время указано - создай событие через Google Calendar MCP (create_event).\n"
        f"Если время неточное - предложи конкретное время и спроси Максима.\n"
        f"Часовой пояс: Europe/Moscow."
    )
    return await process(
        chat_id=ADMIN_TELEGRAM_ID,
        message=prompt,
        chat_config={"context": "Calendar event creation from meeting."},
    )


def create_bot() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("reset", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("inbox", cmd_inbox))
    app.add_handler(CommandHandler("reddit", cmd_reddit))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
        handle_message,
    ))
    app.add_handler(CallbackQueryHandler(handle_reddit_feedback, pattern=r"^rd:"))
    app.add_handler(CallbackQueryHandler(handle_postcall_callback, pattern=r"^pc:"))

    # Global error handler – prevents crashes on transient network errors
    app.add_error_handler(error_handler)

    return app
