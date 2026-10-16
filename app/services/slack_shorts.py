"""Slack -> MoneyPrinterTurbo bridge logic for YouTube Shorts production.

This module holds everything the Slack bot needs that does not depend on the
Slack SDK: parsing a request message, turning it into a /api/v1/videos payload
with the brand preset applied, talking to the MPT API, and suggesting posting
slots. Keeping it SDK-free lets it be unit-tested without slack_bolt installed.

Request format (slash command, @mention, or a message from an allowed agent
such as Mila):

    short: Yazılım projelerinde sakin planlama
    dil: tr
    ses: erkek
    adet: 2
    ---
    Optional ready-made script. When present, the LLM script step is skipped
    and this text is narrated as-is.

Header options may also be written inline, e.g. ``/short dil:en Calm releases``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import requests

TASK_STATE_FAILED = -1
TASK_STATE_COMPLETE = 1

# "short:" / "shorts:" (agent messages) or a literal "/short" word. The colon or
# word boundary is required so subjects such as "Shortcut tips" stay intact.
TRIGGER_PATTERN = re.compile(r"^\s*(?:/short\b|shorts?\s*:)\s*", re.IGNORECASE)
AGENT_TRIGGER_PATTERN = re.compile(r"^\s*shorts?\s*:", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
OPTION_PATTERN = re.compile(
    r"(?<!\S)(dil|lang|ses|voice|adet|count|format|aspect|kaynak|source)\s*:\s*(\S+)",
    re.IGNORECASE,
)

MAX_SUBJECT_LENGTH = 200
MAX_SCRIPT_LENGTH = 5000
# Slack rejects uploads above 1 GB; stop downloading before that.
MAX_VIDEO_BYTES = 1024 * 1024 * 1024

# Edge TTS voices per language. Each brand language gets one female and one
# male voice so requests can say "ses: erkek" without knowing voice IDs.
VOICES = {
    "tr": {"female": "tr-TR-EmelNeural-Female", "male": "tr-TR-AhmetNeural-Male"},
    "en": {"female": "en-US-AvaNeural-Female", "male": "en-US-AndrewNeural-Male"},
    "de": {"female": "de-DE-KatjaNeural-Female", "male": "de-DE-ConradNeural-Male"},
}

_LANGUAGE_ALIASES = {
    "tr": "tr", "turkce": "tr", "türkçe": "tr", "turkish": "tr",
    "en": "en", "ingilizce": "en", "english": "en",
    "de": "de", "almanca": "de", "german": "de", "deutsch": "de",
}
_GENDER_ALIASES = {
    "kadin": "female", "kadın": "female", "female": "female", "k": "female",
    "erkek": "male", "male": "male", "e": "male",
}
_ASPECT_ALIASES = {
    "dikey": "9:16", "shorts": "9:16", "9:16": "9:16", "portrait": "9:16",
    "yatay": "16:9", "16:9": "16:9", "landscape": "16:9",
    "kare": "1:1", "1:1": "1:1", "square": "1:1",
}
_OPTION_KEYS = {
    "dil": "language", "lang": "language",
    "ses": "voice", "voice": "voice",
    "adet": "count", "count": "count",
    "format": "aspect", "aspect": "aspect",
    "kaynak": "source", "source": "source",
}

# ECHO brand voice: calm, orderly, careful. Passed to the LLM as extra script
# requirements so generated scripts match the tone used on madebyecho.xyz.
ECHO_SCRIPT_PROMPT = (
    "Write for a YouTube Shorts video under 45 seconds. Open with a one-sentence "
    "hook in the first 2 seconds. Tone: calm, orderly, careful and plain; no hype, "
    "no exaggerated claims, no emojis. Short sentences that read well as subtitles. "
    "End with a single quiet call to action mentioning madebyecho.xyz."
)

DEFAULT_POSTING_SLOTS = ("12:30", "19:00", "21:00")


class ShortRequestError(ValueError):
    """Raised when a Slack message cannot be turned into a video request."""


@dataclass
class ShortRequest:
    subject: str
    script: str = ""
    language: str = "tr"
    gender: str = "female"
    count: int = 1
    aspect: str = "9:16"
    source: str = ""
    warnings: list[str] = field(default_factory=list)


def parse_request(text: str, default_language: str = "tr") -> ShortRequest:
    """Parse a Slack message body into a ShortRequest."""
    text = MENTION_PATTERN.sub("", text or "").strip()
    text = TRIGGER_PATTERN.sub("", text, count=1)

    header, script = text, ""
    parts = re.split(r"^\s*---\s*$", text, maxsplit=1, flags=re.MULTILINE)
    if len(parts) == 2:
        header, script = parts[0], parts[1].strip()

    request = ShortRequest(subject="", language=default_language)
    subject_lines = []
    for line in header.splitlines():
        for raw_key, raw_value in OPTION_PATTERN.findall(line):
            _apply_option(request, _OPTION_KEYS[raw_key.lower()], raw_value)
        remainder = OPTION_PATTERN.sub("", line).strip()
        if remainder:
            subject_lines.append(remainder)

    request.subject = " ".join(subject_lines).strip()[:MAX_SUBJECT_LENGTH]
    if len(script) > MAX_SCRIPT_LENGTH:
        request.warnings.append(f"Senaryo {MAX_SCRIPT_LENGTH} karaktere kısaltıldı.")
        script = script[:MAX_SCRIPT_LENGTH]
    request.script = script
    if not request.subject and request.script:
        request.subject = request.script.splitlines()[0][:MAX_SUBJECT_LENGTH]
    if not request.subject:
        raise ShortRequestError(
            "Konu bulunamadı. Örnek: `/short Sakin yazılım planlaması dil:tr`"
        )
    return request


def _apply_option(request: ShortRequest, key: str, raw_value: str) -> None:
    value = raw_value.strip().lower()
    if key == "language":
        if value in _LANGUAGE_ALIASES:
            request.language = _LANGUAGE_ALIASES[value]
        else:
            request.warnings.append(f"Bilinmeyen dil `{raw_value}`, varsayılan kullanıldı.")
    elif key == "voice":
        if value in _GENDER_ALIASES:
            request.gender = _GENDER_ALIASES[value]
        else:
            request.warnings.append(f"Bilinmeyen ses `{raw_value}`, kadın sesi kullanıldı.")
    elif key == "count":
        try:
            request.count = min(max(int(value), 1), 5)
        except ValueError:
            request.warnings.append(f"Geçersiz adet `{raw_value}`, 1 kullanıldı.")
    elif key == "aspect":
        if value in _ASPECT_ALIASES:
            request.aspect = _ASPECT_ALIASES[value]
        else:
            request.warnings.append(f"Bilinmeyen format `{raw_value}`, 9:16 kullanıldı.")
    elif key == "source":
        request.source = value


def build_video_payload(request: ShortRequest, settings: dict | None = None) -> dict:
    """Build the JSON body for POST /api/v1/videos with the brand preset."""
    settings = settings or {}
    voices = VOICES.get(request.language, VOICES["tr"])
    payload: dict[str, Any] = {
        "video_subject": request.subject,
        "video_script": request.script,
        "video_language": request.language,
        "video_aspect": request.aspect,
        "video_count": request.count,
        "video_clip_duration": 3,
        "voice_name": voices[request.gender],
        "voice_rate": 1.05,
        "bgm_type": "random",
        "bgm_volume": 0.15,
        "subtitle_enabled": True,
        "subtitle_position": "two_thirds_bottom",
        # BeVietnamPro covers Latin Extended (ğ, ş, ı, İ, ä, ö, ü, ß) unlike
        # the CJK default font, so Turkish and German subtitles render fully.
        "font_name": settings.get("font_name", "BeVietnamPro-Bold.ttf"),
        "font_size": 64,
        "stroke_width": 2.0,
        "match_materials_to_script": True,
    }
    source = request.source or settings.get("video_source", "")
    if source:
        payload["video_source"] = source
    if not request.script:
        payload["video_script_prompt"] = settings.get("script_prompt", ECHO_SCRIPT_PROMPT)
    return payload


def next_posting_slots(
    now: datetime,
    slots: Iterable[str] = DEFAULT_POSTING_SLOTS,
    timezone: str = "Europe/Istanbul",
    count: int = 3,
    min_lead: timedelta = timedelta(minutes=30),
) -> list[datetime]:
    """Return the next ``count`` posting times at or after ``now + min_lead``."""
    tz = ZoneInfo(timezone)
    local_now = now.astimezone(tz)
    parsed = sorted(parse_slot(slot) for slot in slots)
    if not parsed:
        return []
    earliest = local_now + min_lead
    result: list[datetime] = []
    day = local_now.date()
    while len(result) < count:
        for hour, minute in parsed:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if candidate >= earliest:
                result.append(candidate)
                if len(result) == count:
                    break
        day += timedelta(days=1)
    return result


def parse_slot(slot: str) -> tuple[int, int]:
    hour, _, minute = str(slot).partition(":")
    h, m = int(hour), int(minute or 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid posting slot: {slot}")
    return h, m


_TR_DAYS = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")


def format_slot(slot: datetime) -> str:
    return f"{_TR_DAYS[slot.weekday()]} {slot:%d.%m %H:%M}"


def youtube_metadata(request: ShortRequest, script: str = "") -> dict:
    """Draft title/description/tags for a human to review before publishing."""
    title = request.subject.strip()
    if len(title) > 90:
        title = title[:87].rstrip() + "..."
    title = f"{title} #Shorts"
    body = (script or request.script).strip()
    description = (body[:400] + "\n\n" if body else "") + "madebyecho.xyz"
    words = re.findall(r"\w{4,}", request.subject.lower())
    tags = ["shorts", "echo"] + list(dict.fromkeys(words))[:8]
    return {"title": title, "description": description, "tags": tags}


class MptApiError(RuntimeError):
    pass


class MptClient:
    """Minimal client for the MoneyPrinterTurbo HTTP API."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 30,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        if api_key:
            self.session.headers["x-api-key"] = api_key

    def ping(self) -> bool:
        try:
            return self.session.get(f"{self.base_url}/ping", timeout=self.timeout).status_code == 200
        except requests.RequestException:
            return False

    def create_video(self, payload: dict) -> str:
        response = self.session.post(
            f"{self.base_url}/api/v1/videos", json=payload, timeout=self.timeout
        )
        data = self._json(response)
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise MptApiError(f"API task_id döndürmedi: {data}")
        return task_id

    def get_task(self, task_id: str) -> dict:
        response = self.session.get(
            f"{self.base_url}/api/v1/tasks/{task_id}", timeout=self.timeout
        )
        return self._json(response).get("data") or {}

    def wait_for_task(
        self,
        task_id: str,
        on_progress: Callable[[dict], None] | None = None,
        poll_interval: float = 5,
        timeout: float = 1800,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict:
        deadline = clock() + timeout
        last_progress = None
        while True:
            task = self.get_task(task_id)
            state = task.get("state")
            if state == TASK_STATE_COMPLETE:
                return task
            if state == TASK_STATE_FAILED:
                stage = task.get("failed_stage") or "bilinmiyor"
                raise MptApiError(f"Üretim başarısız ({stage}): {task.get('error') or ''}".strip())
            progress = task.get("progress")
            if on_progress and progress != last_progress:
                last_progress = progress
                on_progress(task)
            if clock() >= deadline:
                raise MptApiError(f"Zaman aşımı: görev {task_id} {int(timeout)} sn içinde bitmedi")
            sleep(poll_interval)

    def download(self, uri: str, destination: str, max_bytes: int = MAX_VIDEO_BYTES) -> str:
        # The API builds artifact links from its public `endpoint` setting, which
        # may differ from the address the bot reaches it on. Only the /tasks/
        # path is reused, so the API key is never sent to another host.
        path = urlparse(uri).path
        if not path.startswith("/tasks/") or ".." in unquote(path).split("/"):
            raise MptApiError(f"Beklenmeyen video adresi: {uri}")
        url = f"{self.base_url}{path}"
        written = 0
        with self.session.get(url, stream=True, timeout=self.timeout) as response:
            if response.status_code != 200:
                raise MptApiError(f"Video indirilemedi: HTTP {response.status_code}")
            with open(destination, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise MptApiError("Video Slack yükleme sınırını aşıyor")
                    f.write(chunk)
        return destination

    @staticmethod
    def _json(response: requests.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200:
            message = data.get("message") if isinstance(data, dict) else None
            raise MptApiError(f"API hatası HTTP {response.status_code}: {message or response.text[:200]}")
        return data
