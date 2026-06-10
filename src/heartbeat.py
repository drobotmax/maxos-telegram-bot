"""Heartbeat monitor – pure Python checks every 30 min, no Claude calls.

Reads local files (pipeline, calendar, email), compares against thresholds,
alerts only when something actionable happened. Zero RAM overhead.
"""

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import (
    HEARTBEAT_CALENDAR_ALERT_MINUTES,
    HEARTBEAT_STALE_SYNC_HOURS,
    HEARTBEAT_STATE_FILE,
    HEARTBEAT_VIP_KEYWORDS,
    MAXOS_DIR,
    TIMEZONE,
)

logger = logging.getLogger(__name__)
_tz = ZoneInfo(TIMEZONE)
_maxos = Path(MAXOS_DIR)
_state_path = Path(__file__).parent.parent / HEARTBEAT_STATE_FILE


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load heartbeat state. Returns clean defaults if missing/corrupt."""
    defaults = {
        "last_run": "",
        "alerted_pipeline_today": [],
        "alerted_calendar_events": [],
        "last_email_hash": "",
        "last_calendar_text": "",
        "morning_plan": None,
        "alert_date": "",
    }
    try:
        if _state_path.exists():
            data = json.loads(_state_path.read_text())
            today = datetime.now(_tz).strftime("%Y-%m-%d")
            if data.get("alert_date") != today:
                data["alerted_pipeline_today"] = []
                data["alerted_calendar_events"] = []
                data["morning_plan"] = None
                data["alert_date"] = today
            for k, v in defaults.items():
                data.setdefault(k, v)
            return data
    except Exception as e:
        logger.warning(f"Failed to load heartbeat state: {e}")
    defaults["alert_date"] = datetime.now(_tz).strftime("%Y-%m-%d")
    return defaults


def save_state(state: dict):
    """Persist heartbeat state to disk."""
    try:
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        _state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save heartbeat state: {e}")


# ---------------------------------------------------------------------------
# Check 1: Pipeline overdue follow-ups
# ---------------------------------------------------------------------------

def check_pipeline_overdue(state: dict) -> list[str]:
    """Find leads with overdue follow-ups not yet alerted today."""
    pipeline_path = _maxos / "data" / "pipeline.json"
    if not pipeline_path.exists():
        return []
    try:
        pipeline = json.loads(pipeline_path.read_text())
    except Exception as e:
        logger.warning(f"Heartbeat: failed to read pipeline.json: {e}")
        return []

    today_str = datetime.now(_tz).strftime("%Y-%m-%d")
    today_date = datetime.now(_tz).date()
    already = set(state.get("alerted_pipeline_today", []))
    alerts = []

    for lead in pipeline.get("leads", []):
        if lead.get("stage") not in ("contacted", "responded"):
            continue
        followup = lead.get("followup")
        if not followup or followup > today_str:
            continue
        if lead.get("followupCount", 0) >= 3:
            continue
        lead_id = lead.get("id", lead.get("company", "unknown"))
        if lead_id in already:
            continue

        try:
            fu_date = datetime.strptime(followup, "%Y-%m-%d").date()
            days_overdue = (today_date - fu_date).days
        except ValueError:
            days_overdue = 0

        company = lead.get("company", lead_id)
        stage = lead.get("stage", "?")
        if days_overdue > 0:
            alerts.append(f"Pipeline: {company} ({stage}) - follow-up {days_overdue}d overdue")
        else:
            alerts.append(f"Pipeline: {company} ({stage}) - follow-up due today")
        already.add(lead_id)

    state["alerted_pipeline_today"] = list(already)
    return alerts


# ---------------------------------------------------------------------------
# Check 2: Calendar upcoming + change detection
# ---------------------------------------------------------------------------

def _parse_calendar_events(text: str) -> list[dict]:
    """Parse calendar-today.md into structured events.

    Expected format:
      - HH:MM Title
    """
    events = []
    for line in text.strip().split("\n"):
        m = re.match(r"^-\s+(\d{1,2}):(\d{2})\s+(.+)$", line.strip())
        if m:
            events.append({
                "hour": int(m.group(1)),
                "minute": int(m.group(2)),
                "title": m.group(3).strip(),
            })
    return events


def check_calendar(state: dict) -> list[str]:
    """Alert for upcoming events and detect calendar changes."""
    cal_path = _maxos / "store" / "calendar-today.md"
    if not cal_path.exists():
        return []
    try:
        cal_text = cal_path.read_text().strip()
    except Exception:
        return []
    if not cal_text:
        return []

    alerts = []
    now = datetime.now(_tz)
    already = set(state.get("alerted_calendar_events", []))
    events = _parse_calendar_events(cal_text)

    # Upcoming event alerts
    for ev in events:
        try:
            event_time = now.replace(
                hour=ev["hour"], minute=ev["minute"], second=0, microsecond=0,
            )
        except ValueError:
            continue
        diff_min = (event_time - now).total_seconds() / 60
        if 0 < diff_min <= HEARTBEAT_CALENDAR_ALERT_MINUTES:
            event_key = f"{ev['hour']}:{ev['minute']:02d}_{ev['title'][:20]}"
            if event_key not in already:
                alerts.append(f"Встреча через {int(diff_min)} мин: {ev['title']}")
                already.add(event_key)

    # Calendar change detection
    prev_cal = state.get("last_calendar_text", "")
    if prev_cal and cal_text != prev_cal:
        prev_titles = {e["title"] for e in _parse_calendar_events(prev_cal)}
        curr_titles = {e["title"] for e in events}
        for title in curr_titles - prev_titles:
            alerts.append(f"Новое событие: {title}")
        for title in prev_titles - curr_titles:
            alerts.append(f"Отменено: {title}")

    state["alerted_calendar_events"] = list(already)
    state["last_calendar_text"] = cal_text
    return alerts


# ---------------------------------------------------------------------------
# Check 3: Email change detection
# ---------------------------------------------------------------------------

def check_email_changes(state: dict) -> list[str]:
    """Detect if email digest changed and contains VIP keywords."""
    email_path = _maxos / "store" / "email-digest.md"
    if not email_path.exists():
        return []
    try:
        email_text = email_path.read_text().strip()
    except Exception:
        return []

    current_hash = hashlib.md5(email_text.encode()).hexdigest()
    prev_hash = state.get("last_email_hash", "")
    if current_hash == prev_hash:
        return []
    state["last_email_hash"] = current_hash

    lower = email_text.lower()
    if "нет важных" in lower or "no important" in lower:
        return []

    for kw in HEARTBEAT_VIP_KEYWORDS:
        if kw.lower() in lower:
            lines = [l.strip() for l in email_text.split("\n") if l.strip() and not l.startswith("#")]
            preview = lines[0][:80] if lines else "новое содержание"
            return [f"Email обновлён: {preview}"]

    # Content changed but no VIP keywords - still worth a brief note
    return [f"Email digest обновлён"]


# ---------------------------------------------------------------------------
# Check 4: Infrastructure health
# ---------------------------------------------------------------------------

async def check_infra_health() -> list[str]:
    """Check WhatsApp bridge + Mac sync freshness."""
    alerts = []

    # WhatsApp bridge
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:8080/api/status", timeout=5)
            if resp.status_code == 200:
                status = resp.json()
                if not status.get("connected"):
                    alerts.append("WhatsApp bridge disconnected")
                elif not status.get("logged_in"):
                    alerts.append("WhatsApp not logged in (needs QR)")
            else:
                alerts.append(f"WhatsApp API returned {resp.status_code}")
    except httpx.ConnectError:
        alerts.append("WhatsApp bridge unreachable")
    except Exception as e:
        alerts.append(f"WhatsApp check failed: {type(e).__name__}")

    # Mac sync freshness
    try:
        cal_path = _maxos / "store" / "calendar-today.md"
        if cal_path.exists():
            age_hours = (datetime.now().timestamp() - cal_path.stat().st_mtime) / 3600
            if age_hours > HEARTBEAT_STALE_SYNC_HOURS:
                alerts.append(f"Mac sync stale ({age_hours:.0f}h)")
    except Exception:
        pass

    return alerts


# ---------------------------------------------------------------------------
# Check 5: Morning plan drift
# ---------------------------------------------------------------------------

def capture_morning_plan(state: dict):
    """Capture morning snapshot of calendar + pipeline for drift detection.

    Called once after the 07:00 morning_checkin completes.
    """
    cal_path = _maxos / "store" / "calendar-today.md"
    cal_text = ""
    if cal_path.exists():
        try:
            cal_text = cal_path.read_text().strip()
        except Exception:
            pass

    pipeline_path = _maxos / "data" / "pipeline.json"
    pipeline_summary = {}
    if pipeline_path.exists():
        try:
            data = json.loads(pipeline_path.read_text())
            active = [l for l in data.get("leads", []) if l.get("stage") in ("contacted", "responded")]
            pipeline_summary = {
                "active_count": len(active),
                "responded": [l.get("company") for l in active if l.get("stage") == "responded"],
            }
        except Exception:
            pass

    state["morning_plan"] = {
        "captured_at": datetime.now(_tz).isoformat(),
        "calendar_events": [e["title"] for e in _parse_calendar_events(cal_text)],
        "pipeline": pipeline_summary,
    }
    save_state(state)
    logger.info("Morning plan snapshot captured")


def check_plan_drift(state: dict) -> list[str]:
    """Compare current calendar with morning snapshot."""
    morning = state.get("morning_plan")
    if not morning:
        return []

    morning_events = set(morning.get("calendar_events", []))
    cal_path = _maxos / "store" / "calendar-today.md"
    current_events = set()
    if cal_path.exists():
        try:
            cal_text = cal_path.read_text().strip()
            current_events = {e["title"] for e in _parse_calendar_events(cal_text)}
        except Exception:
            pass

    alerts = []
    new = current_events - morning_events
    dropped = morning_events - current_events
    if new:
        alerts.append(f"С утра добавлено: {', '.join(new)}")
    if dropped:
        alerts.append(f"С утра отменено: {', '.join(dropped)}")
    return alerts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_heartbeat() -> list[str]:
    """Run calendar-only checks. Other alerts covered by dedicated cron jobs:
    - Pipeline follow-ups: 10:00 cron
    - Email digest: 06:00 + 18:00 cron
    - Infra health: morning checkin
    - Plan drift: redundant with calendar change detection
    """
    state = load_state()
    all_alerts: list[str] = []

    try:
        all_alerts.extend(check_calendar(state))
    except Exception as e:
        logger.error(f"Heartbeat calendar check failed: {e}")

    state["last_run"] = datetime.now(_tz).isoformat()
    save_state(state)

    if all_alerts:
        logger.info(f"Heartbeat: {len(all_alerts)} alerts")
    else:
        logger.debug("Heartbeat: all clear")
    return all_alerts


def format_heartbeat_message(alerts: list[str]) -> str:
    """Format alerts into a single Telegram message."""
    now = datetime.now(_tz)
    header = f"Heartbeat {now.strftime('%H:%M')}"
    body = "\n".join(f"  {a}" for a in alerts)
    return f"{header}\n\n{body}"
