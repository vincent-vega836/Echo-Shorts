"""Free image material for the "mixed_image" source.

Each search term has the form ``"<archive query> :: <image prompt>"``. The
archive query looks for a real public-domain picture on Wikimedia Commons
(paintings, engravings, photographs: authentic and free); when none fits, the
image prompt is rendered by a local text-to-image command (e.g. FLUX on Apple
Silicon via mflux), which is free and unlimited. Every picture is framed onto
a portrait canvas over a blurred copy of itself so the later cover-fit never
crops a landscape painting down to a sliver.
"""

from __future__ import annotations

import io
import os
import random
import re
import shlex
import subprocess
import unicodedata
import uuid
from typing import Callable, List

import requests
from loguru import logger
from PIL import Image, ImageEnhance, ImageFilter

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia asks API clients to identify themselves.
USER_AGENT = "EchoShorts/1.0 (https://github.com/vincent-vega836/Echo-Shorts)"
TERM_SEPARATOR = "::"
# Marks a stock-video term that should show a real archive picture instead.
ARCHIVE_PREFIX = "archive:"
MIN_SHORT_SIDE = 600
MAX_IMAGE_BYTES = 25 * 1024 * 1024
CANVAS_SIZES = {
    VideoAspect.portrait: (1080, 1920),
    VideoAspect.landscape: (1920, 1080),
    VideoAspect.square: (1080, 1080),
}
# Generated at this size (multiples of 16) and upscaled onto the canvas.
LOCAL_SIZES = {
    VideoAspect.portrait: (720, 1280),
    VideoAspect.landscape: (1280, 720),
    VideoAspect.square: (1024, 1024),
}
DEFAULT_LOCAL_TEMPLATE = (
    "Photorealistic historical drama film still, period-accurate costumes, "
    "props and architecture. {term}. Dramatic natural light, shallow depth "
    "of field, subtle film grain. No modern clothing, shoes or objects, no "
    "text, no watermark."
)
# A landscape picture on a portrait canvas fills at least this share of the
# canvas height; its sides are cropped instead of shrinking it to a thin strip.
MIN_FILL = 0.6
# A Commons hit must share at least half of the query's words in its title or
# object name. The search engine alone returns loose matches, and descriptions
# and categories are too broad (a grave inside St. Vitus Cathedral is no
# "Saint Vitus shrine").
QUERY_STOPWORDS = frozenset({"a", "an", "and", "at", "de", "du", "for", "in", "la", "le", "of", "on", "the", "with"})
# Words that mark a crop or copy of the same work; a video shows a work once.
VARIANT_WORDS = frozenset({"crop", "cropped", "detail", "details", "ausschnitt", "file", "jpg", "jpeg", "png", "tif", "tiff"})


class FreeImageError(RuntimeError):
    pass


def split_term(term: str) -> tuple[str, str]:
    """``"query :: prompt"`` -> (query, prompt); a plain term serves as both."""
    query, sep, prompt = term.partition(TERM_SEPARATOR)
    query, prompt = query.strip(), prompt.strip()
    if not sep:
        return query, query
    return query or prompt, prompt or query


def _is_free_license(extmetadata: dict) -> bool:
    """Public domain and CC0 only: they need no on-screen attribution."""
    def value(key: str) -> str:
        entry = extmetadata.get(key) or {}
        return str(entry.get("value", "") if isinstance(entry, dict) else entry).lower()

    license_id = value("License")
    short_name = value("LicenseShortName")
    return (
        license_id.startswith("pd")
        or license_id == "cc0"
        or "public domain" in short_name
        or short_name.startswith("cc0")
    )


def _words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", re.sub(r"<[^>]+>", " ", text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return set(re.findall(r"[a-z0-9]+", text))


def _is_relevant(query: str, title: str, extmetadata: dict) -> bool:
    wanted = _words(query) - QUERY_STOPWORDS
    if not wanted:
        return True
    object_name = str((extmetadata.get("ObjectName") or {}).get("value", ""))
    found = _words(f"{title} {object_name}")
    return len(wanted & found) * 2 >= len(wanted)


def _work_key(title: str) -> str:
    """Same key for a file and its crops ("... detail.jpg", "... (cropped).jpg")."""
    return " ".join(sorted(_words(title) - VARIANT_WORDS))


def search_commons(query: str, used: set[str], session=requests) -> tuple[bytes, dict] | None:
    """Return (image bytes, source info) for the best free Commons match."""
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": 1600,
        "iiextmetadatafilter": "License|LicenseShortName|Artist|ObjectName",
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        response = session.get(COMMONS_API, params=params, headers=headers, timeout=(10, 30))
        pages = (response.json().get("query") or {}).get("pages") or {}
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning(f"commons search failed: query={query!r}, error={type(exc).__name__}")
        return None

    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        title = page.get("title", "")
        info = (page.get("imageinfo") or [{}])[0]
        if title in used or _work_key(title) in used or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if min(int(info.get("width") or 0), int(info.get("height") or 0)) < MIN_SHORT_SIDE:
            continue
        extmetadata = info.get("extmetadata") or {}
        if not _is_free_license(extmetadata) or not _is_relevant(query, title, extmetadata):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        try:
            image = session.get(url, headers=headers, timeout=(10, 60))
            if image.status_code != 200 or len(image.content) > MAX_IMAGE_BYTES:
                continue
        except requests.RequestException:
            continue
        used.update((title, _work_key(title)))
        source = {
            "provider": "wikimedia_commons",
            "asset_id": title,
            "source_page": info.get("descriptionurl"),
        }
        return image.content, source
    return None


