import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _ids_from_env(name: str) -> set[int]:
    """Parse a comma-separated list of integer ids from an env var."""
    ids: set[int] = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                pass
    return ids


def _list_from_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "")
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items or default

# --- Telegram ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))

# --- MAX messenger ---
# Bot token from dev.max.ru (Authorization header, no "Bearer" prefix)
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
# Maxim's user_id in MAX
MAX_ADMIN_USER_ID = int(os.getenv("MAX_ADMIN_USER_ID", "0"))
# Namespace offset to keep MAX chat ids separate from Telegram in shared sessions DB
MAX_CHAT_ID_OFFSET = 10**15

# --- Claude ---
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
# Router tiers (router.py): DEEP = substantive chat/briefings, FAST = scheduled/simple.
# Restored 31.05.26 after a stale-local deploy clobbered these on the VPS.
CLAUDE_MODEL_DEEP = os.getenv("CLAUDE_MODEL_DEEP", CLAUDE_MODEL)
CLAUDE_MODEL_FAST = os.getenv("CLAUDE_MODEL_FAST", "claude-haiku-4-5-20251001")

MAXOS_DIR = os.getenv("MAXOS_DIR", os.path.expanduser("~/MaxOS"))
CLAUDE_MAX_TURNS = 25

# --- Security ---
# Admin + extra trusted contacts (comma-separated Telegram user ids in env)
ALLOWED_USERS: set[int] = {ADMIN_TELEGRAM_ID} | _ids_from_env("ALLOWED_USER_IDS")
ADMIN_USERS: set[int] = {ADMIN_TELEGRAM_ID}

# MAX messenger whitelist (separate id space from Telegram)
MAX_ALLOWED_USERS: set[int] = {MAX_ADMIN_USER_ID} if MAX_ADMIN_USER_ID else set()
MAX_ADMIN_USERS: set[int] = {MAX_ADMIN_USER_ID} if MAX_ADMIN_USER_ID else set()

RATE_LIMIT_MESSAGES = 10      # per user
RATE_LIMIT_WINDOW_SEC = 60    # per window

# --- File handling ---
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5MB

# --- Todoist ---
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "")
TODOIST_PROJECT_ID = os.getenv("TODOIST_PROJECT_ID", "")

# --- Pipeline ---
# --- Inbox filter: skip these groups in unreplied/inbox analysis ---
# Comma-separated group names in env (SKIP_INBOX_GROUPS)
SKIP_INBOX_GROUP_NAMES = _list_from_env("SKIP_INBOX_GROUPS", [])

# --- Per-chat rules ---
# auto_respond=True → answer all messages
# auto_respond=False → only @mention in groups
# Personal per-chat rules live in data/chat-rules.json (gitignored):
#   {"default_dm": {...}, "<chat_id>": {"lang": ..., "auto_respond": ..., "context": ...}}
CHAT_RULES: dict = {
    "default_dm": {
        "lang": "ru",
        "auto_respond": True,
        "context": "Ты - личный AI-ассистент владельца бота. Полный доступ ко всем проектам и инструментам.",
    },
}

_CHAT_RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "chat-rules.json"
if _CHAT_RULES_FILE.exists():
    try:
        for _key, _rule in json.loads(_CHAT_RULES_FILE.read_text()).items():
            CHAT_RULES[_key if _key == "default_dm" else int(_key)] = _rule
    except (ValueError, OSError) as _e:
        print(f"config: failed to load {_CHAT_RULES_FILE}: {_e}")

# --- Schedule ---
TIMEZONE = "Europe/Moscow"
MORNING_CHECKIN_HOUR = 7
DAILY_REPORT_HOUR = 18
EMAIL_CHECK_HOURS = [6, 18]  # check emails at 06:00 and 18:00
CALENDAR_SCAN_HOURS = [6, 21]  # scan calendar at 06:00 and 21:00

# --- Research Digest ---
RESEARCH_DIGEST_HOUR = 6
RESEARCH_DIGEST_MINUTE = 15
RESEARCH_TOPICS = [
    {"id": "ai_sales", "query": "AI sales automation case study results ROI 2026", "label": "AI + продажи"},
    {"id": "ai_agents", "query": "autonomous AI agents production deployment lessons learned 2026", "label": "AI-агенты"},
    {"id": "ai_research", "query": "LLM benchmark research paper new model release 2026", "label": "Исследования"},
    {"id": "ai_consulting", "query": "AI consulting revenue automation solopreneur case study 2026", "label": "Консалтинг"},
    {"id": "ai_engineering", "query": "Claude MCP API workflow automation developer experience 2026", "label": "AI-инженерия"},
]
RESEARCH_DEDUP_FILE = "data/research-seen-urls.json"
RESEARCH_DEDUP_DAYS = 7

