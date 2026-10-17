"""Render a YouTube Short from the command line for an existing agent.

Agents that already live in Slack (such as Mila) cannot share the Echo Shorts
bot's Socket Mode connection, and a workspace may have no room for another
app. This entry point lets such an agent use the same request grammar and
brand preset without a Slack app: it renders through the MPT API, saves the
MP4s locally and prints one JSON object the agent can post with its own
Slack identity. Publishing still stays with a human.

    uv run python shorts_cli.py --out ./shorts "short: Sakin planlama dil:tr"

Exit codes: 0 rendered, 2 invalid request or settings, 1 render failed.
Progress goes to stderr so stdout stays a single JSON document.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Callable, Mapping, TextIO

from app.services import slack_shorts as ss
from app.services.slack_shorts_bot import SlackSettings, load_settings


def render_short(
    request: ss.ShortRequest,
    settings: SlackSettings,
    mpt: ss.MptClient,
    out_dir: str,
    now: datetime,
    log: Callable[[str], None] = lambda message: None,
) -> dict:
    """Render `request`, download its videos into `out_dir`, return a summary."""
    task_id = mpt.create_video(ss.build_video_payload(request, dict(settings.preset)))
    log(f"task {task_id} started")
    task = mpt.wait_for_task(
        task_id,
        on_progress=lambda t: log(f"task {task_id}: {t.get('progress', 0)}%"),
        poll_interval=settings.poll_interval_seconds,
        timeout=settings.job_timeout_seconds,
    )
    uris = task.get("videos") or []
    if not uris:
        raise ss.MptApiError("Görev tamamlandı ama video dosyası döndürmedi.")

    os.makedirs(out_dir, exist_ok=True)
    videos = [
        os.path.abspath(mpt.download(uri, os.path.join(out_dir, f"short-{task_id}-{index}.mp4")))
        for index, uri in enumerate(uris, start=1)
    ]
    script = str(task.get("script") or request.script or "").strip()
    slots = ss.next_posting_slots(now, settings.posting_slots, settings.timezone)
    return {
        "task_id": task_id,
        "subject": request.subject,
        "language": request.language,
        "videos": videos,
        "script": script,
        **ss.youtube_metadata(request, script),
        "posting_slots": [{"at": slot.isoformat(), "label": ss.format_slot(slot)} for slot in slots],
        "warnings": request.warnings,
    }


def main(
    argv: list[str] | None = None,
    section: Mapping | None = None,
    environ: Mapping[str, str] | None = None,
    fallback_api_key: str = "",
    mpt_factory: Callable[[SlackSettings], ss.MptClient] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = argparse.ArgumentParser(description="Render an ECHO YouTube Short via the MPT API.")
    parser.add_argument("text", nargs="?", help="request text, same format as the Slack bot; '-' or omitted reads stdin")
    parser.add_argument("--out", default="shorts", help="directory for the MP4 files (default: ./shorts)")
    args = parser.parse_args(argv)

    def fail(code: int, message: str) -> int:
        json.dump({"error": message}, stdout, ensure_ascii=False)
        stdout.write("\n")
        return code

    text = sys.stdin.read() if args.text in (None, "-") else args.text
    try:
        settings = load_settings(section or {}, environ if environ is not None else os.environ, fallback_api_key)
        request = ss.parse_request(text, settings.default_language)
    except ValueError as exc:  # ShortRequestError is a ValueError too
        return fail(2, str(exc))

    mpt = (mpt_factory or (lambda s: ss.MptClient(s.mpt_api_url, s.mpt_api_key)))(settings)
    try:
        result = render_short(
            request, settings, mpt, args.out, now(), log=lambda m: print(m, file=stderr, flush=True)
        )
    except ss.MptApiError as exc:
        return fail(1, str(exc))
    except ss.requests.RequestException:
        return fail(1, f"MPT API'ye ulaşılamadı: {settings.mpt_api_url}")
    json.dump(result, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0
