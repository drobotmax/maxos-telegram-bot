import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
from . import sessions, memory
from .context import build_system_append
from .config import MAXOS_DIR, CLAUDE_MAX_TURNS, CLAUDE_MODEL, CLAUDE_MODEL_DEEP, CLAUDE_MODEL_FAST, TIMEZONE
from .router import route
import shutil

CLAUDE_CLI_PATH = shutil.which("claude") or "/opt/homebrew/bin/claude"

logger = logging.getLogger(__name__)

_tz = ZoneInfo(TIMEZONE)

# Compaction threshold: summarize after this many messages
_SUMMARY_THRESHOLD = 20

# Heartbeat file — updated on every successful response
HEARTBEAT_FILE = Path(__file__).parent.parent / "data" / "heartbeat"

# Router log: structured per-request telemetry for future model routing
ROUTER_LOG = Path(__file__).parent.parent / "data" / "router-log.jsonl"


def _log_request(record: dict) -> None:
    """Append a structured request record to router-log.jsonl for analysis."""
    try:
        ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        record["ts"] = datetime.now(_tz).isoformat()
        with ROUTER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Router log write failed: {e}")

# Global lock: only ONE Claude CLI process at a time (prevents OOM on 1GB VPS)
_claude_lock = asyncio.Lock()

# Allowlist of tools the bot can use without prompting.
# Bot runs headless on VPS — no interactive user to approve prompts.
# Whitelist of senders is enforced upstream in bot.py / max_bot.py.
ALLOWED_TOOLS: list[str] = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Grep",
    "Glob",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "mcp__todoist",
    "mcp__google-workspace",
    "mcp__telegram-mcp",
    "mcp__whatsapp-mcp",
    "mcp__granola",
    "mcp__obsidian",
    "mcp__max-messenger",
    "mcp__notebooklm-mcp",
    "mcp__scheduled-tasks",
]


def _touch_heartbeat():
    """Write current timestamp to heartbeat file for monitoring."""
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(_tz).isoformat())
    except Exception:
        pass


def _time_prefix() -> str:
    now = datetime.now(_tz)
    return f"[Сейчас: {now.strftime('%Y-%m-%d %H:%M %Z')} ({now.strftime('%A')})]"


async def process(chat_id: int, message: str, chat_config: dict | None = None) -> str:
    """Send a message to Claude Code with memory-enriched context."""
    session_id = await sessions.get_session_id(chat_id)

    # Build rich system prompt append from memory + chat config
    if chat_config is None:
        chat_config = {}
    system_append = await build_system_append(chat_id, message, chat_config)

    t0 = time.monotonic()
    if _claude_lock.locked():
        logger.info(f"Claude queue: chat={chat_id} waiting (another task running)")

    async with _claude_lock:
        wait_time = time.monotonic() - t0
        if wait_time > 1:
            logger.info(f"Claude queue: chat={chat_id} waited {wait_time:.1f}s")
        logger.info(f"Claude request: chat={chat_id}, resume={session_id is not None}, append_len={len(system_append)}")

        try:
            result_text = None
            new_session_id = None
            async for msg in query(
                prompt=message,
                options=ClaudeAgentOptions(
                    resume=session_id,
                    permission_mode="acceptEdits",
                    allowed_tools=ALLOWED_TOOLS,
                    max_turns=CLAUDE_MAX_TURNS,
                    cwd=MAXOS_DIR,
                    model=route("interactive"),
                    cli_path=CLAUDE_CLI_PATH,
                    setting_sources=[],
                    stderr=lambda s: logger.warning(f"Claude CLI stderr: {s}"),
                    system_prompt={
                        "type": "preset",
                        "preset": "claude_code",
                        "append": system_append,
                    },
                ),
            ):
                if isinstance(msg, ResultMessage):
                    if msg.session_id:
                        new_session_id = msg.session_id
                        await sessions.save_session_id(chat_id, msg.session_id)
                    result_text = msg.result
                    elapsed = time.monotonic() - t0
                    cost = msg.total_cost_usd or 0
                    logger.info(
                        f"Claude result: chat={chat_id} cost=${cost:.4f} "
                        f"turns={msg.num_turns} time={elapsed:.1f}s "
                        f"resp_len={len(result_text or '')}"
                    )
                    _log_request({
                        "kind": "interactive",
                        "chat_id": chat_id,
                        "model": CLAUDE_MODEL,
                        "msg_len": len(message),
                        "append_len": len(system_append),
                        "resp_len": len(result_text or ""),
                        "turns": msg.num_turns,
                        "cost_usd": cost,
                        "time_s": round(elapsed, 2),
                        "resume": session_id is not None,
                    })

            response = result_text or "Не удалось получить ответ."
            _touch_heartbeat()

            # Save exchange to memory DB
            try:
                await memory.save_exchange(chat_id, message, response, new_session_id)
            except Exception as e:
                logger.warning(f"Memory save failed: {e}")

            return response

        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error(f"Claude error: chat={chat_id} error={e} time={elapsed:.1f}s")
            _log_request({
                "kind": "interactive",
                "chat_id": chat_id,
                "model": CLAUDE_MODEL,
                "msg_len": len(message),
                "append_len": len(system_append) if 'system_append' in locals() else 0,
                "time_s": round(elapsed, 2),
                "error": str(e),
            })
            return f"Ошибка: {e}"


