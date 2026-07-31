"""HN + Lobsters digest: fetch top stories, filter by keywords, send to Telegram.

Pure Python - no Claude calls. Uses public APIs:
- HN: https://hacker-news.firebaseio.com/v0/
- Lobsters: https://lobste.rs/hottest.json
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import (
    BOT_TOKEN,
    ADMIN_TELEGRAM_ID,
    hub_dest,
    TIMEZONE,
    RESEARCH_DEDUP_FILE,
    RESEARCH_DEDUP_DAYS,
    HN_KEYWORDS,
    HN_MIN_SCORE,
    HN_MAX_STORIES,
    HN_LOBSTERS_ENABLED,
)

logger = logging.getLogger(__name__)
_tz = ZoneInfo(TIMEZONE)
_data_dir = Path(__file__).parent.parent / "data"
_dedup_path = _data_dir / RESEARCH_DEDUP_FILE.split("/")[-1]


# --- Dedup ---


def _load_seen() -> dict[str, str]:
    try:
        if _dedup_path.exists():
            data = json.loads(_dedup_path.read_text())
            cutoff = (datetime.now(_tz) - timedelta(days=RESEARCH_DEDUP_DAYS)).isoformat()
            return {url: ts for url, ts in data.items() if ts > cutoff}
    except Exception as e:
        logger.warning(f"Failed to load research dedup: {e}")
    return {}


def _save_seen(urls: dict[str, str]):
    try:
        _dedup_path.parent.mkdir(parents=True, exist_ok=True)
        _dedup_path.write_text(json.dumps(urls, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save research dedup: {e}")


# --- HN API ---


async def _fetch_hn_top_ids(client: httpx.AsyncClient, limit: int = 200) -> list[int]:
    """Fetch top story IDs from HN."""
    resp = await client.get(
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()[:limit]


async def _fetch_hn_item(client: httpx.AsyncClient, item_id: int) -> dict | None:
    """Fetch a single HN item."""
    try:
        resp = await client.get(
            f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


async def _fetch_hn_stories(client: httpx.AsyncClient, limit: int = 200) -> list[dict]:
    """Fetch top HN stories with metadata."""
    ids = await _fetch_hn_top_ids(client, limit)
    stories = []
    # Fetch in batches of 30 to avoid overwhelming the API
    for i in range(0, len(ids), 30):
        batch = ids[i:i + 30]
        import asyncio
        items = await asyncio.gather(
            *[_fetch_hn_item(client, sid) for sid in batch]
        )
        for item in items:
            if item and item.get("type") == "story" and item.get("score", 0) >= HN_MIN_SCORE:
                stories.append({
                    "source": "HN",
                    "title": item.get("title", ""),
                    "url": item.get("url", f"https://news.ycombinator.com/item?id={item['id']}"),
                    "hn_url": f"https://news.ycombinator.com/item?id={item['id']}",
                    "score": item.get("score", 0),
                    "comments": item.get("descendants", 0),
                    "time": item.get("time", 0),
                })
    return stories


# --- Lobsters API ---


async def _fetch_lobsters(client: httpx.AsyncClient) -> list[dict]:
    """Fetch hottest stories from Lobsters."""
    try:
        resp = await client.get(
            "https://lobste.rs/hottest.json",
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
        items = resp.json()
        stories = []
        for item in items:
            score = item.get("score", 0)
            if score < 5:
                continue
            stories.append({
                "source": "Lobsters",
                "title": item.get("title", ""),
                "url": item.get("url") or item.get("short_id_url", ""),
                "hn_url": item.get("short_id_url", ""),
                "score": score,
                "comments": item.get("comment_count", 0),
                "tags": item.get("tags", []),
                "time": 0,
            })
        return stories
    except Exception as e:
        logger.warning(f"Failed to fetch Lobsters: {e}")
        return []


# --- Filter & rank ---


def _matches_keywords(title: str, keywords: list[dict]) -> str | None:
    """Check if title matches any keyword group. Returns label or None."""
    title_lower = title.lower()
    for group in keywords:
        for kw in group["keywords"]:
            if kw.lower() in title_lower:
                return group["label"]
    return None


def _filter_and_rank(
    stories: list[dict],
    seen_urls: dict[str, str],
    keywords: list[dict],
    max_results: int,
) -> list[dict]:
    """Filter by keywords, dedup, sort by score, return top N."""
    matched = []
    for s in stories:
        if s["url"] in seen_urls or s.get("hn_url", "") in seen_urls:
            continue
        label = _matches_keywords(s["title"], keywords)
        if label:
            s["label"] = label
            matched.append(s)

    # Sort by score descending
    matched.sort(key=lambda x: x["score"], reverse=True)
    return matched[:max_results]


# --- TL;DR enrichment (one batch Claude SDK call) ---


_LABEL_EMOJI = {
    "AI/LLM": "🤖",
    "Agents": "🧩",
    "Sales/Marketing": "📈",
    "Engineering": "🛠",
}


async def _enrich_tldrs(stories: list[dict]) -> None:
    """Mutate stories in-place: add 'tldr' field (1-2 sentences in RU) via Claude SDK.

    Single batch call. On failure, stories get no tldr and format falls back to title-only.
    """
    if not stories:
        return

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
    except Exception as e:
        logger.warning(f"claude_agent_sdk unavailable, skipping TL;DR: {e}")
        return

    from .config import CLAUDE_MODEL, MAXOS_DIR
    import shutil
    cli_path = shutil.which("claude") or "/opt/homebrew/bin/claude"

    items = [
        {"i": i, "title": s["title"], "url": s["url"], "label": s.get("label", "")}
        for i, s in enumerate(stories)
    ]
    prompt = (
        "Для каждой новости ниже напиши TL;DR на русском: 1-2 коротких предложения - "
        "что это и почему интересно. Без воды, без англицизмов где можно. "
        "Используй знание домена (имена проектов, компаний, технологий). "
        "Ответ строго в формате JSON: массив объектов {\"i\": N, \"tldr\": \"...\"}. "
        "Без обёртки, без markdown, без комментариев. Только JSON-массив.\n\n"
        f"Новости:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    )

    result_text = None
    try:
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                permission_mode="acceptEdits",
                allowed_tools=[],
                max_turns=1,
                cwd=MAXOS_DIR,
                model=CLAUDE_MODEL,
                cli_path=cli_path,
                setting_sources=[],
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result
    except Exception as e:
        logger.warning(f"TL;DR enrichment failed: {e}")
        return

    if not result_text:
        return

    # Strip possible markdown fence
    txt = result_text.strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        if txt.endswith("```"):
            txt = txt.rsplit("```", 1)[0]
        txt = txt.strip()

    try:
        parsed = json.loads(txt)
        for entry in parsed:
            idx = entry.get("i")
            tldr = entry.get("tldr", "").strip()
            if isinstance(idx, int) and 0 <= idx < len(stories) and tldr:
                stories[idx]["tldr"] = tldr
    except Exception as e:
        logger.warning(f"Failed to parse TL;DR JSON: {e}; raw={result_text[:200]}")


# --- Format ---


def format_digest(stories: list[dict]) -> str:
    """Format stories into a Telegram message, grouped by label with TL;DR."""
    now = datetime.now(_tz)
    if not stories:
        return f"🔬 Tech Digest {now.strftime('%d.%m.%Y')} - сегодня ничего релевантного."

    # Group by label preserving order of first occurrence
    groups: dict[str, list[dict]] = {}
    for s in stories:
        label = s.get("label", "Other")
        groups.setdefault(label, []).append(s)

    lines = [f"🔬 **Tech Digest. {now.strftime('%d.%m.%Y')}**\n"]
    n = 0
    for label, items in groups.items():
        emoji = _LABEL_EMOJI.get(label, "📌")
        lines.append(f"**{emoji} {label}**\n")
        for s in items:
            n += 1
            tldr = s.get("tldr", "").strip()
            body = f" - {tldr}" if tldr else ""
            lines.append(
                f"{n}. **{s['title']}**{body}\n"
                f"   {s['url']}\n"
            )

    lines.append(f"{len(stories)} постов из HN + Lobsters")
    return "\n".join(lines)


# --- Main entry point ---


async def build_hn_digest() -> str:
    """Build HN + Lobsters digest. Returns formatted text."""
    logger.info("Building HN + Lobsters digest")

    seen = _load_seen()
    all_stories = []

    async with httpx.AsyncClient() as client:
        try:
            hn_stories = await _fetch_hn_stories(client)
            all_stories.extend(hn_stories)
            logger.info(f"Fetched {len(hn_stories)} HN stories (score >= {HN_MIN_SCORE})")
        except Exception as e:
            logger.error(f"Failed to fetch HN: {e}")

        if HN_LOBSTERS_ENABLED:
            lobsters = await _fetch_lobsters(client)
            all_stories.extend(lobsters)
            logger.info(f"Fetched {len(lobsters)} Lobsters stories")

    ranked = _filter_and_rank(all_stories, seen, HN_KEYWORDS, HN_MAX_STORIES)
    await _enrich_tldrs(ranked)
    text = format_digest(ranked)

    # Save seen URLs
    now_iso = datetime.now(_tz).isoformat()
    for s in ranked:
        seen[s["url"]] = now_iso
        if s.get("hn_url"):
            seen[s["hn_url"]] = now_iso
    _save_seen(seen)

    return text


async def send_hn_digest():
    """Scheduled job: send HN + Lobsters digest to admin."""
    from telegram import Bot
    text = await build_hn_digest()
    bot = Bot(token=BOT_TOKEN)
    dest = hub_dest("digest")
    for i in range(0, len(text), 4096):
        await bot.send_message(text=text[i:i + 4096], **dest)
