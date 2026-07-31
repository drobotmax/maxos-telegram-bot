import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from .config import (
    BOT_TOKEN,
    ADMIN_TELEGRAM_ID,
    MAXOS_DIR,
    MORNING_CHECKIN_HOUR,
    DAILY_REPORT_HOUR,
    TIMEZONE,
    RESEARCH_DIGEST_HOUR,
    RESEARCH_DIGEST_MINUTE,
    REDDIT_DIGEST_HOUR,
    REDDIT_DIGEST_MINUTE,
    SKIP_INBOX_GROUP_NAMES,
    hub_dest,
)
from .claude_worker import process_scheduled
from .heartbeat import capture_morning_plan, load_state
from .reddit_digest import send_reddit_digest
from .hn_digest import send_hn_digest
from . import memory

_tz = ZoneInfo(TIMEZONE)
_maxos = Path(MAXOS_DIR)

logger = logging.getLogger(__name__)

_bot: Bot | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)
    return _bot


async def _send_to_admin(text: str, topic: str = "misc"):
    """Deliver a scheduled message to its MaxOS Hub topic (DM if unconfigured)."""
    if not ADMIN_TELEGRAM_ID:
        logger.warning("ADMIN_TELEGRAM_ID not set, skipping scheduled message")
        return
    bot = _get_bot()
    dest = hub_dest(topic)
    for i in range(0, len(text), 4096):
        await bot.send_message(text=text[i : i + 4096], **dest)


# --- Data pre-fetch helpers ---


def _read_file(path: Path, max_chars: int = 3000) -> str:
    """Read a file safely, return empty string on failure."""
    try:
        if path.exists():
            text = path.read_text().strip()
            return text[:max_chars] if text else ""
    except Exception as e:
        logger.warning(f"Failed to read {path}: {e}")
    return ""


def _parse_actions(raw: str) -> str:
    """Extract open action items table from actions-tracker.md."""
    if not raw:
        return "Нет данных"
    lines = raw.split("\n")
    in_open = False
    items = []
    for line in lines:
        if "Открытые" in line or "## Open" in line.lower():
            in_open = True
            continue
        if in_open and line.startswith("##"):
            break
        if in_open and line.startswith("|") and not line.startswith("| #") and not line.startswith("|---"):
            items.append(line.strip())
    return "\n".join(items) if items else "Нет открытых задач"


def _parse_handoff(raw: str) -> str:
    """Extract key info from session-handoff: what was done + blockers."""
    if not raw:
        return "Нет данных"
    # Take first 1500 chars – Claude will synthesize
    return raw[:1500]


def _parse_projects(raw: str) -> str:
    """Extract active projects table from _index.md."""
    if not raw:
        return "Нет данных"
    lines = raw.split("\n")
    in_active = False
    items = []
    for line in lines:
        if "Активные" in line or "Active" in line:
            in_active = True
            continue
        if in_active and line.startswith("##"):
            break
        if in_active and line.startswith("|") and not line.startswith("|---"):
            items.append(line.strip())
    return "\n".join(items) if items else raw[:800]


# --- Health Check (silent – only alerts on failure) ---


