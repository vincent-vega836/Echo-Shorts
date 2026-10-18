"""Command-line entry point for agents (such as Mila) that run a Shorts channel.

Agents that already live in Slack cannot share the Echo Shorts bot's Socket
Mode connection, and a workspace may have no room for another app. These
commands give such an agent the same request grammar and channel preset, plus
the weekly batch / approval / publishing loop, without a Slack app:

    render  "short: <topic>"          render one Short (default command)
    batch   --plan week.json          render a week and queue it for approval
    queue   [--status pending]        list queued Shorts
    approve ID... | --all-pending     a person approved these in Slack
    reject  ID...                     a person rejected these
    publish-due [--dry-run]           upload approved Shorts whose slot passed
    compile --out long.mp4 [--last N] join Shorts into a 16:9 long video

Every command prints one JSON document on stdout; progress goes to stderr.
Exit codes: 0 success, 1 a render/upload/compile failed, 2 invalid input or
settings. Approval is always a person's decision: run approve/reject only on
their explicit instruction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Callable, Mapping, TextIO

from app.services import shorts_compile, shorts_queue
from app.services import slack_shorts as ss
from app.services.slack_shorts_bot import SlackSettings, load_settings

COMMANDS = ("render", "batch", "queue", "approve", "reject", "publish-due", "compile")
WEEK = 7
SUBTITLE_SYNC_WARNING = (
    "ElevenLabs returns no word timings: set [app] subtitle_provider = \"whisper\" "
    "(e.g. [whisper] model_size = \"small.en\") or word-by-word subtitles will drift"
)


def subtitle_sync_warning(settings: SlackSettings, subtitle_provider: str) -> str:
    uses_elevenlabs = bool(settings.preset.get("elevenlabs_voice"))
    word_level = ss.get_profile(settings.profile).subtitle_display_mode == "word_by_word"
    if uses_elevenlabs and word_level and subtitle_provider.strip().lower() != "whisper":
        return SUBTITLE_SYNC_WARNING
    return ""


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
        **ss.youtube_metadata(request, script, settings.profile),
        "references": request.references,
        "posting_slots": [{"at": slot.isoformat(), "label": ss.format_slot(slot)} for slot in slots],
        "warnings": request.warnings,
    }


def _read_plan(path: str) -> list[dict]:
    """A plan is a JSON list of request texts or {"text", "category"} objects."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read plan {path}: {exc}") from None
    if not isinstance(raw, list) or not raw:
        raise ValueError("plan must be a non-empty JSON list")
    plan = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
            raise ValueError(f"plan item {index} needs a non-empty 'text'")
        plan.append({"text": str(entry["text"]), "category": str(entry.get("category", ""))})
    return plan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shorts_cli.py", description="Run a YouTube Shorts channel via the MPT API.")
    parser.add_argument("--queue", help="queue file (default: [slack].queue_path)")
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="render one Short")
    render.add_argument("text", nargs="?", help="request text; '-' or omitted reads stdin")
    render.add_argument("--out", default="shorts", help="directory for the MP4 files")

    batch = sub.add_parser("batch", help="render a plan and queue it for approval")
    batch.add_argument("--plan", required=True, help="JSON list of requests")
    batch.add_argument("--out", default="shorts", help="directory for the MP4 files")
    batch.add_argument("--limit", type=int, default=WEEK, help=f"max items to render (default {WEEK})")

    queue = sub.add_parser("queue", help="list queued Shorts")
    queue.add_argument("--status", choices=shorts_queue.STATUSES)

    for name in ("approve", "reject"):
        cmd = sub.add_parser(name, help=f"{name} pending Shorts on a person's instruction")
        cmd.add_argument("ids", nargs="*")
        cmd.add_argument("--all-pending", action="store_true")

    publish = sub.add_parser("publish-due", help="upload approved Shorts whose slot has passed")
    publish.add_argument("--dry-run", action="store_true")

    compile_cmd = sub.add_parser("compile", help="join Shorts into a 16:9 long video")
    compile_cmd.add_argument("clips", nargs="*")
    compile_cmd.add_argument("--last", type=int, default=0, help="use the last N published Shorts from the queue")
    compile_cmd.add_argument("--out", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    section: Mapping | None = None,
    environ: Mapping[str, str] | None = None,
    fallback_api_key: str = "",
    mpt_factory: Callable[[SlackSettings], ss.MptClient] | None = None,
    uploader: Callable[..., dict] | None = None,
    compiler: Callable[[list[str], str], str] | None = None,
    subtitle_provider: str = "edge",
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    # `shorts_cli.py "short: ..."` keeps working as shorthand for `render`.
    if _command_index(argv) is None and "-h" not in argv and "--help" not in argv:
        split = _global_options_end(argv)
        argv = [*argv[:split], "render", *argv[split:]]
    args = _build_parser().parse_args(argv)

    def emit(code: int, document) -> int:
        json.dump(document, stdout, ensure_ascii=False, indent=2)
        stdout.write("\n")
        return code

    def log(message: str) -> None:
        print(message, file=stderr, flush=True)

    try:
        settings = load_settings(section or {}, environ if environ is not None else os.environ, fallback_api_key)
    except ValueError as exc:
        return emit(2, {"error": str(exc)})
    queue_path = args.queue or settings.queue_path
    sync_warning = subtitle_sync_warning(settings, subtitle_provider)
    if sync_warning and args.command in ("render", "batch"):
        log(f"warning: {sync_warning}")
    mpt_factory = mpt_factory or (lambda s: ss.MptClient(s.mpt_api_url, s.mpt_api_key))

    try:
        if args.command == "render":
            text = sys.stdin.read() if args.text in (None, "-") else args.text
            request = settings.parse(text)
            if sync_warning:
                request.warnings.append(sync_warning)
            return emit(0, render_short(request, settings, mpt_factory(settings), args.out, now(), log))

        if args.command == "batch":
            plan = _read_plan(args.plan)[: max(args.limit, 0)]
            requests = [(settings.parse(entry["text"]), entry) for entry in plan]
            return _batch(requests, settings, mpt_factory(settings), args.out, queue_path, now(), log, emit)

        items = shorts_queue.load(queue_path)
        if args.command == "queue":
            return emit(0, [i for i in items if not args.status or i["status"] == args.status])

        if args.command in ("approve", "reject"):
            ids = [i["id"] for i in items if i["status"] == shorts_queue.PENDING] if args.all_pending else args.ids
            if not ids:
                raise ValueError("give queue ids or --all-pending")
            status = shorts_queue.APPROVED if args.command == "approve" else shorts_queue.REJECTED
            changed = shorts_queue.set_status(items, ids, status)
            shorts_queue.save(queue_path, items)
            return emit(0, changed)

        if args.command == "publish-due":
            if args.dry_run:
                return emit(0, shorts_queue.due(items, now()))
            upload = uploader or _default_uploader()
            handled = shorts_queue.publish_due(items, now(), upload, settings.publish_platforms)
            shorts_queue.save(queue_path, items)
            failed = any(i["status"] != shorts_queue.PUBLISHED for i in handled)
            return emit(1 if failed else 0, handled)

        if args.command == "compile":
            clips = list(args.clips)
            if args.last:
                published = [i for i in items if i["status"] == shorts_queue.PUBLISHED]
                published.sort(key=lambda i: i["publish_at"])
                clips += [i["video"] for i in published[-args.last:]]
            output = (compiler or shorts_compile.compile_shorts)(clips, args.out)
            return emit(0, {"output": output, "clips": clips})
    except (ValueError, shorts_queue.QueueError) as exc:  # includes ShortRequestError
        return emit(2, {"error": str(exc)})
    except (ss.MptApiError, shorts_compile.CompileError) as exc:
        return emit(1, {"error": str(exc)})
    except ss.requests.RequestException:
        return emit(1, {
            "error": f"MPT API'ye ulaşılamadı: {settings.mpt_api_url} "
            "(sunucu çalışıyor mu? Başka bir terminalde: uv run python main.py)"
        })
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


def _global_options_end(argv: list[str]) -> int:
    """Index just past the leading global options (`--queue PATH`)."""
    index = 0
    while index < len(argv):
        if argv[index] == "--queue":
            index += 2
        elif argv[index].startswith("--queue="):
            index += 1
        else:
            break
    return min(index, len(argv))


def _command_index(argv: list[str]) -> int | None:
    index = _global_options_end(argv)
    return index if index < len(argv) and argv[index] in COMMANDS else None


def _batch(requests, settings, mpt, out_dir, queue_path, now, log, emit) -> int:
    items = shorts_queue.load(queue_path)
    sources = [s for s in settings.preset.get("video_sources", "").split(",") if s]
    queued, failed = [], []
    for index, (request, entry) in enumerate(requests):
        if not request.source and sources:
            # Alternate stock libraries so the same clips do not keep recurring.
            request.source = sources[index % len(sources)]
        log(f"[{index + 1}/{len(requests)}] {request.subject}")
        try:
            result = render_short(request, settings, mpt, out_dir, now, log)
        except (ss.MptApiError, ss.requests.RequestException) as exc:
            failed.append({"text": entry["text"], "error": str(exc)})
            continue
        # Take the slot only after a successful render so a failure leaves no gap day.
        slot = shorts_queue.free_slots(items, now, 1, settings.posting_slots, settings.timezone)[0]
        queued.append(shorts_queue.add(items, result, slot, entry["category"]))
        # Save after every render so a crash mid-week keeps finished videos queued.
        shorts_queue.save(queue_path, items)
    return emit(0 if queued else 1, {"queued": queued, "failed": failed})


def _default_uploader() -> Callable[..., dict]:
    from app.services import upload_post

    if not upload_post.upload_post_service.is_configured():
        raise ValueError(
            "Upload-Post is not configured: set upload_post_enabled, upload_post_api_key "
            "and upload_post_username in [app]"
        )
    return upload_post.cross_post_video