# --- HN + Lobsters Digest (replaces old WebSearch research) ---
HN_MIN_SCORE = 50  # only stories with 50+ upvotes
HN_MAX_STORIES = 7  # top N after filtering
HN_LOBSTERS_ENABLED = True
HN_KEYWORDS = [
    {
        "label": "AI/LLM",
        "keywords": [
            "llm", "gpt", "claude", "gemini", "openai", "anthropic",
            "language model", "transformer", "fine-tun", "rag",
            "embedding", "vector", "neural", "deep learning",
            "machine learning", "ai agent", "ai tool",
            "copilot", "chatbot", "generative ai",
        ],
    },
    {
        "label": "Agents",
        "keywords": [
            "agent", "mcp", "tool use", "function call",
            "autonomous", "agentic", "multi-agent",
            "orchestrat", "workflow automat",
        ],
    },
    {
        "label": "Sales/Marketing",
        "keywords": [
            "outreach", "cold email", "sales automat",
            "lead gen", "crm", "pipeline", "conversion",
            "b2b", "saas", "revenue", "growth",
            "marketing automat", "personali",
        ],
    },
    {
        "label": "Engineering",
        "keywords": [
            "api", "sdk", "python", "typescript", "rust",
            "self-host", "open source", "cli", "terminal",
            "devops", "deploy", "infra", "postgres",
            "sqlite", "redis", "docker", "serverless",
        ],
    },
    {
        "label": "Startup",
        "keywords": [
            "startup", "founder", "indie", "solopreneur",
            "bootstrap", "side project", "launch",
            "pricing", "monetiz", "consulting",
            "freelanc", "one-person",
        ],
    },
]

# --- Content Digest (Substack RSS + GitHub trending; replaced Reddit JSON 31.05.26
#     after Reddit blocked anonymous JSON with 403). Pipeline keeps REDDIT_* names. ---
REDDIT_DIGEST_HOUR = 12
REDDIT_DIGEST_MINUTE = 0

# Curated feeds, verified active 31.05.26. Add/remove URLs freely — fetch is generic.
SUBSTACK_FEEDS = {
    "sales_gtm": [
        "https://www.saastr.com/feed/",
        "https://www.gtmnow.com/feed",
    ],
    "growth_marketing": [
        "https://www.lennysnewsletter.com/feed",
        "https://www.elenaverna.com/feed",
    ],
    "productivity": [
        "https://www.densediscovery.com/feed",
    ],
}
# GitHub trending repos by topic → tech_tools category (Search API, no auth needed)
GITHUB_TOPICS = ["llm", "ai-agents", "developer-tools"]

REDDIT_CATEGORY_LABELS = {
    "sales_gtm": "💼 Sales & GTM",
    "growth_marketing": "📈 Growth & Marketing",
    "productivity": "🧠 Productivity",
    "tech_tools": "🤖 Tech & Tools",
}
CONTENT_RECENCY_DAYS = 7      # newsletter cadence (weekly+) — surface items newer than this
GITHUB_PER_TOPIC = 3
REDDIT_TOP_PER_CATEGORY = 3
REDDIT_DEDUP_FILE = "data/reddit-seen-urls.json"
REDDIT_PREFS_FILE = "data/reddit-preferences.json"
REDDIT_FEEDBACK_FILE = "data/reddit-feedback.json"
REDDIT_DEDUP_DAYS = 7

# --- Heartbeat Monitor ---
HEARTBEAT_STATE_FILE = "data/heartbeat-state.json"
HEARTBEAT_CALENDAR_ALERT_MINUTES = 60   # alert if event within N min
HEARTBEAT_STALE_SYNC_HOURS = 6          # alert if Mac sync older than N hours
# Comma-separated overrides in env (HEARTBEAT_VIP_KEYWORDS): project/client names etc.
HEARTBEAT_VIP_KEYWORDS = _list_from_env("HEARTBEAT_VIP_KEYWORDS", [
    "urgent", "срочно", "asap", "invoice", "payment",
    "встреча", "звонок", "call",
])


def get_chat_config(chat_id: int, is_private: bool = False) -> dict:
    if is_private:
        return CHAT_RULES["default_dm"]
    return CHAT_RULES.get(chat_id, {
        "lang": "ru",
        "auto_respond": False,
        "context": "Неизвестная группа.",
    })