async def _health_check():
    """Run health checks silently. Only alert admin if something is broken."""
    issues = []
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "whatsapp-bridge"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() != "active":
            issues.append("WhatsApp Bridge down")
            subprocess.run(
                ["sudo", "systemctl", "restart", "whatsapp-bridge"],
                capture_output=True, timeout=10,
            )
            issues[-1] += " (restarted)"
    except Exception as e:
        issues.append(f"WhatsApp check failed: {e}")

    try:
        r = subprocess.run(
            ["curl", "-s", "http://localhost:8080/api/status"],
            capture_output=True, text=True, timeout=10,
        )
        status = json.loads(r.stdout) if r.stdout.strip() else {}
        if not status.get("connected"):
            issues.append("WhatsApp disconnected (needs QR re-scan)")
        elif not status.get("logged_in"):
            issues.append("WhatsApp not logged in (needs QR re-scan)")
    except json.JSONDecodeError:
        issues.append("WhatsApp API responded but status unreadable")
    except Exception as e:
        issues.append(f"WhatsApp API check failed: {e}")

    try:
        # Detect a stalled Mac→GitHub→VPS pipe. Mac auto-backup commits every 2h,
        # so if the newest commit in the VPS repo is >8h old, the sync is frozen
        # (e.g. a silent push failure on the Mac side froze the GitHub mirror
        # 25-30.05.26, leaving the bot reading 5-day-old data).
        r = subprocess.run(
            ["git", "-C", str(Path.home() / "MaxOS"), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        )
        ts = r.stdout.strip()
        if r.returncode == 0 and ts.isdigit():
            commit_age_h = (datetime.now().timestamp() - int(ts)) / 3600
            if commit_age_h > 8:
                issues.append(f"MaxOS git sync stale ({commit_age_h:.0f}h) – check Mac push")
    except Exception:
        pass

    if issues:
        # Log only – no admin broadcast. This infra alert carried no actionable
        # value (WhatsApp/git status already surface in the SessionStart hook).
        logger.warning(f"Health issues: {issues}")
    else:
        logger.info("Health check passed")


# --- Briefing builders (reused by scheduler + bot commands) ---


def _prefetch_data() -> dict:
    """Pre-read all MaxOS data sources. Returns dict of parsed sections."""
    # Todoist tasks (primary) with fallback to legacy actions-tracker
    todoist_raw = _read_file(_maxos / "store" / "todoist-tasks.md", max_chars=3000)
    if not todoist_raw or "API token не настроен" in todoist_raw:
        todoist_raw = _parse_actions(_read_file(_maxos / "store" / "actions-tracker.md"))
    handoff_raw = _read_file(_maxos / "store" / "session-handoff.md")
    projects_raw = _read_file(_maxos / "projects" / "_index.md")
    meetings_raw = _read_file(_maxos / "store" / "meetings-context.md", max_chars=1500)
    email_raw = _read_file(_maxos / "store" / "email-digest.md", max_chars=2000)
    calendar_raw = _read_file(_maxos / "store" / "calendar-today.md", max_chars=1000)

    now = datetime.now(_tz)
    hour = now.hour
    if hour < 12:
        greeting = "Доброе утро"
    elif hour < 17:
        greeting = "Добрый день"
    else:
        greeting = "Добрый вечер"

    return {
        "tasks": todoist_raw,
        "handoff": _parse_handoff(handoff_raw),
        "projects": _parse_projects(projects_raw),
        "meetings": meetings_raw or "",
        "email": email_raw or "",
        "calendar": calendar_raw or "",
        "date_str": now.strftime("%d %b %Y, %A"),
        "greeting": greeting,
    }


def _build_morning_prompt(data: dict) -> str:
    return (
        f"Ты – личный ассистент Максима. Тон: как толковый EA, кратко и по делу. "
        f"Не мотивашка, не корпоративный отчёт. Факты и действия.\n\n"
        f"Дата: {data['date_str']}\n\n"
        f"=== ДАННЫЕ ===\n\n"
        f"--- Session Handoff (прошлая сессия) ---\n{data['handoff']}\n\n"
        f"--- Todoist Tasks ---\n{data['tasks']}\n\n"
        f"--- Active Projects ---\n{data['projects']}\n\n"
        f"--- Recent Meetings ---\n{data['meetings'] or 'Нет данных'}\n\n"
        f"--- Calendar Today ---\n{data['calendar'] or 'Нет данных'}\n\n"
        f"--- Email Digest ---\n{data['email'] or 'Нет данных'}\n\n"
        f"=== ЗАДАЧА ===\n\n"
        f"На основе ВСЕХ данных выше, сформируй утренний брифинг.\n"
        f"Формат строго такой (одно сообщение, один экран телефона):\n\n"
        f"{data['greeting']}. [дата]\n\n"
        f"ТОП-3 ПРИОРИТЕТА\n"
        f"1. [проект/контекст] → [конкретное следующее действие]\n"
        f"2. [проект/контекст] → [следующее действие]\n"
        f"3. [проект/контекст] → [следующее действие]\n\n"
        f"ПРАВИЛА:\n"
        f"- Только короткое тире (–), никогда длинное (—)\n"
        f"- Каждый пункт: 1 строка максимум\n"
        f"- Имена, цифры, даты – конкретно. Никаких 'продолжить работу над проектом'\n"
        f"- ТОП-3 берётся ТОЛЬКО из предоставленных данных. НЕ выдумывай задачи.\n"
        f"- НЕ включай секцию РАСПИСАНИЕ – Максим видит календарь сам\n"
        f"- НЕ включай секцию OPEN ITEMS – просроченные задачи видны в Todoist\n"
        f"- Если данных нет – пропусти секцию, не пиши 'нет данных'\n"
        f"- НИКАКИХ ошибок, стектрейсов, системных сообщений в ответе\n"
        f"- Если MCP/файл недоступен – молча пропусти\n"
        f"- Максимум 800 символов. Лаконичность важнее полноты\n"
        f"- Последняя строка: 💬 Дозадай вопросы – я в контексте"
    )


def _build_report_prompt(data: dict) -> str:
    return (
        f"Ты – личный ассистент Максима. Тон: кратко, по делу.\n\n"
        f"Дата: {data['date_str']}\n\n"
        f"=== ДАННЫЕ ===\n\n"
        f"--- Session Handoff ---\n{data['handoff']}\n\n"
        f"--- Todoist Tasks ---\n{data['tasks']}\n\n"
        f"--- Projects ---\n{data['projects']}\n\n"
        f"--- Meetings ---\n{data['meetings'] or 'Нет данных'}\n\n"
        f"--- Calendar Today ---\n{data['calendar'] or 'Нет данных'}\n\n"
        f"--- Email Digest ---\n{data['email'] or 'Нет данных'}\n\n"
        f"=== ЗАДАЧА ===\n\n"
        f"Сформируй вечерний отчёт ИЗ ТОГО, ЧТО ЕСТЬ в данных выше. Формат:\n\n"
        f"Итоги дня. [дата]\n\n"
        f"СДЕЛАНО\n"
        f"[что закрыто/продвинуто – конкретно]\n\n"
        f"НА ЗАВТРА\n"
        f"1. [приоритет] → [действие]\n"
        f"2. [приоритет] → [действие]\n"
        f"3. [приоритет] → [действие]\n\n"
        f"БЛОКЕРЫ\n"
        f"[если есть, иначе пропусти секцию]\n\n"
        f"ПРАВИЛА:\n"
        f"- Только короткое тире (–)\n"
        f"- Конкретика: имена, цифры, даты\n"
        f"- Пропусти пустые секции\n"
        f"- НИКАКИХ ошибок/системных сообщений\n"
        f"- Максимум 1500 символов\n"
        f"- КРИТИЧНО: НИКОГДА не проси у Максима контекст и не пиши мета-сообщений "
        f"(«контекст устарел», «дай контекст», «дозадай», «я не в контексте», «нужны данные»). "
        f"Твоя задача – отчёт из имеющихся данных, а не запрос данных у пользователя.\n"
        f"- Session Handoff может быть устаревшим – это нормально, используй как фон и НЕ комментируй его свежесть. "
        f"«НА ЗАВТРА» бери из Todoist/Calendar/Projects, даже если handoff старый.\n"
        f"- Если по «СДЕЛАНО» нет свежих данных – просто пропусти секцию и дай «НА ЗАВТРА». "
        f"Минимальный отчёт из Todoist+Calendar лучше, чем просьба о контексте.\n"
        f"- Последняя строка: 💬 Дозадай вопросы – я в контексте"
    )


async def build_morning_briefing(chat_id: int | None = None) -> str:
    """Build and run morning briefing. Returns result text.

    Used by both scheduler (morning_checkin) and /checkin command.
    """
    data = _prefetch_data()
    prompt = _build_morning_prompt(data)
    return await process_scheduled(prompt, chat_id=chat_id)


async def build_daily_report(chat_id: int | None = None) -> str:
    """Build and run daily report. Returns result text.

    Used by both scheduler (daily_report) and /report command.
    """
    data = _prefetch_data()
    prompt = _build_report_prompt(data)
    return await process_scheduled(prompt, chat_id=chat_id)


# --- Scheduled jobs (call builders + send to admin) ---


async def morning_checkin():
    # Routine LLM briefing disabled per user request (29.05.26): the Mac
    # morning-briefing.py already sends a dashboard-updated ping, and /checkin
    # remains available on-demand. This job now only runs silent monitoring:
    # health-check alerts (sent only on problems) + morning-plan snapshot for
    # drift detection.
    logger.info("Running morning health-check + plan snapshot")
    await _health_check()
    try:
        state = load_state()
        capture_morning_plan(state)
    except Exception as e:
        logger.warning(f"Failed to capture morning plan: {e}")


async def daily_report():
    logger.info("Running daily report")
    result = await build_daily_report(chat_id=ADMIN_TELEGRAM_ID)
    await _send_to_admin(result, topic="morning")


# --- Research Digest (HN + Lobsters, replaces old WebSearch approach) ---


async def research_digest():
    """Scheduled job: HN + Lobsters tech digest."""
    logger.info("Running HN + Lobsters digest")
    await send_hn_digest()


async def memory_garbage_collect():
    """Nightly cleanup of stale memories."""
    logger.info("Running memory garbage collection")
    stats = await memory.garbage_collect()
    total_deleted = stats["hard_deleted"] + stats["decay_deleted"]
    if total_deleted > 0:
        logger.info(
            f"Memory GC cleaned {total_deleted} records "
            f"({stats['total_remaining']} remaining)"
        )


async def build_inbox_check(chat_id: int | None = None) -> str:
    """On-demand inbox check (called from /inbox command)."""
    logger.info("Running on-demand inbox check")
    now = datetime.now(_tz)
    today_str = now.strftime("%Y-%m-%d %H:%M")

    # Pre-read Telegram unreads (synced from Mac)
    tg_unreads = _read_file(_maxos / "store" / "telegram-unreads.md", max_chars=2000)

    prompt = (
        f"Сейчас {today_str}. Проверь ВСЕ каналы на непрочитанные сообщения.\n\n"
        f"=== WhatsApp (live) ===\n"
        f"Используй WhatsApp MCP:\n"
        f"1. list_chats – найди чаты с недавней активностью (последние 3 часа)\n"
        f"2. list_messages – для каждого такого чата прочитай последние сообщения\n"
        f"3. Определи, нужен ли мой ответ (если последнее от меня – пропусти)\n\n"
        f"=== Telegram (pre-fetched) ===\n"
        f"{tg_unreads or 'Нет данных (Mac sync не прошёл)'}\n\n"
        f"Для каждого неотвеченного сообщения составь черновик ответа.\n\n"
        f"ПРАВИЛА:\n"
        f"- НЕ отправляй ответы, только покажи черновики\n"
        f"- Русский для русскоязычных, English для англоязычных\n"
        f"- Короткое тире (–), не длинное (—)\n"
        f"- Учитывай контекст: pipeline.json, projects/, comms/\n"
        f"- Если контакт из pipeline.json – учти контекст сделки\n"
        f"- Для YOLO контактов – прочитай projects/yolo.md\n"
        f"- Черновик: 2-3 предложения, конкретно, профессионально\n"
        f"- Пропускай спам, рассылки, ботов\n"
        f"- ПРОПУСКАЙ эти группы (не анализируй): {', '.join(SKIP_INBOX_GROUP_NAMES)}\n\n"
        f"ФОРМАТ:\n"
        f"📩 [Имя] ([WhatsApp/Telegram])\n"
        f"Сообщение: [краткое содержание]\n"
        f"💬 Черновик: [ответ]\n"
        f"---\n\n"
        f"Если нет неотвеченных: ✅ Все каналы чисты"
    )

    return await process_scheduled(prompt, chat_id=chat_id)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    # Morning briefing
    scheduler.add_job(
        morning_checkin,
        CronTrigger(hour=MORNING_CHECKIN_HOUR, minute=0, timezone=_tz),
        id="morning_checkin",
        name="Morning Briefing",
    )

    # AI Research Digest (Mon + Thu only)
    scheduler.add_job(
        research_digest,
        CronTrigger(
            day_of_week="mon,thu",
            hour=RESEARCH_DIGEST_HOUR,
            minute=RESEARCH_DIGEST_MINUTE,
            timezone=_tz,
        ),
        id="research_digest",
        name="AI Research Digest (Mon+Thu)",
    )

    # Reddit Digest (Saturday only)
    scheduler.add_job(
        send_reddit_digest,
        CronTrigger(
            day_of_week="sat",
            hour=REDDIT_DIGEST_HOUR,
            minute=REDDIT_DIGEST_MINUTE,
            timezone=_tz,
        ),
        id="reddit_digest",
        name="Reddit Digest (Sat)",
    )

    # Evening report
    scheduler.add_job(
        daily_report,
        CronTrigger(hour=DAILY_REPORT_HOUR, minute=0, timezone=_tz),
        id="daily_report",
        name="Daily Report",
    )

    # Nightly memory garbage collection (03:00)
    scheduler.add_job(
        memory_garbage_collect,
        CronTrigger(hour=3, minute=0, timezone=_tz),
        id="memory_gc",
        name="Memory GC",
    )

    # Heartbeat disabled – morning briefing covers calendar overview,
    # pipeline has its own 10:00 job, and frequent heartbeat alerts were noise.

    return scheduler
