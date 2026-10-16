"""Slack bot that turns Slack requests into YouTube Shorts via the MPT API.

Run the API first (`python main.py`), then:

    uv sync --extra slack
    SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... uv run python slack_bot.py

The bot uses Socket Mode, so it needs no public URL. See docs/slack-mila.md.

Publishing is deliberately left to a human: the bot posts the finished video,
draft YouTube metadata and suggested posting slots, and a person approves it.
Agents such as Mila never receive account credentials.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone

from loguru import logger

from app.config import config
from app.services import slack_shorts as ss

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError:  # pragma: no cover - exercised only without the extra
    raise SystemExit("slack-bolt is not installed. Run: uv sync --extra slack")


def _setting(key: str, default=None):
    env_value = os.environ.get(f"SLACK_{key.upper()}")
    if env_value is not None:
        return env_value
    return config.slack.get(key, default)


def _id_list(key: str) -> set[str]:
    value = _setting(key, [])
    if isinstance(value, str):
        value = [item for item in re.split(r"[,\s]+", value) if item]
    return set(value)


ALLOWED_CHANNELS = _id_list("allowed_channels")
ALLOWED_BOT_IDS = _id_list("allowed_bot_ids")
APPROVER_USER_IDS = _id_list("approver_user_ids")
DEFAULT_LANGUAGE = _setting("default_language", "tr")
TIMEZONE = _setting("timezone", "Europe/Istanbul")
POSTING_SLOTS = config.slack.get("posting_slots", list(ss.DEFAULT_POSTING_SLOTS))
PRESET_SETTINGS = {
    key: config.slack[key]
    for key in ("font_name", "video_source", "script_prompt")
    if config.slack.get(key)
}

client = ss.MptClient(
    base_url=_setting("mpt_api_url", "http://127.0.0.1:8080"),
    api_key=_setting("mpt_api_key", "") or config.app.get("api_key", ""),
)
executor = ThreadPoolExecutor(max_workers=int(_setting("max_parallel_jobs", 2)))
# Requests are kept in memory so "regenerate" can reuse them; they are lost on
# restart, which only disables the button for older messages.
_jobs: dict[str, dict] = {}

app = App(token=_setting("bot_token"))


def _channel_allowed(channel: str) -> bool:
    return not ALLOWED_CHANNELS or channel in ALLOWED_CHANNELS


def _start_job(slack_client, channel: str, thread_ts: str, text: str, requester: str):
    if not _channel_allowed(channel):
        if requester.startswith(("U", "W")):
            slack_client.chat_postEphemeral(
                channel=channel, user=requester, text="Bu kanalda Shorts üretimi kapalı."
            )
        return
    try:
        request = ss.parse_request(text, default_language=DEFAULT_LANGUAGE)
    except ss.ShortRequestError as exc:
        slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=str(exc))
        return
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"request": request, "channel": channel, "thread_ts": thread_ts}
    executor.submit(_run_job, slack_client, job_id, requester)


def _run_job(slack_client, job_id: str, requester: str):
    job = _jobs[job_id]
    request: ss.ShortRequest = job["request"]
    channel, thread_ts = job["channel"], job["thread_ts"]
    notes = "".join(f"\n> {w}" for w in request.warnings)
    status = slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f":clapper: *{request.subject}* için Short hazırlanıyor "
        f"({request.language}, {request.aspect}, {request.count} adet)…{notes}",
    )

    def update(text: str):
        slack_client.chat_update(channel=channel, ts=status["ts"], text=text)

    try:
        task_id = client.create_video(ss.build_video_payload(request, PRESET_SETTINGS))
        job["task_id"] = task_id
        task = client.wait_for_task(
            task_id,
            on_progress=lambda t: update(
                f":hourglass_flowing_sand: *{request.subject}* — %{t.get('progress', 0)}"
            ),
        )
        update(f":white_check_mark: *{request.subject}* hazır, yükleniyor…")
        with tempfile.TemporaryDirectory(prefix="echo-shorts-") as tmp:
            for index, uri in enumerate(task.get("videos") or [], start=1):
                path = client.download(uri, os.path.join(tmp, f"short-{index}.mp4"))
                slack_client.files_upload_v2(
                    channel=channel,
                    thread_ts=thread_ts,
                    file=path,
                    filename=f"{job_id}-{index}.mp4",
                    title=f"{request.subject} ({index})",
                )
        script = (task.get("script") or request.script or "").strip()
        _post_review(slack_client, job_id, request, script, requester)
        update(f":white_check_mark: *{request.subject}* hazır.")
    except Exception as exc:  # report every failure back to the thread
        logger.exception(f"slack short job {job_id} failed")
        update(f":x: *{request.subject}* üretilemedi: {exc}")


def _post_review(slack_client, job_id, request, script, requester):
    job = _jobs[job_id]
    meta = ss.youtube_metadata(request, script)
    slots = ss.next_posting_slots(
        datetime.now(timezone.utc), POSTING_SLOTS, TIMEZONE
    )
    slot_text = ", ".join(ss.format_slot(s) for s in slots)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Başlık:* {meta['title']}\n"
                f"*Etiketler:* {', '.join(meta['tags'])}\n"
                f"*Önerilen saatler ({TIMEZONE}):* {slot_text}\n"
                f"*İsteyen:* <@{requester}>",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": meta["description"][:2900]}],
        },
        {
            "type": "actions",
            "block_id": f"review:{job_id}",
            "elements": [
                {"type": "button", "action_id": "short_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Onayla"}, "value": job_id},
                {"type": "button", "action_id": "short_regenerate",
                 "text": {"type": "plain_text", "text": "Yeniden üret"}, "value": job_id},
                {"type": "button", "action_id": "short_reject", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reddet"}, "value": job_id},
            ],
        },
    ]
    slack_client.chat_postMessage(
        channel=job["channel"], thread_ts=job["thread_ts"],
        text=f"İnceleme: {meta['title']}", blocks=blocks,
    )


@app.command("/short")
def handle_short_command(ack, command, client):
    ack()
    posted = client.chat_postMessage(
        channel=command["channel_id"],
        text=f"<@{command['user_id']}> Short isteği: {command.get('text', '')}",
    )
    _start_job(client, command["channel_id"], posted["ts"], command.get("text", ""), command["user_id"])


@app.event("app_mention")
def handle_mention(event, client):
    _start_job(
        client, event["channel"], event.get("thread_ts") or event["ts"],
        event.get("text", ""), event.get("user", ""),
    )


@app.message(re.compile(r"^\s*shorts?\s*:", re.IGNORECASE))
def handle_agent_message(message, client):
    # Humans use /short or @mention; this path lets agents such as Mila, which
    # post as bots, queue a Short by writing "short: <konu>" in an allowed channel.
    bot_id = message.get("bot_id")
    if not bot_id or bot_id not in ALLOWED_BOT_IDS:
        return
    _start_job(
        client, message["channel"], message.get("thread_ts") or message["ts"],
        message.get("text", ""), message.get("user") or bot_id,
    )


def _review_action(ack, body, client, verdict: str):
    ack()
    user = body["user"]["id"]
    job_id = body["actions"][0]["value"]
    channel = body["channel"]["id"]
    if APPROVER_USER_IDS and user not in APPROVER_USER_IDS:
        client.chat_postEphemeral(channel=channel, user=user, text="Bu Short'u onaylama yetkiniz yok.")
        return
    original = body["message"]
    blocks = [b for b in original.get("blocks", []) if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": verdict.format(user=user)}]})
    client.chat_update(channel=channel, ts=original["ts"], text=original.get("text", ""), blocks=blocks)
    return job_id


@app.action("short_approve")
def handle_approve(ack, body, client):
    _review_action(ack, body, client, ":white_check_mark: <@{user}> onayladı. Yayını hesap sahibi yapar.")


@app.action("short_reject")
def handle_reject(ack, body, client):
    _review_action(ack, body, client, ":x: <@{user}> reddetti.")


@app.action("short_regenerate")
def handle_regenerate(ack, body, client):
    job_id = _review_action(ack, body, client, ":repeat: <@{user}> yeniden üretim istedi.")
    job = _jobs.get(job_id) if job_id else None
    if not job:
        if job_id:
            client.chat_postMessage(
                channel=body["channel"]["id"], thread_ts=body["message"].get("thread_ts"),
                text="Bu isteğin kaydı bot yeniden başladığı için yok; lütfen tekrar gönderin.",
            )
        return
    new_id = uuid.uuid4().hex[:12]
    _jobs[new_id] = {**job, "request": ss.ShortRequest(**asdict(job["request"]))}
    executor.submit(_run_job, client, new_id, body["user"]["id"])


if __name__ == "__main__":
    logger.info("Echo Shorts Slack bot starting (Socket Mode)")
    SocketModeHandler(app, _setting("app_token")).start()