async def process_scheduled(prompt: str, chat_id: int | None = None) -> str:
    """Run a scheduled task with optional drill-down support.

    If chat_id is provided, saves the session_id so that when the user
    replies to the scheduled message, handle_message() resumes the same
    Claude session — enabling drill-down questions.
    """
    prompt = f"{_time_prefix()}\n\n{prompt}"
    t0 = time.monotonic()
    if _claude_lock.locked():
        logger.info(f"Scheduled task queued (another task running)")

    async with _claude_lock:
        wait_time = time.monotonic() - t0
        if wait_time > 1:
            logger.info(f"Scheduled task waited {wait_time:.1f}s in queue")
        logger.info(f"Scheduled task: prompt_len={len(prompt)} chat_id={chat_id}")
        try:
            result_text = None
            async for msg in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    permission_mode="acceptEdits",
                    allowed_tools=ALLOWED_TOOLS,
                    max_turns=CLAUDE_MAX_TURNS,
                    cwd=MAXOS_DIR,
                    model=route("scheduled"),
                    cli_path=CLAUDE_CLI_PATH,
                    setting_sources=[],
                ),
            ):
                if isinstance(msg, ResultMessage):
                    result_text = msg.result
                    elapsed = time.monotonic() - t0
                    cost = msg.total_cost_usd or 0
                    logger.info(
                        f"Scheduled result: cost=${cost:.4f} "
                        f"time={elapsed:.1f}s resp_len={len(result_text or '')}"
                    )
                    _log_request({
                        "kind": "scheduled",
                        "chat_id": chat_id,
                        "model": CLAUDE_MODEL,
                        "msg_len": len(prompt),
                        "resp_len": len(result_text or ""),
                        "turns": msg.num_turns,
                        "cost_usd": cost,
                        "time_s": round(elapsed, 2),
                    })
                    # Save session for drill-down
                    if chat_id and msg.session_id:
                        await sessions.save_session_id(chat_id, msg.session_id)
                        logger.info(f"Scheduled task saved session for chat={chat_id}")

            _touch_heartbeat()
            return result_text or "Scheduled task вернул пустой результат."

        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error(f"Scheduled task error: {e} time={elapsed:.1f}s")
            _log_request({
                "kind": "scheduled",
                "chat_id": chat_id,
                "model": CLAUDE_MODEL,
                "msg_len": len(prompt),
                "time_s": round(elapsed, 2),
                "error": str(e),
            })
            return f"Ошибка scheduled task: {e}"


async def generate_summary(chat_id: int) -> str | None:
    """Generate a session summary using Claude and save it."""
    recent = await memory.get_recent(chat_id, limit=20)
    if len(recent) < 4:
        return None

    conversation = "\n".join(f"{r['role']}: {r['content'][:300]}" for r in recent)
    prompt = (
        f"Summarize this conversation in 2-3 sentences (in the language it was conducted in). "
        f"Focus on key topics, decisions, and action items:\n\n{conversation}"
    )

    async with _claude_lock:
        try:
            result = None
            async for msg in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    permission_mode="acceptEdits",
                    max_turns=3,
                    model=CLAUDE_MODEL,
                    cli_path=CLAUDE_CLI_PATH,
                    cwd=MAXOS_DIR,
                ),
            ):
                if isinstance(msg, ResultMessage):
                    result = msg.result

            if result:
                await memory.save_summary(chat_id, result, len(recent))
                logger.info(f"Summary generated for chat={chat_id}, msgs={len(recent)}")
                return result
        except Exception as e:
            logger.warning(f"Summary generation failed: {e}")
        return None
