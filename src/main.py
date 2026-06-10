import asyncio
import logging
import signal
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from . import sessions, memory, max_bridge_bot, tg_bridge
from .bot import create_bot
from .scheduler import create_scheduler
from .config import BOT_TOKEN, ADMIN_TELEGRAM_ID, MAX_BOT_TOKEN
from .max_bot import run_max_bot


def setup_logging():
    log_dir = Path(__file__).parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def _run_all():
    logger = logging.getLogger(__name__)

    await sessions.init_db()
    await memory.init_db()
    logger.info("Session and memory DBs initialized")

    app = create_bot()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    logger.info("Telegram polling started")

    scheduler = create_scheduler()
    scheduler.start()
    logger.info(f"Scheduler started: {len(scheduler.get_jobs())} jobs")

    max_task: asyncio.Task | None = None
    if MAX_BOT_TOKEN:
        max_task = asyncio.create_task(run_max_bot(), name="max-bot")
        logger.info("MAX bot task created")
    else:
        logger.info("MAX_BOT_TOKEN not set — MAX transport disabled")

    # TG<->MAX bridge: Telethon userbot forwards incoming TG to MAX;
    # replies in MAX go back to TG via tg_bridge callbacks registered here.
    # run_tg_bridge exits quietly if the session/env is not configured.
    max_bridge_bot.configure(tg_bridge.send_to_tg, tg_bridge.resolve_chat)
    tg_bridge_task = asyncio.create_task(
        tg_bridge.run_tg_bridge(max_bridge_bot.notify_max), name="tg-bridge"
    )
    logger.info("TG bridge task created")

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Stop signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        if not tg_bridge_task.done():
            tg_bridge_task.cancel()
            try:
                await tg_bridge_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await max_bridge_bot.aclose()
        except Exception:
            pass
        if max_task and not max_task.done():
            max_task.cancel()
            try:
                await max_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.warning(f"Telegram shutdown warning: {e}")
        logger.info("Stopped")


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        logger.error("TELEGRAM_BOT_TOKEN not configured in .env")
        sys.exit(1)
    if not ADMIN_TELEGRAM_ID:
        logger.error("ADMIN_TELEGRAM_ID not configured in .env")
        sys.exit(1)

    logger.info("MaxOS Bot starting (Telegram + MAX)...")
    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
