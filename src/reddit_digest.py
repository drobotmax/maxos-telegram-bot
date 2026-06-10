"""Content digest: fetch fresh posts from curated Substack feeds + GitHub
trending, rank, and annotate with Claude.

Replaced Reddit's anonymous JSON API (blocked with 403 on 31.05.26). Sources
are now Substack RSS (business/sales/growth/productivity newsletters) and the
GitHub Search API (trending repos by topic). The downstream pipeline — dedup,
preference filtering, Claude annotation, 👍/👎 feedback — is unchanged, so the
module keeps its REDDIT_*/rd: names to avoid rewiring scheduler/bot.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from .config import (
    BOT_TOKEN,
    ADMIN_TELEGRAM_ID,
    TIMEZONE,
    SUBSTACK_FEEDS,
    GITHUB_TOPICS,
    GITHUB_PER_TOPIC,
    CONTENT_RECENCY_DAYS,
    REDDIT_CATEGORY_LABELS,
    REDDIT_TOP_PER_CATEGORY,
    REDDIT_DEDUP_FILE,
    REDDIT_PREFS_FILE,
    REDDIT_FEEDBACK_FILE,
    REDDIT_DEDUP_DAYS,
)
from .claude_worker import process_scheduled

logger = logging.getLogger(__name__)
_tz = ZoneInfo(TIMEZONE)
_data_dir = Path(__file__).parent.parent / "data"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (MaxOS Content Digest)",
}


# --- Persistence helpers ---


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return {}


def _save_json(path: Path, data: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save {path}: {e}")


def _seen_path() -> Path:
    return _data_dir / REDDIT_DEDUP_FILE.split("/")[-1]


def _prefs_path() -> Path:
    return _data_dir / REDDIT_PREFS_FILE.split("/")[-1]


def _feedback_path() -> Path:
    return _data_dir / REDDIT_FEEDBACK_FILE.split("/")[-1]


def _load_seen() -> dict[str, str]:
    data = _load_json(_seen_path())
    cutoff = (datetime.now(_tz) - timedelta(days=REDDIT_DEDUP_DAYS)).isoformat()
    return {url: ts for url, ts in data.items() if ts > cutoff}


def _save_seen(urls: dict[str, str]):
    _save_json(_seen_path(), urls)


def load_preferences() -> dict:
    defaults = {
        "exclude_keywords": [],
        "liked_count": 0,
        "disliked_count": 0,
    }
    prefs = _load_json(_prefs_path())
    for k, v in defaults.items():
        prefs.setdefault(k, v)
    return prefs


def save_preferences(prefs: dict):
    _save_json(_prefs_path(), prefs)


# --- Source fetch (Substack RSS + GitHub trending) ---


def _strip_html(text: str) -> str:
    """Crude tag strip for RSS description previews."""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _feed_title(url: str) -> str:
    """Short human label for a feed, derived from its host."""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    host = host.removeprefix("www.")
    return host.split(".")[0]


async def _fetch_substack_feed(client: httpx.AsyncClient, url: str) -> list[dict]:
    """Fetch recent items from one RSS feed. Newer items rank higher."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=CONTENT_RECENCY_DAYS)
    label = _feed_title(url)
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning(f"Feed {label} returned {resp.status_code}")
            return []
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.warning(f"Failed to fetch/parse feed {label}: {e}")
        return []

    posts = []
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        if not link or not title:
            continue
        pub_raw = item.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub_raw)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if pub < cutoff:
            continue
        age_h = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
        author = (
            item.findtext("{http://purl.org/dc/elements/1.1/}creator")
            or item.findtext("author")
            or label
        ).strip()
        desc = _strip_html(item.findtext("description") or "")
        posts.append({
            "sub": label,
            "title": title,
            "url": link,
            "permalink": link,
            "author": author,
            "score": int(10000 - age_h),  # recency rank, newer = higher
            "comments": 0,
            "preview": desc[:150],
            "display": f"{int(age_h // 24)}d ago" if age_h >= 24 else f"{int(age_h)}h ago",
        })
    return posts


