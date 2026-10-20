"""Approval queue for rendered Shorts: weekly batch, human approval, publishing.

Flow (see docs/slack-mila.md):

1. `shorts_cli.py batch` renders a week of Shorts and adds each one here as
   `pending`, already assigned to the next free daily posting slot.
2. Mila posts them to Slack; once a person approves, `shorts_cli.py approve`
   marks them `approved` (or `reject` marks them `rejected`).
3. `shorts_cli.py publish-due`, run hourly by cron/launchd, uploads every
   approved item whose slot has passed and marks it `published`.

An upload whose outcome is unknown (timeout, lost response) becomes
`needs_check` instead of being retried: publishing twice is worse than
publishing late, so a person confirms it in Upload-Post first.

The queue is a small JSON file written atomically; one process at a time is
expected to modify it (the hourly job and occasional manual commands).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from typing import Callable, Iterable

from app.services import slack_shorts as ss

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
PUBLISHED = "published"
NEEDS_CHECK = "needs_check"
FAILED = "failed"
STATUSES = (PENDING, APPROVED, REJECTED, PUBLISHED, NEEDS_CHECK, FAILED)

# Items that still hold their posting slot.
_ACTIVE = (PENDING, APPROVED, PUBLISHED, NEEDS_CHECK)


class QueueError(ValueError):
    pass


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise QueueError(f"{path} is not a Shorts queue file")
    return items


def save(path: str, items: list[dict]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".queue-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def free_slots(
    items: list[dict], now: datetime, count: int, slots: Iterable[str], timezone: str
) -> list[datetime]:
    """Next `count` posting slots that no active queue item already holds."""
    taken = {item["publish_at"] for item in items if item.get("status") in _ACTIVE}
    result: list[datetime] = []
    want = count
    while len(result) < count:
        candidates = ss.next_posting_slots(now, slots, timezone, count=want + len(taken))
        result = [slot for slot in candidates if slot.isoformat() not in taken][:count]
        want *= 2
    return result


def add(items: list[dict], render_result: dict, publish_at: datetime, category: str = "") -> dict:
    item = {
        "id": uuid.uuid4().hex[:8],
        "status": PENDING,
        "publish_at": publish_at.isoformat(),
        "category": category,
        "created_at": datetime.now(publish_at.tzinfo).isoformat(timespec="seconds"),
        "video": render_result["videos"][0],
        "title": render_result["title"],
        "description": render_result["description"],
        "tags": render_result["tags"],
        "subject": render_result["subject"],
        "task_id": render_result.get("task_id", ""),
        "result": None,
    }
    items.append(item)
    return item


def set_status(items: list[dict], ids: Iterable[str], status: str, *, from_statuses=(PENDING,)) -> list[dict]:
    """Move the given items to `status`; every id must exist and be movable."""
    by_id = {item["id"]: item for item in items}
    ids = list(dict.fromkeys(ids))
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise QueueError(f"unknown queue id(s): {', '.join(missing)}")
    blocked = [i for i in ids if by_id[i]["status"] not in from_statuses]
    if blocked:
        detail = ", ".join(f"{i} ({by_id[i]['status']})" for i in blocked)
        raise QueueError(f"cannot mark as {status}: {detail}")
    for i in ids:
        by_id[i]["status"] = status
    return [by_id[i] for i in ids]


def due(items: list[dict], now: datetime) -> list[dict]:
    return [
        item
        for item in items
        if item["status"] == APPROVED and datetime.fromisoformat(item["publish_at"]) <= now
    ]


def publish_due(
    items: list[dict],
    now: datetime,
    upload: Callable[..., dict],
    platforms: Iterable[str],
) -> list[dict]:
    """Upload every due item once and record the outcome on it."""
    platforms = list(platforms)
    handled = []
    for item in due(items, now):
        if not os.path.exists(item["video"]):
            item["status"] = FAILED
            item["result"] = {"error": f"video file missing: {item['video']}"}
            handled.append(item)
            continue
        result = upload(
            item["video"],
            item["title"],
            platforms,
            youtube_extra={
                "youtube_title": item["title"],
                "youtube_description": item["description"],
                "tags": item["tags"],
            },
        )
        if result.get("success"):
            item["status"] = PUBLISHED
        elif "unconfirmed" in str(result.get("error", "")):
            item["status"] = NEEDS_CHECK
        else:
            item["status"] = FAILED
        item["result"] = {
            key: result.get(key) for key in ("success", "request_id", "error", "results") if key in result
        }
        handled.append(item)
    return handled
