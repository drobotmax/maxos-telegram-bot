#!/usr/bin/env python3
"""Google Calendar tool for the Telegram bot.

Usage:
  # Create event
  python3 calendar-tool.py create --title "Meeting" --start "2026-02-24T08:00" --end "2026-02-24T09:00"
  python3 calendar-tool.py create --title "Meeting" --start "2026-02-24T08:00" --duration 60
  python3 calendar-tool.py create --title "Meeting" --start "2026-02-24T08:00" --duration 60 --description "Notes"

  # List events for a date
  python3 calendar-tool.py list --date "2026-02-24"
  python3 calendar-tool.py list  # today

  # Delete event
  python3 calendar-tool.py delete --event-id "abc123"
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import os

# Resolve credentials: GOOGLE_CREDS_FILE env wins, otherwise the first
# credentials json found under known home dirs (VPS user, then local).
_env_creds = os.getenv("GOOGLE_CREDS_FILE", "")
CREDS_FILE = Path(_env_creds) if _env_creds else None
if CREDS_FILE is None:
    for _home in [Path("/home/maxos"), Path.home()]:
        _creds_dir = _home / ".google_workspace_mcp" / "credentials"
        _found = sorted(_creds_dir.glob("*.json")) if _creds_dir.exists() else []
        if _found:
            CREDS_FILE = _found[0]
            break
if CREDS_FILE is None:
    sys.exit("calendar-tool: no Google credentials found; set GOOGLE_CREDS_FILE")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
CALENDAR_ID = "primary"
# Comma-separated calendar ids to skip (holidays, sports etc.)
EXCLUDED_CALENDARS = {
    c.strip() for c in os.getenv("CALENDAR_EXCLUDED", "").split(",") if c.strip()
}


def get_service():
    if not CREDS_FILE.exists():
        print(json.dumps({"error": f"Credentials not found: {CREDS_FILE}"}))
        sys.exit(1)

    data = json.loads(CREDS_FILE.read_text())
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", []),
    )
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    # Save refreshed token
    if creds.token != data.get("token"):
        data["token"] = creds.token
        if creds.expiry:
            data["expiry"] = creds.expiry.isoformat()
        CREDS_FILE.write_text(json.dumps(data, indent=2))

    return service


def cmd_create(args):
    service = get_service()

    start_dt = datetime.fromisoformat(args.start)

    if args.end:
        end_dt = datetime.fromisoformat(args.end)
    elif args.duration:
        end_dt = start_dt + timedelta(minutes=args.duration)
    else:
        end_dt = start_dt + timedelta(hours=1)

    event = {
        "summary": args.title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }
    if args.description:
        event["description"] = args.description
    if args.location:
        event["location"] = args.location

    result = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    print(json.dumps({
        "status": "created",
        "id": result["id"],
        "summary": result.get("summary"),
        "start": result["start"].get("dateTime"),
        "end": result["end"].get("dateTime"),
        "link": result.get("htmlLink"),
    }, ensure_ascii=False))


def cmd_list(args):
    service = get_service()

    if args.date:
        day = datetime.fromisoformat(args.date)
    else:
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo(TIMEZONE))

    time_min = day.replace(hour=0, minute=0, second=0).isoformat()
    time_max = day.replace(hour=23, minute=59, second=59).isoformat()

    # Ensure timezone offset
    if "+" not in time_min and "Z" not in time_min:
        time_min += "+03:00"
        time_max += "+03:00"

    # Get all calendars, filter out excluded
    cal_list = service.calendarList().list().execute()
    calendar_ids = [
        c["id"] for c in cal_list.get("items", [])
        if c["id"] not in EXCLUDED_CALENDARS
    ]

    events = []
    for cal_id in calendar_ids:
        result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        for e in result.get("items", []):
            events.append({
                "id": e["id"],
                "summary": e.get("summary", "(no title)"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
                "location": e.get("location"),
            })

    # Sort by start time
    events.sort(key=lambda e: e["start"] or "")

    print(json.dumps({"date": day.strftime("%Y-%m-%d"), "events": events, "count": len(events)}, ensure_ascii=False))


def cmd_delete(args):
    service = get_service()
    service.events().delete(calendarId=CALENDAR_ID, eventId=args.event_id).execute()
    print(json.dumps({"status": "deleted", "id": args.event_id}))


def main():
    parser = argparse.ArgumentParser(description="Google Calendar tool")
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-02-24T08:00")
    p_create.add_argument("--end", help="ISO datetime")
    p_create.add_argument("--duration", type=int, help="Duration in minutes (default 60)")
    p_create.add_argument("--description", default="")
    p_create.add_argument("--location", default="")

    # list
    p_list = sub.add_parser("list")
    p_list.add_argument("--date", help="YYYY-MM-DD (default today)")

    # delete
    p_delete = sub.add_parser("delete")
    p_delete.add_argument("--event-id", required=True)

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "delete":
        cmd_delete(args)


if __name__ == "__main__":
    main()
