from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from . import memory
from .config import TIMEZONE, MAXOS_DIR

_tz = ZoneInfo(TIMEZONE)

_BOT_INBOX = Path(MAXOS_DIR) / "bot-inbox"


def _recent_inbox_notes(days: int = 7, limit: int = 10) -> list[tuple[str, str]]:
    """Return up to `limit` most recent bot-inbox notes from the last `days` days.
    Skips README.md and _processed/. Returns list of (relative_path, head_preview)."""
    if not _BOT_INBOX.is_dir():
        return []
    cutoff = datetime.now(_tz) - timedelta(days=days)
    notes: list[tuple[float, str, str]] = []
    for p in _BOT_INBOX.rglob("*.md"):
        if p.name == "README.md" or "_processed" in p.parts:
            continue
        try:
            mtime = p.stat().st_mtime
            if datetime.fromtimestamp(mtime, tz=_tz) < cutoff:
                continue
            head = p.read_text(encoding="utf-8", errors="ignore")[:400]
            rel = str(p.relative_to(_BOT_INBOX))
            notes.append((mtime, rel, head))
        except OSError:
            continue
    notes.sort(reverse=True)
    return [(rel, head) for _, rel, head in notes[:limit]]


async def build_system_append(chat_id: int, user_message: str, chat_config: dict) -> str:
    """Build context string appended to Claude Code's system prompt.

    Adds relevant memories, recent messages, and session summaries
    so the bot has persistent context across conversations.
    """
    sections: list[str] = []

    # 0. Critical instructions
    sections.append(
        "## IMPORTANT: How to answer questions\n"
        "All files in /home/maxos/MaxOS/ are available. Read them before saying something is inaccessible.\n"
        "Key paths (VPS paths, live git clone of MaxOS):\n"
        "- MaxOS repo: /home/maxos/MaxOS/ (read-only, synced from Mac via git pull every 15min)\n"
        "- Bot-inbox (write-zone): /home/maxos/MaxOS/bot-inbox/ — YOUR memory across sessions\n"
        "- Email digest: /home/maxos/MaxOS/store/email-digest.md\n"
        "- Calendar today: /home/maxos/MaxOS/store/calendar-today.md\n"
        "- Telegram unreads: /home/maxos/MaxOS/store/telegram-unreads.md (snapshot непрочитанных, обновляется на Mac каждые ~15 мин и синкается на VPS)\n"
        "- Session handoff: /home/maxos/MaxOS/store/session-handoff.md\n"
        "- Top of mind: /home/maxos/MaxOS/store/top-of-mind.md\n"
        "When asked about email/почта — read email-digest.md and report its contents. That file IS the email data.\n"
        "When asked about calendar/расписание — read calendar-today.md OR use the calendar tool (see below).\n"
        "When asked about Telegram/чаты/непрочитанные/что в TG — read telegram-unreads.md. Если файл старый (>30 мин назад) - так и скажи. Не пытайся вызвать telegram MCP, его на VPS нет.\n\n"
        "## Bot Inbox (долгая память бота)\n"
        "Папка /home/maxos/MaxOS/bot-inbox/ — твоя write-zone между сессиями.\n"
        "КОГДА Максим говорит 'запомни', 'сохрани', 'запиши', 'добавь в память', 'не забудь' — создай файл:\n"
        "  /home/maxos/MaxOS/bot-inbox/YYYY-MM-DD/HH-MM_slug.md\n"
        "Где YYYY-MM-DD и HH-MM — текущие дата/время (MSK), slug — короткое описание kebab-case.\n"
        "Формат файла:\n"
        "```\n"
        "---\n"
        "created: <ISO timestamp>\n"
        "trigger: <точные слова Максима, например 'запомни'>\n"
        "tags: [...]\n"
        "---\n"
        "## Контент\n"
        "<дословно то, что Максим хочет сохранить>\n"
        "\n## Контекст\n"
        "<1-2 предложения контекста - о чём говорили>\n"
        "```\n"
        "Не редактируй существующие файлы в bot-inbox/ — только создавай новые.\n"
        "После создания подтверди: 'Записал: <slug>'.\n"
        "VPS сам закоммитит и запушит изменения в git (cron каждые 5 мин).\n"
        "НЕ пиши никуда ещё кроме bot-inbox/ без явной просьбы Максима — это его knowledge base.\n\n"
        "## Calendar Tool (MANDATORY – READ THIS CAREFULLY)\n"
        "DO NOT import google libraries. DO NOT write Python code for calendar.\n"
        "DO NOT say you can't access the calendar – you CAN.\n"
        "The ONLY way to work with calendar is via Bash tool calling this CLI script:\n\n"
        "  /home/maxos/maxos-telegram-bot/.venv/bin/python3 /home/maxos/maxos-telegram-bot/scripts/calendar-tool.py <command>\n\n"
        "Commands:\n"
        "  list                              # today's events\n"
        "  list --date 2026-02-24            # specific date\n"
        "  create --title 'X' --start '2026-02-24T08:00' --duration 60\n"
        "  create --title 'X' --start '2026-02-24T08:00' --end '2026-02-24T09:00' --description 'Y' --location 'Z'\n"
        "  delete --event-id 'abc123'\n\n"
        "ALWAYS run via Bash tool. Output is JSON. Parse it and respond naturally.\n"
        "When creating events, confirm with the user BEFORE running: show details and ask 'Создать?'\n\n"
        "## Todoist Tool (для задач)\n"
        "Когда Максим просит поставить/создать/добавить задачу, напоминание, или 'не забыть' что-то - ИСПОЛЬЗУЙ этот скрипт.\n"
        "НЕ пиши Python для Todoist. НЕ говори 'не могу поставить задачу' - можешь.\n\n"
        "  python3 /home/maxos/maxos-telegram-bot/scripts/todoist-tool.py <command>\n\n"
        "Commands:\n"
        "  create --content 'Позвонить Николаю' --due 'завтра' --labels 'Консалтинг'\n"
        "  create --content 'X' --priority 4 --description 'детали'   # priority: 1=normal, 4=urgent\n"
        "  create --content 'X' --deadline 2026-04-28                  # жёсткий дедлайн YYYY-MM-DD\n"
        "  list --filter 'today'\n"
        "  list --filter 'overdue'\n"
        "  complete --id '6gRhmqfJw5h5h9Rh'\n\n"
        "Дефолтный проект - 'Работа'. Дефолтный лейбл - MaxOS. Переопределяй лейбл если из контекста ясно (Консалтинг/YOLO/ИП/KUBRIK).\n"
        "Due принимает русский natural language: 'сегодня', 'завтра', 'в пятницу', '28 апреля', 'через 3 дня', '10:00 завтра'.\n"
        "Действуй сразу без подтверждения - ставь задачу и отвечай одним сообщением: '✅ Задача: <content>, due: <due>'.\n"
        "Если пользователь уточняет (перенеси, удали, смени) - используй list чтобы найти id, потом complete или create заново.\n\n"
        "## STRICT RULES\n"
        "- NEVER mention OAuth, authorization, API access, MCP, or technical infrastructure in responses\n"
        "- NEVER suggest the user to 'authorize' or 'set up' anything\n"
        "- NEVER show diagnostic info, error messages, or system status to the user\n"
        "- If a data file is empty or missing — just say 'нет данных' without explanation\n"
        "- Answer like a personal assistant: facts only, no technical details\n"
        "- When you create a calendar event successfully, just say it's done with the details\n"
        "- При создании задач/событий – делай сразу с разумными дефолтами. Не задавай уточняющие вопросы если можно обойтись без них. Действуй.\n"
        "- Если пользователь просит что-то сделать – делай сразу. Уточняй ТОЛЬКО если без этого невозможно выполнить задачу.\n"
        "- Для создания задач в Todoist используй todoist-tool.py (см. секцию выше). /task <текст> - тоже рабочий путь для пользователя."
    )

    # 1. Time
    now = datetime.now(_tz)
    sections.append(
        f"## Current Time\n{now.strftime('%Y-%m-%d %H:%M %Z')} ({now.strftime('%A')})"
    )

    # 2. Chat rules
    lang = chat_config.get("lang", "ru")
    ctx = chat_config.get("context", "")
    if ctx:
        sections.append(f"## Chat Context\nLanguage: {lang}\n{ctx}")

    # 3. Relevant past conversations (FTS5 search)
    try:
        relevant = await memory.search_relevant(user_message, limit=3)
        if relevant:
            lines = []
            for r in relevant:
                ts = r["timestamp"][:16]  # trim to minute
                lines.append(f"- [{ts}] {r['role']}: {r['content'][:300]}")
            sections.append("## Relevant Past Conversations\n" + "\n".join(lines))
    except Exception:
        pass  # memory not critical, don't block response

    # 4. Recent messages in this chat
    try:
        recent = await memory.get_recent(chat_id, limit=5)
        if recent:
            lines = []
            for r in recent:
                lines.append(f"- {r['role']}: {r['content'][:200]}")
            sections.append("## Recent Messages in This Chat\n" + "\n".join(lines))
    except Exception:
        pass

    # 5. Session summaries
    try:
        summaries = await memory.get_summaries(chat_id, limit=2)
        if summaries:
            lines = []
            for s in summaries:
                lines.append(f"- [{s['created_at'][:10]}] {s['summary'][:400]}")
            sections.append("## Session Summaries\n" + "\n".join(lines))
    except Exception:
        pass

    # 6. Recent bot-inbox notes (long-term memory across sessions)
    try:
        inbox = _recent_inbox_notes(days=7, limit=10)
        if inbox:
            lines = []
            for rel, head in inbox:
                # Strip frontmatter for preview
                body = head.split("---", 2)[-1].strip() if head.startswith("---") else head
                lines.append(f"### {rel}\n{body[:300]}")
            sections.append(
                "## Recent Bot-Inbox Notes (долгая память за 7 дней)\n"
                "Это то, что Максим просил запомнить ранее. Учитывай при ответах.\n\n"
                + "\n\n".join(lines)
            )
    except Exception:
        pass

    return "\n\n".join(sections)
