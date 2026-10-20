"""Slack handlers for requesting YouTube Shorts (used by slack_bot.py).

The handlers are written against the small subset of the Slack Web API they
call (chat_postMessage, chat_update, chat_postEphemeral, files_upload_v2), and
`ShortsBot.register` attaches them to any Bolt-compatible app. Nothing here
imports slack_bolt, so the behavior is covered by the regular test suite even
though the Slack SDK is an optional extra.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from app.services import slack_shorts as ss

_PRESET_KEYS = ("font_name", "video_source", "script_prompt", "elevenlabs_voice")
_PUBLISH_PLATFORMS = ("youtube", "tiktok", "instagram")
_MAX_ERROR_LENGTH = 300


@dataclass(frozen=True)
class SlackSettings:
    bot_token: str = ""
    app_token: str = ""
    mpt_api_url: str = "http://127.0.0.1:8080"
    mpt_api_key: str = ""
    allowed_channels: frozenset[str] = frozenset()
    allowed_bot_ids: frozenset[str] = frozenset()
    approver_user_ids: frozenset[str] = frozenset()
    profile: str = "echo"
    default_language: str = "tr"
    default_gender: str = "female"
    timezone: str = "Europe/Istanbul"
    posting_slots: tuple[str, ...] = ss.DEFAULT_POSTING_SLOTS
    max_parallel_jobs: int = 2
    poll_interval_seconds: float = 5
    job_timeout_seconds: float = 1800
    preset: Mapping[str, str] = field(default_factory=dict)
    publish_platforms: tuple[str, ...] = ("youtube",)
    queue_path: str = "storage/shorts/queue.json"

    def parse(self, text: str) -> ss.ShortRequest:
        return ss.parse_request(text, self.default_language, self.default_gender)


def load_settings(
    section: Mapping[str, Any],
    environ: Mapping[str, str],
    fallback_api_key: str = "",
) -> SlackSettings:
    """Build validated settings from the [slack] section and SLACK_* env vars.

    Environment variables win so tokens can stay out of config.toml.
    """

    def get(key: str, default: Any = None) -> Any:
        env_value = environ.get(f"SLACK_{key.upper()}")
        return env_value if env_value not in (None, "") else section.get(key, default)

    def id_set(key: str) -> frozenset[str]:
        value = get(key, [])
        if isinstance(value, str):
            value = value.replace(",", " ").split()
        return frozenset(str(item).strip() for item in value if str(item).strip())

    def number(key: str, default: float, low: float, high: float) -> float:
        raw = get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"[slack].{key} must be a number, got {raw!r}") from None
        if not low <= value <= high:
            raise ValueError(f"[slack].{key} must be between {low:g} and {high:g}")
        return value

    try:
        profile = ss.get_profile(str(get("profile", "echo")))
    except ValueError as exc:
        raise ValueError(f"[slack].{exc}") from None
    language = str(get("default_language", profile.language)).lower()
    if language not in ss.VOICES:
        raise ValueError(f"[slack].default_language must be one of {sorted(ss.VOICES)}")
    tz_name = str(get("timezone", profile.timezone))
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"[slack].timezone is not a valid IANA zone: {tz_name!r}") from None
    slots = get("posting_slots", list(profile.posting_slots))
    if isinstance(slots, str):
        slots = slots.replace(",", " ").split()
    slots = tuple(str(slot) for slot in slots)
    for slot in slots:
        ss.parse_slot(slot)
    voice = str(get("elevenlabs_voice", ""))
    if voice and not re.match(r"^elevenlabs:[^:\s]+:.+$", voice):
        raise ValueError("[slack].elevenlabs_voice must look like 'elevenlabs:<voice_id>:<name>'")
    platforms = get("publish_platforms", ["youtube"])
    if isinstance(platforms, str):
        platforms = platforms.replace(",", " ").split()
    platforms = tuple(str(p).lower() for p in platforms)
    unknown = sorted(set(platforms) - set(_PUBLISH_PLATFORMS))
    if not platforms or unknown:
        raise ValueError(f"[slack].publish_platforms must be a non-empty subset of {list(_PUBLISH_PLATFORMS)}")
    preset = {key: str(get(key)) for key in _PRESET_KEYS if get(key)}
    preset["profile"] = profile.name
    sources = get("video_sources", [])
    if isinstance(sources, str):
        sources = sources.replace(",", " ").split()
    if sources:
        # Kept as one comma-separated string so the preset stays str -> str.
        preset["video_sources"] = ",".join(str(src).strip().lower() for src in sources)

    return SlackSettings(
        bot_token=str(get("bot_token", "")),
        app_token=str(get("app_token", "")),
        mpt_api_url=str(get("mpt_api_url", "http://127.0.0.1:8080")),
        mpt_api_key=str(get("mpt_api_key", "") or fallback_api_key or ""),
        allowed_channels=id_set("allowed_channels"),
        allowed_bot_ids=id_set("allowed_bot_ids"),
        approver_user_ids=id_set("approver_user_ids"),
        profile=profile.name,
        default_language=language,
        default_gender=profile.gender,
        timezone=tz_name,
        posting_slots=slots,
        max_parallel_jobs=int(number("max_parallel_jobs", 2, 1, 8)),
        poll_interval_seconds=number("poll_interval_seconds", 5, 1, 60),
        job_timeout_seconds=number("job_timeout_seconds", 1800, 60, 7200),
        preset=preset,
        publish_platforms=platforms,
        queue_path=str(get("queue_path", "storage/shorts/queue.json")),
    )


@dataclass
class Job:
    request: ss.ShortRequest
    channel: str
    thread_ts: str
    requester: str
    task_id: str = ""


def _mention(requester: str) -> str:
    # Users render as mentions; bot IDs (B...) do not, so name them plainly.
    return f"<@{requester}>" if requester[:1] in ("U", "W") else f"bot `{requester}`"


def _user_facing_error(exc: Exception) -> str:
    if isinstance(exc, (ss.MptApiError, ss.ShortRequestError)):
        return str(exc)[:_MAX_ERROR_LENGTH]
    # Unexpected errors may carry internal paths or URLs; keep them in the log.
    return "beklenmeyen bir hata oluştu, ayrıntılar bot loglarında."


class _BoundedDict(OrderedDict):
    """Insertion-ordered dict that forgets the oldest entries beyond `limit`."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        while len(self) > self.limit:
            self.popitem(last=False)