async def _fetch_github_topic(client: httpx.AsyncClient, topic: str, limit: int) -> list[dict]:
    """Fetch trending repos: newly created in the last month, ranked by stars gained."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    params = {
        "q": f"topic:{topic} created:>{since}",
        "sort": "stars",
        "order": "desc",
        "per_page": str(limit),
    }
    try:
        resp = await client.get(
            "https://api.github.com/search/repositories",
            params=params,
            headers={**_HEADERS, "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"GitHub topic:{topic} returned {resp.status_code}")
            return []
        items = resp.json().get("items", [])
    except Exception as e:
        logger.warning(f"Failed to fetch GitHub topic:{topic}: {e}")
        return []

    posts = []
    for repo in items:
        stars = repo.get("stargazers_count", 0)
        posts.append({
            "sub": "github",
            "title": f"{repo.get('full_name', '')} — {repo.get('description') or ''}".strip(" —"),
            "url": repo.get("html_url", ""),
            "permalink": repo.get("html_url", ""),
            "author": (repo.get("owner") or {}).get("login", ""),
            "score": stars,
            "comments": repo.get("open_issues_count", 0),
            "preview": (repo.get("description") or "")[:150],
            "display": f"★{stars}",
        })
    return posts


async def fetch_all_posts() -> dict[str, list[dict]]:
    """Fetch fresh posts from all sources, grouped by category."""
    result: dict[str, list[dict]] = {}
    async with httpx.AsyncClient() as client:
        for category, feeds in SUBSTACK_FEEDS.items():
            all_posts: list[dict] = []
            for feed_url in feeds:
                all_posts.extend(await _fetch_substack_feed(client, feed_url))
            result[category] = all_posts

        tech: list[dict] = []
        for topic in GITHUB_TOPICS:
            tech.extend(await _fetch_github_topic(client, topic, GITHUB_PER_TOPIC))
        # de-dupe repos that match multiple topics, keep highest-star
        seen_urls: dict[str, dict] = {}
        for p in tech:
            cur = seen_urls.get(p["url"])
            if cur is None or p["score"] > cur["score"]:
                seen_urls[p["url"]] = p
        result["tech_tools"] = list(seen_urls.values())
    return result


# --- Filter & rank ---


def filter_and_rank(
    posts_by_category: dict[str, list[dict]],
    seen_urls: dict[str, str],
    prefs: dict,
) -> dict[str, list[dict]]:
    """Filter out seen posts, apply preference rules, sort by score, pick top N."""
    exclude_kw = [kw.lower() for kw in prefs.get("exclude_keywords", [])]

    result = {}
    for category, posts in posts_by_category.items():
        filtered = []
        for p in posts:
            if p["url"] in seen_urls or p.get("permalink", "") in seen_urls:
                continue
            title_lower = p["title"].lower()
            if any(kw in title_lower for kw in exclude_kw):
                continue
            filtered.append(p)

        # Sort by score (highest first) instead of Reddit's position
        filtered.sort(key=lambda x: x["score"], reverse=True)
        result[category] = filtered[:REDDIT_TOP_PER_CATEGORY]

    return result


# --- Claude annotation ---


async def _annotate_posts(ranked: dict[str, list[dict]]) -> dict[str, str]:
    """Ask Claude to add short 'why read this' annotations. Returns {url: annotation}."""
    all_posts = []
    for posts in ranked.values():
        all_posts.extend(posts)

    if not all_posts:
        return {}

    posts_text = "\n".join(
        f"{i+1}. [{p['sub']}] {p['title']}"
        for i, p in enumerate(all_posts)
    )

    prompt = (
        f"Ты – AI-ассистент Максима Дробота (Sales Architect, AI-автоматизации, консалтинг).\n\n"
        f"Вот {len(all_posts)} материалов из newsletter'ов и GitHub:\n{posts_text}\n\n"
        f"Для каждого поста напиши ОДНУ строку (максимум 15 слов): "
        f"почему это стоит прочитать именно Максиму. "
        f"Привяжи к его контексту: AI-агенты, продажи, консалтинг, автоматизация, startup.\n\n"
        f"Формат строго:\n"
        f"1. [аннотация]\n"
        f"2. [аннотация]\n"
        f"...\n\n"
        f"Если пост не релевантен – напиши 'skip'.\n"
        f"Только короткое тире (–), никогда длинное (—).\n"
        f"Русский язык. Без пояснений, только список."
    )

    try:
        result = await process_scheduled(prompt, chat_id=ADMIN_TELEGRAM_ID)
        annotations = {}
        for line in result.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Parse "1. annotation" format
            parts = line.split(".", 1)
            if len(parts) == 2:
                try:
                    idx = int(parts[0].strip()) - 1
                    ann = parts[1].strip()
                    if 0 <= idx < len(all_posts) and ann.lower() != "skip":
                        annotations[all_posts[idx]["url"]] = ann
                except (ValueError, IndexError):
                    continue
        return annotations
    except Exception as e:
        logger.warning(f"Failed to annotate Reddit posts: {e}")
        return {}


# --- Format ---


def format_digest(ranked: dict[str, list[dict]], annotations: dict[str, str] | None = None) -> str:
    """Format ranked posts into a Telegram message with source + annotations."""
    now = datetime.now(_tz)
    lines = [f"📰 Контент-дайджест. {now.strftime('%d.%m.%Y')}\n"]

    total = 0
    for category, posts in ranked.items():
        if not posts:
            continue
        label = REDDIT_CATEGORY_LABELS.get(category, category)
        lines.append(f"\n{label}")
        for p in posts:
            total += 1
            meta = p.get("display") or ""
            line = f"{total}. [{p['sub']}] {p['title']}"
            if meta:
                line += f"\n   {meta}"

            ann = (annotations or {}).get(p["url"])
            if ann:
                line += f"\n   → {ann}"

            line += f"\n   {p['permalink']}"
            lines.append(line)

    if total == 0:
        return "📰 Контент-дайджест – сегодня ничего свежего не нашлось."

    lines.append(f"\n{total} материалов из подобранных источников")
    return "\n".join(lines)


def build_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 Норм", callback_data="rd:like"),
                InlineKeyboardButton("👎 Слабо", callback_data="rd:dislike"),
            ]
        ]
    )


# --- Feedback handler ---


def record_feedback(feedback_type: str):
    """Record a feedback event (like/dislike) with timestamp."""
    fb = _load_json(_feedback_path())
    if "events" not in fb:
        fb["events"] = []
    fb["events"].append({
        "type": feedback_type,
        "date": datetime.now(_tz).isoformat(),
    })
    fb["events"] = fb["events"][-100:]
    _save_json(_feedback_path(), fb)

    prefs = load_preferences()
    if feedback_type == "like":
        prefs["liked_count"] = prefs.get("liked_count", 0) + 1
    elif feedback_type == "dislike":
        prefs["disliked_count"] = prefs.get("disliked_count", 0) + 1
    save_preferences(prefs)


# --- Main entry point ---


async def build_reddit_digest() -> tuple[str, InlineKeyboardMarkup]:
    """Build the full Reddit digest. Returns (text, keyboard)."""
    logger.info("Building Reddit digest")

    seen = _load_seen()
    prefs = load_preferences()

    posts_by_cat = await fetch_all_posts()
    ranked = filter_and_rank(posts_by_cat, seen, prefs)

    # Claude annotation pass
    annotations = await _annotate_posts(ranked)

    text = format_digest(ranked, annotations)
    keyboard = build_feedback_keyboard()

    # Save seen URLs
    now_iso = datetime.now(_tz).isoformat()
    for posts in ranked.values():
        for p in posts:
            seen[p["url"]] = now_iso
            if p.get("permalink"):
                seen[p["permalink"]] = now_iso
    _save_seen(seen)

    return text, keyboard


async def send_reddit_digest():
    """Scheduled job: send Reddit digest to admin."""
    text, keyboard = await build_reddit_digest()
    bot = Bot(token=BOT_TOKEN)
    if len(text) <= 4096:
        await bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=text,
            reply_markup=keyboard,
        )
    else:
        for i in range(0, len(text), 4096):
            chunk = text[i : i + 4096]
            markup = keyboard if i + 4096 >= len(text) else None
            await bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=chunk,
                reply_markup=markup,
            )
