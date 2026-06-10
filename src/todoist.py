"""Todoist API v1 integration for task creation."""
import logging
import httpx
from .config import TODOIST_API_TOKEN, TODOIST_PROJECT_ID

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.todoist.com/api/v1"


async def create_task(
    content: str,
    description: str = "",
    due_string: str | None = None,
    priority: int = 1,  # 1=normal, 4=urgent
    labels: list[str] | None = None,
) -> dict | None:
    """Create a task in Todoist via REST API. Returns task dict or None on error."""
    if not TODOIST_API_TOKEN:
        logger.error("TODOIST_API_TOKEN not set")
        return None

    payload = {
        "content": content,
        "project_id": TODOIST_PROJECT_ID,
    }
    if description:
        payload["description"] = description
    if due_string:
        payload["due_string"] = due_string
    if priority > 1:
        payload["priority"] = priority
    if labels:
        payload["labels"] = labels

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_BASE_URL}/tasks",
                json=payload,
                headers={
                    "Authorization": f"Bearer {TODOIST_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            task = resp.json()
            task_url = f"https://todoist.com/showTask?id={task.get('id')}" if task.get("id") else ""
            if task_url:
                task["url"] = task_url
            logger.info(f"Todoist task created: {task.get('id')} - {content[:50]}")
            return task
    except Exception as e:
        logger.error(f"Todoist API error: {e}")
        return None


async def get_tasks(filter_str: str = "today | overdue") -> list[dict]:
    """Fetch tasks from Todoist. Returns list of task dicts."""
    if not TODOIST_API_TOKEN:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_BASE_URL}/tasks",
                params={"project_id": TODOIST_PROJECT_ID, "filter": filter_str},
                headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", []) if isinstance(data, dict) else data
    except Exception as e:
        logger.error(f"Todoist API error: {e}")
        return []
