#!/usr/bin/env python3
"""Todoist tool for the Telegram bot (Todoist API v1).

Usage:
  # Create task
  python3 todoist-tool.py create --content "Позвонить Николаю" --due "tomorrow" --labels "Консалтинг"
  python3 todoist-tool.py create --content "Подготовить КП" --priority 3 --description "для Цветкова"

  # List tasks (default filter: today | overdue)
  python3 todoist-tool.py list
  python3 todoist-tool.py list --filter "today"
  python3 todoist-tool.py list --filter "overdue"

Env: TODOIST_API_TOKEN (required), TODOIST_PROJECT_ID (default: 6g68Cw5fjj3c2w8h = Работа)
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

BASE_URL = "https://api.todoist.com/api/v1"
DEFAULT_PROJECT_ID = os.getenv("TODOIST_PROJECT_ID", "6g68Cw5fjj3c2w8h")


def _load_env_from_file():
    """Load TODOIST_API_TOKEN from bot .env if not already in env."""
    if os.getenv("TODOIST_API_TOKEN"):
        return
    for path in ("/home/maxos/maxos-telegram-bot/.env",
                 os.path.expanduser("~/maxos-telegram-bot/.env")):
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TODOIST_API_TOKEN="):
                    os.environ["TODOIST_API_TOKEN"] = line.split("=", 1)[1].strip().strip("'\"")
                    return


def _request(method: str, path: str, body: dict | None = None):
    token = os.getenv("TODOIST_API_TOKEN")
    if not token:
        print(json.dumps({"error": "TODOIST_API_TOKEN not set"}))
        sys.exit(1)

    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        print(json.dumps({"error": f"HTTP {e.code}: {err_body[:300]}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def cmd_create(args):
    payload = {
        "content": args.content,
        "project_id": args.project_id or DEFAULT_PROJECT_ID,
    }
    if args.description:
        payload["description"] = args.description
    if args.due:
        payload["due_string"] = args.due
    if args.deadline:
        payload["deadline_date"] = args.deadline
    if args.priority and args.priority > 1:
        payload["priority"] = args.priority
    if args.labels:
        payload["labels"] = [l.strip() for l in args.labels.split(",") if l.strip()]
    else:
        payload["labels"] = ["MaxOS"]

    task = _request("POST", "/tasks", payload)
    print(json.dumps({
        "status": "created",
        "id": task.get("id"),
        "content": task.get("content"),
        "due": (task.get("due") or {}).get("string"),
        "labels": task.get("labels"),
        "priority": task.get("priority"),
        "url": f"https://todoist.com/showTask?id={task.get('id')}",
    }, ensure_ascii=False))


def cmd_list(args):
    query_params = {}
    if args.project_id or not args.filter:
        query_params["project_id"] = args.project_id or DEFAULT_PROJECT_ID
    if args.filter:
        query_params["query"] = args.filter
        path = "/tasks/filter?" + urllib.parse.urlencode(query_params)
    else:
        path = "/tasks?" + urllib.parse.urlencode(query_params)

    data = _request("GET", path)
    tasks = data.get("results", []) if isinstance(data, dict) else data
    summary = [
        {
            "id": t.get("id"),
            "content": t.get("content"),
            "due": (t.get("due") or {}).get("string"),
            "priority": t.get("priority"),
            "labels": t.get("labels"),
        }
        for t in tasks
    ]
    print(json.dumps({"count": len(summary), "tasks": summary}, ensure_ascii=False))


def cmd_complete(args):
    _request("POST", f"/tasks/{args.id}/close")
    print(json.dumps({"status": "completed", "id": args.id}))


def main():
    _load_env_from_file()

    parser = argparse.ArgumentParser(description="Todoist tool (API v1)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a task")
    p_create.add_argument("--content", required=True, help="Task title")
    p_create.add_argument("--description", default="", help="Optional description")
    p_create.add_argument("--due", help="Natural language due: 'today', 'tomorrow', 'next Mon 10:00'")
    p_create.add_argument("--deadline", help="ISO date deadline YYYY-MM-DD (immovable)")
    p_create.add_argument("--priority", type=int, default=1, help="1=normal, 4=urgent")
    p_create.add_argument("--labels", help="Comma-separated labels (default: MaxOS)")
    p_create.add_argument("--project-id", help="Override project ID")

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--filter", help="Filter query, e.g. 'today', 'overdue', '@YOLO'")
    p_list.add_argument("--project-id", help="Override project ID")

    p_complete = sub.add_parser("complete", help="Mark task as done")
    p_complete.add_argument("--id", required=True, help="Task ID")

    args = parser.parse_args()
    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "complete":
        cmd_complete(args)


if __name__ == "__main__":
    main()