def generate_local(prompt: str, aspect: VideoAspect, out_dir: str, run=subprocess.run) -> bytes | None:
    """Run ``local_image_command`` (placeholders: prompt, width, height, seed, output)."""
    template = str(config.app.get("local_image_command", "") or "").strip()
    if not template:
        return None
    style = str(config.app.get("local_image_prompt_template", "") or DEFAULT_LOCAL_TEMPLATE)
    full_prompt = style.replace("{term}", prompt) if "{term}" in style else prompt
    width, height = LOCAL_SIZES[VideoAspect(aspect)]
    output = os.path.join(out_dir, f"local-{uuid.uuid4().hex[:12]}.png")
    values = {
        "{prompt}": full_prompt,
        "{width}": str(width),
        "{height}": str(height),
        "{seed}": str(random.randint(1, 2**31 - 1)),
        "{output}": output,
    }
    # Placeholders are filled per argument after splitting, never through a
    # shell, so a prompt cannot inject extra arguments or commands.
    command = []
    for token in shlex.split(template):
        for key, val in values.items():
            token = token.replace(key, val)
        command.append(token)
    timeout = float(config.app.get("local_image_timeout", 600) or 600)
    try:
        completed = run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error(f"local image command failed: {type(exc).__name__}: {exc}")
        return None
    if completed.returncode != 0 or not os.path.isfile(output):
        logger.error(f"local image command failed: {(completed.stderr or '')[-300:]}")
        return None
    with open(output, "rb") as f:
        data = f.read()
    os.remove(output)
    return data


def frame_for_canvas(image_bytes: bytes, aspect: VideoAspect) -> Image.Image:
    """Fit the picture on the target canvas over a blurred, darkened copy."""
    width, height = CANVAS_SIZES[VideoAspect(aspect)]
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            picture = source.convert("RGB")
    except (OSError, ValueError) as exc:
        raise FreeImageError(f"not a decodable image: {exc}") from exc

    scale = max(width / picture.width, height / picture.height)
    background = picture.resize(
        (max(width, round(picture.width * scale)), max(height, round(picture.height * scale)))
    )
    left = (background.width - width) // 2
    top = (background.height - height) // 2
    background = background.crop((left, top, left + width, top + height))
    background = ImageEnhance.Brightness(background.filter(ImageFilter.GaussianBlur(40))).enhance(0.55)

    fit = min(width / picture.width, height / picture.height)
    if picture.width / picture.height > width / height:
        fill = MIN_FILL * height / picture.height
    else:
        fill = MIN_FILL * width / picture.width
    front_scale = max(fit, min(scale, fill))
    front = picture.resize((round(picture.width * front_scale), round(picture.height * front_scale)))
    left = max(0, (front.width - width) // 2)
    top = max(0, (front.height - height) // 2)
    front = front.crop((left, top, left + min(width, front.width), top + min(height, front.height)))
    background.paste(front, ((width - front.width) // 2, (height - front.height) // 2))
    return background


def generate_images_mixed(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    save_dir: str = "",
    used: set[str] | None = None,
    commons: Callable[..., tuple[bytes, dict] | None] = search_commons,
    local: Callable[..., bytes | None] = generate_local,
) -> List[MaterialInfo]:
    """One picture for one narration moment: archive first, then local AI."""
    used = set() if used is None else used
    save_dir = save_dir or os.path.join(os.getcwd(), "storage", "cache_images")
    os.makedirs(save_dir, exist_ok=True)
    query, prompt = split_term(search_term)

    found = commons(query, used) if query else None
    if found:
        image_bytes, source = found
    else:
        image_bytes = local(prompt, video_aspect, save_dir) if prompt else None
        source = {"provider": "local_image", "asset_id": None, "source_page": None}
    if not image_bytes:
        logger.warning(f"no free image for term: {search_term!r}")
        return []

    try:
        canvas = frame_for_canvas(image_bytes, video_aspect)
    except FreeImageError as exc:
        logger.warning(f"skipping undecodable image for {search_term!r}: {exc}")
        return []
    path = os.path.join(save_dir, f"free-image-{uuid.uuid4().hex[:12]}.jpg")
    canvas.save(path, quality=92)
    logger.info(f"free image ({source['provider']}): {source.get('asset_id') or prompt[:60]}")

    item = MaterialInfo()
    item.provider = "mixed_image"
    item.url = path
    item.duration = max(int(minimum_duration), 1)
    item.source_info = {
        **source,
        "search_term": search_term,
        "rendition": {"id": None, "width": canvas.width, "height": canvas.height},
    }
    return [item]