class ShortsBot:
    def __init__(
        self,
        settings: SlackSettings,
        mpt: ss.MptClient,
        submit: Callable[..., Any],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        progress_interval: float = 10,
        max_remembered_jobs: int = 200,
    ):
        self.settings = settings
        self.mpt = mpt
        self.submit = submit
        self.now = now
        self.clock = clock
        self.sleep = sleep
        self.progress_interval = progress_interval
        self._lock = threading.Lock()
        self._jobs: _BoundedDict = _BoundedDict(max_remembered_jobs)
        self._seen: _BoundedDict = _BoundedDict(max_remembered_jobs * 5)

    # ------------------------------------------------------------------ wiring

    def register(self, app) -> None:
        @app.command("/short")
        def _command(ack, command, client, respond):
            ack()
            self.on_command(command, client, respond)

        @app.event("app_mention")
        def _mention_event(event, client):
            self.on_mention(event, client)

        @app.message(ss.AGENT_TRIGGER_PATTERN)
        def _agent_message(message, client):
            self.on_agent_message(message, client)

        for action_id, handler in (
            ("short_approve", self.on_approve),
            ("short_reject", self.on_reject),
            ("short_regenerate", self.on_regenerate),
        ):
            app.action(action_id)(self._action_handler(handler))

    @staticmethod
    def _action_handler(handler):
        def _handle(ack, body, client):
            ack()
            handler(body, client)

        return _handle

    # ---------------------------------------------------------------- triggers

    def on_command(self, command: Mapping, client, respond) -> None:
        channel, user = command["channel_id"], command["user_id"]
        text = command.get("text", "")
        if not self._channel_allowed(channel):
            respond(text="Bu kanalda Shorts üretimi kapalı.", response_type="ephemeral")
            return
        try:
            request = self.settings.parse(text)
        except ss.ShortRequestError as exc:
            respond(text=str(exc), response_type="ephemeral")
            return
        try:
            anchor = client.chat_postMessage(
                channel=channel, text=f"{_mention(user)} Short isteği: *{request.subject}*"
            )
        except Exception:
            logger.exception("slack: cannot post /short anchor message")
            respond(
                text="Bu kanala yazamıyorum. Önce `/invite @Echo Shorts` ile beni ekleyin.",
                response_type="ephemeral",
            )
            return
        self._enqueue(client, request, channel, anchor["ts"], user)

    def on_mention(self, event: Mapping, client) -> None:
        if event.get("bot_id") and event["bot_id"] not in self.settings.allowed_bot_ids:
            return
        self._handle_message(event, client, event.get("user") or event.get("bot_id", ""))

    def on_agent_message(self, message: Mapping, client) -> None:
        # Humans use /short or a mention; this path lets allowlisted agents
        # such as Mila queue a Short by writing "short: <konu>".
        bot_id = message.get("bot_id")
        if not bot_id or bot_id not in self.settings.allowed_bot_ids:
            return
        self._handle_message(message, client, message.get("user") or bot_id)

    def _handle_message(self, event: Mapping, client, requester: str) -> None:
        channel, ts = event["channel"], event["ts"]
        # A message that both starts with "short:" and mentions the bot
        # arrives as two events; only the first may start a (possibly paid) job.
        with self._lock:
            if (channel, ts) in self._seen:
                return
            self._seen[(channel, ts)] = True
        if not self._channel_allowed(channel):
            return
        thread_ts = event.get("thread_ts") or ts
        try:
            request = self.settings.parse(event.get("text", ""))
        except ss.ShortRequestError as exc:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=str(exc))
            return
        self._enqueue(client, request, channel, thread_ts, requester)

    def _channel_allowed(self, channel: str) -> bool:
        return not self.settings.allowed_channels or channel in self.settings.allowed_channels

    def _enqueue(self, client, request, channel, thread_ts, requester) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = Job(request, channel, thread_ts, requester)
        self.submit(self.run_job, client, job_id)
        return job_id

    # --------------------------------------------------------------- execution

    def run_job(self, client, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return
        request = job.request
        notes = "".join(f"\n> {warning}" for warning in request.warnings)
        status = client.chat_postMessage(
            channel=job.channel,
            thread_ts=job.thread_ts,
            text=f":clapper: *{request.subject}* hazırlanıyor "
            f"({request.language}, {request.aspect}, {request.count} adet)…{notes}",
        )

        def update(text: str) -> None:
            try:
                client.chat_update(channel=job.channel, ts=status["ts"], text=text)
            except Exception:
                # A deleted or uneditable status message must not abort a
                # render that is already being paid for.
                logger.warning(f"slack: status update failed for job {job_id}")

        last_update = [self.clock()]

        def on_progress(task: Mapping) -> None:
            if self.clock() - last_update[0] < self.progress_interval:
                return
            last_update[0] = self.clock()
            update(f":hourglass_flowing_sand: *{request.subject}* — %{task.get('progress', 0)}")

        try:
            payload = ss.build_video_payload(request, dict(self.settings.preset))
            job.task_id = self.mpt.create_video(payload)
            logger.info(f"slack job {job_id} -> MPT task {job.task_id}")
            task = self.mpt.wait_for_task(
                job.task_id,
                on_progress=on_progress,
                poll_interval=self.settings.poll_interval_seconds,
                timeout=self.settings.job_timeout_seconds,
                sleep=self.sleep,
                clock=self.clock,
            )
            videos = task.get("videos") or []
            if not videos:
                raise ss.MptApiError("Görev tamamlandı ama video dosyası döndürmedi.")
            update(f":outbox_tray: *{request.subject}* hazır, Slack'e yükleniyor…")
            self._upload(client, job, job_id, videos)
            script = str(task.get("script") or request.script or "").strip()
            self._post_review(client, job_id, job, script)
            update(f":white_check_mark: *{request.subject}* hazır — incelemeniz bekleniyor.")
        except Exception as exc:
            logger.exception(f"slack job {job_id} failed")
            update(f":x: *{request.subject}* üretilemedi: {_user_facing_error(exc)}")

    def _upload(self, client, job: Job, job_id: str, videos: list[str]) -> None:
        with tempfile.TemporaryDirectory(prefix="echo-shorts-") as tmp:
            uploads = []
            for index, uri in enumerate(videos, start=1):
                path = self.mpt.download(uri, os.path.join(tmp, f"short-{index}.mp4"))
                uploads.append(
                    {
                        "file": path,
                        "filename": f"short-{job_id}-{index}.mp4",
                        "title": f"{job.request.subject} ({index}/{len(videos)})",
                    }
                )
            client.files_upload_v2(
                channel=job.channel, thread_ts=job.thread_ts, file_uploads=uploads
            )

    def _post_review(self, client, job_id: str, job: Job, script: str) -> None:
        meta = ss.youtube_metadata(job.request, script, self.settings.profile)
        slots = ss.next_posting_slots(self.now(), self.settings.posting_slots, self.settings.timezone)
        slot_text = ", ".join(ss.format_slot(slot) for slot in slots) or "—"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Başlık:* {meta['title']}\n"
                    f"*Etiketler:* {', '.join(meta['tags'])}\n"
                    f"*Önerilen saatler ({self.settings.timezone}):* {slot_text}\n"
                    f"*İsteyen:* {_mention(job.requester)}",
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
                    _button("short_approve", "Onayla", job_id, "primary"),
                    _button("short_regenerate", "Yeniden üret", job_id),
                    _button("short_reject", "Reddet", job_id, "danger"),
                ],
            },
        ]
        client.chat_postMessage(
            channel=job.channel,
            thread_ts=job.thread_ts,
            text=f"İnceleme: {meta['title']}",
            blocks=blocks,
        )

    # ----------------------------------------------------------------- reviews

    def on_approve(self, body: Mapping, client) -> None:
        self._close_review(body, client, ":white_check_mark: <@{user}> onayladı. Yayını hesap sahibi yapar.")

    def on_reject(self, body: Mapping, client) -> None:
        self._close_review(body, client, ":x: <@{user}> reddetti.")

    def on_regenerate(self, body: Mapping, client) -> None:
        job_id = self._close_review(body, client, ":repeat: <@{user}> yeniden üretim istedi.")
        if job_id is None:
            return
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            client.chat_postMessage(
                channel=body["channel"]["id"],
                thread_ts=body["message"].get("thread_ts"),
                text="Bu isteğin kaydı artık yok (bot yeniden başlamış olabilir); lütfen tekrar gönderin.",
            )
            return
        request = replace(job.request, warnings=[])
        self._enqueue(client, request, job.channel, job.thread_ts, body["user"]["id"])

    def _close_review(self, body: Mapping, client, verdict: str) -> str | None:
        user = body["user"]["id"]
        channel = body["channel"]["id"]
        if self.settings.approver_user_ids and user not in self.settings.approver_user_ids:
            client.chat_postEphemeral(
                channel=channel, user=user, text="Bu Short için karar yetkiniz yok."
            )
            return None
        job_id = body["actions"][0]["value"]
        with self._lock:
            already_decided = ("review", job_id) in self._seen
            self._seen[("review", job_id)] = user
        if already_decided:
            # Two people clicking at once must not start two regenerations.
            client.chat_postEphemeral(channel=channel, user=user, text="Bu Short için karar zaten verildi.")
            return None
        message = body["message"]
        blocks = [block for block in message.get("blocks", []) if block.get("type") != "actions"]
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": verdict.format(user=user)}]}
        )
        client.chat_update(
            channel=channel, ts=message["ts"], text=message.get("text", ""), blocks=blocks
        )
        return job_id


def _button(action_id: str, text: str, value: str, style: str | None = None) -> dict:
    button = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": text},
        "value": value,
    }
    if style:
        button["style"] = style
    return button
