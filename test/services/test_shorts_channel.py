"""Facts-channel profile, approval queue, publishing and compilation."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.schema import TaskVideoRequest
from app.services import shorts_cli, shorts_compile, shorts_queue
from app.services import slack_shorts as ss
from app.services.slack_shorts_bot import load_settings

# Friday 2026-10-16 08:00 New York (12:00 UTC), before the 12:30 slot.
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
VOICE = "elevenlabs:abc123:Deep Narrator"
FACTS = {"profile": "facts", "elevenlabs_voice": VOICE}


class FactsProfileTests(unittest.TestCase):
    def test_settings_take_language_timezone_and_slots_from_profile(self):
        settings = load_settings(FACTS, {})
        self.assertEqual((settings.default_language, settings.default_gender), ("en", "male"))
        self.assertEqual((settings.timezone, settings.posting_slots), ("America/New_York", ("12:30",)))
        self.assertEqual(settings.preset, {"elevenlabs_voice": VOICE, "profile": "facts"})

    def test_payload_uses_karaoke_subtitles_and_elevenlabs_voice(self):
        settings = load_settings(FACTS, {})
        payload = ss.build_video_payload(settings.parse("Your brain glows"), dict(settings.preset))
        self.assertEqual(payload["voice_name"], VOICE)
        self.assertEqual(payload["subtitle_display_mode"], "word_by_word")
        self.assertEqual(payload["subtitle_animation"], "pop_spring")
        self.assertEqual(payload["video_clip_duration"], 2)
        self.assertEqual(payload["video_script_prompt"], ss.FACTS_SCRIPT_PROMPT)
        # Footage must be findable on Pixabay: concrete scenes, no proper nouns.
        self.assertEqual(payload["video_terms_prompt"], ss.FACTS_TERMS_PROMPT)
        self.assertIn("no proper nouns", payload["video_terms_prompt"])
        # Lower third, above the Shorts/TikTok UI overlay, never mid-frame.
        self.assertEqual(payload["subtitle_position"], "custom")
        self.assertTrue(65 <= payload["custom_position"] <= 80)
        TaskVideoRequest(**payload)

    def test_facts_profile_uses_free_deep_narrator_without_elevenlabs(self):
        settings = load_settings({"profile": "facts"}, {})
        payload = ss.build_video_payload(settings.parse("Your brain glows"), dict(settings.preset))
        self.assertEqual(payload["voice_name"], "en-US-AndrewMultilingualNeural-Male")
        female = ss.build_video_payload(settings.parse("Fact ses:kadın"), dict(settings.preset))
        self.assertEqual(female["voice_name"], "en-US-AvaNeural-Female")
        self.assertEqual(shorts_cli.subtitle_sync_warning(settings, "edge"), "")

    def test_elevenlabs_voice_only_replaces_profile_language(self):
        settings = load_settings(FACTS, {})
        payload = ss.build_video_payload(settings.parse("Konu dil:tr"), dict(settings.preset))
        self.assertEqual(payload["voice_name"], "tr-TR-AhmetNeural-Male")

    def test_references_go_to_description_not_script(self):
        request = ss.parse_request(
            "Dancing plague ref:https://en.wikipedia.org/wiki/Dancing_plague_of_1518 ref:notaurl",
            "en",
        )
        self.assertEqual(request.subject, "Dancing plague")
        self.assertEqual(request.references, ["https://en.wikipedia.org/wiki/Dancing_plague_of_1518"])
        self.assertEqual(len(request.warnings), 1)
        meta = ss.youtube_metadata(request, "In 1518...", "facts")
        self.assertIn("Sources:\nhttps://en.wikipedia.org/wiki/Dancing_plague_of_1518", meta["description"])
        self.assertNotIn("madebyecho.xyz", meta["description"])
        self.assertEqual(meta["tags"][:3], ["shorts", "facts", "mystery"])

    def test_reference_limit(self):
        text = "Topic " + " ".join(f"ref:https://e.com/{i}" for i in range(7))
        request = ss.parse_request(text)
        self.assertEqual(len(request.references), ss.MAX_REFERENCES)

    def test_invalid_profile_voice_and_platform_settings(self):
        for section, message in (
            ({"profile": "cats"}, "profile"),
            ({"elevenlabs_voice": "Deep Narrator"}, "elevenlabs_voice"),
            ({"publish_platforms": ["youtube", "myspace"]}, "publish_platforms"),
        ):
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, message):
                load_settings(section, {})

    def test_video_sources_rotation_setting(self):
        settings = load_settings({"video_sources": ["Pexels", "pixabay"]}, {})
        self.assertEqual(settings.preset["video_sources"], "pexels,pixabay")

    def test_single_render_uses_configured_source_not_api_default(self):
        settings = load_settings({"profile": "facts", "video_sources": ["pixabay"]}, {})
        payload = ss.build_video_payload(settings.parse("Fact"), dict(settings.preset))
        self.assertEqual(payload["video_source"], "pixabay")
        explicit = ss.build_video_payload(settings.parse("Fact kaynak:coverr"), dict(settings.preset))
        self.assertEqual(explicit["video_source"], "coverr")
        with_source = {**settings.preset, "video_source": "pexels"}
        self.assertEqual(ss.build_video_payload(settings.parse("Fact"), with_source)["video_source"], "pexels")


def rendered(subject="Fact", video="/tmp/v.mp4"):
    return {"videos": [video], "title": f"{subject} #Shorts", "description": "d", "tags": ["shorts"],
            "subject": subject, "task_id": "t"}


class QueueTests(unittest.TestCase):
    def test_free_slots_skip_taken_days_and_rejected_items_release_them(self):
        items = []
        first = shorts_queue.free_slots(items, NOW, 2, ("12:30",), "America/New_York")
        self.assertEqual([s.isoformat() for s in first],
                         ["2026-10-16T12:30:00-04:00", "2026-10-17T12:30:00-04:00"])
        shorts_queue.add(items, rendered(), first[0])
        rejected = shorts_queue.add(items, rendered(), first[1])
        rejected["status"] = shorts_queue.REJECTED
        nxt = shorts_queue.free_slots(items, NOW, 2, ("12:30",), "America/New_York")
        self.assertEqual([s.day for s in nxt], [17, 18])

    def test_set_status_validates_ids_and_transitions(self):
        items = []
        item = shorts_queue.add(items, rendered(), NOW)
        with self.assertRaisesRegex(shorts_queue.QueueError, "unknown"):
            shorts_queue.set_status(items, ["nope"], shorts_queue.APPROVED)
        shorts_queue.set_status(items, [item["id"]], shorts_queue.APPROVED)
        with self.assertRaisesRegex(shorts_queue.QueueError, "approved"):
            shorts_queue.set_status(items, [item["id"]], shorts_queue.REJECTED)

    def test_save_load_roundtrip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "queue.json")
            self.assertEqual(shorts_queue.load(path), [])
            items = []
            shorts_queue.add(items, rendered(), NOW)
            shorts_queue.save(path, items)
            self.assertEqual(shorts_queue.load(path), items)
            with open(path, "w") as f:
                f.write("[]")
            with self.assertRaises(shorts_queue.QueueError):
                shorts_queue.load(path)

    def test_publish_due_records_each_outcome_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            videos = []
            for name in "abc":
                path = os.path.join(tmp, f"{name}.mp4")
                open(path, "wb").close()
                videos.append(path)
            items = []
            past = NOW - timedelta(hours=1)
            ok, unsure, bad = (shorts_queue.add(items, rendered(v, v), past) for v in videos)
            missing = shorts_queue.add(items, rendered("m", os.path.join(tmp, "gone.mp4")), past)
            future = shorts_queue.add(items, rendered("f", videos[0]), NOW + timedelta(days=1))
            pending = shorts_queue.add(items, rendered("p", videos[0]), past)
            for item in (ok, unsure, bad, missing, future):
                item["status"] = shorts_queue.APPROVED
            outcomes = {
                videos[0]: {"success": True, "request_id": "r1"},
                videos[1]: {"success": False, "error": "timeout; upload outcome is unconfirmed"},
                videos[2]: {"success": False, "error": "invalid token"},
            }
            upload = MagicMock(side_effect=lambda path, title, platforms, youtube_extra: outcomes[path])
            handled = shorts_queue.publish_due(items, NOW, upload, ["youtube", "tiktok"])

        self.assertEqual(len(handled), 4)
        self.assertEqual([ok["status"], unsure["status"], bad["status"], missing["status"]],
                         ["published", "needs_check", "failed", "failed"])
        self.assertEqual((future["status"], pending["status"]), ("approved", "pending"))
        self.assertEqual(upload.call_count, 3)
        args = upload.call_args_list[0]
        self.assertEqual(args.args[2], ["youtube", "tiktok"])
        self.assertEqual(args.kwargs["youtube_extra"]["tags"], ["shorts"])
        self.assertEqual(shorts_queue.publish_due(items, NOW, upload, ["youtube"]), [])


def fake_mpt(fail_subjects=()):
    mpt = MagicMock()
    mpt.create_video.side_effect = lambda payload: (
        (_ for _ in ()).throw(ss.MptApiError("render failed"))
        if payload["video_subject"] in fail_subjects
        else f"task-{payload['video_subject'].split()[0]}"
    )
    mpt.wait_for_task.side_effect = lambda task_id, **kw: {"state": 1, "videos": [f"/tasks/{task_id}/final-1.mp4"]}

    def download(uri, path):
        open(path, "wb").close()
        return path

    mpt.download.side_effect = download
    return mpt


class ChannelCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = os.path.join(self.tmp.name, "queue.json")
        self.section = {**FACTS, "video_sources": ["pexels", "pixabay"], "publish_platforms": ["youtube", "tiktok"]}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, **kwargs):
        out = io.StringIO()
        code = shorts_cli.main(["--queue", self.queue, *argv], section=self.section, environ={},
                               now=lambda: NOW, stdout=out, stderr=io.StringIO(), **kwargs)
        return code, json.loads(out.getvalue())

    def write_plan(self, entries):
        path = os.path.join(self.tmp.name, "plan.json")
        with open(path, "w") as f:
            json.dump(entries, f)
        return path

    def test_weekly_batch_approve_publish_cycle(self):
        plan = self.write_plan([
            {"text": "Brain fact ref:https://example.org/brain", "category": "psychology"},
            "Space fact",
            {"text": "History fact", "category": "history"},
        ])
        mpt = fake_mpt(fail_subjects={"Space fact"})
        code, result = self.run_cli(["batch", "--plan", plan, "--out", self.tmp.name],
                                    mpt_factory=lambda s: mpt)
        self.assertEqual(code, 0)
        self.assertEqual(len(result["queued"]), 2)
        self.assertEqual(result["failed"], [{"text": "Space fact", "error": "render failed"}])
        sources = [c.args[0]["video_source"] for c in mpt.create_video.call_args_list]
        self.assertEqual(sources, ["pexels", "pixabay", "pexels"])
        self.assertIn("https://example.org/brain", result["queued"][0]["description"])
        self.assertEqual([i["publish_at"][:10] for i in result["queued"]], ["2026-10-16", "2026-10-17"])

        code, listed = self.run_cli(["queue", "--status", "pending"])
        self.assertEqual(len(listed), 2)
        first, second = (i["id"] for i in listed)

        self.assertEqual(self.run_cli(["reject", second])[0], 0)
        code, approved = self.run_cli(["approve", "--all-pending"])
        self.assertEqual([i["id"] for i in approved], [first])

        code, due = self.run_cli(["publish-due", "--dry-run"])
        self.assertEqual(due, [])  # 12:30 slot has not passed at 08:00

        upload = MagicMock(return_value={"success": True})
        later = NOW + timedelta(hours=5)  # 17:00 UTC = 13:00 New York, slot passed
        out = io.StringIO()
        code = shorts_cli.main(["--queue", self.queue, "publish-due"], section=self.section, environ={},
                               uploader=upload, now=lambda: later, stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())[0]["status"], "published")
        self.assertEqual(upload.call_args.args[2], ["youtube", "tiktok"])

    def test_batch_with_only_failures_and_bad_plans(self):
        plan = self.write_plan(["Only fact"])
        code, result = self.run_cli(["batch", "--plan", plan], mpt_factory=lambda s: fake_mpt({"Only fact"}))
        self.assertEqual((code, result["queued"]), (1, []))
        for content in ("[]", '[{"category": "x"}]', "not json"):
            path = os.path.join(self.tmp.name, "bad.json")
            with open(path, "w") as f:
                f.write(content)
            with self.subTest(content=content):
                self.assertEqual(self.run_cli(["batch", "--plan", path])[0], 2)

    def test_approve_errors_and_failed_publish_exit_code(self):
        self.assertEqual(self.run_cli(["approve"])[0], 2)
        self.assertEqual(self.run_cli(["approve", "missing"])[0], 2)
        items = []
        video = os.path.join(self.tmp.name, "v.mp4")
        open(video, "wb").close()
        shorts_queue.add(items, rendered("x", video), NOW - timedelta(hours=1))["status"] = "approved"
        shorts_queue.save(self.queue, items)
        code, handled = self.run_cli(["publish-due"], uploader=MagicMock(return_value={"success": False, "error": "x"}))
        self.assertEqual((code, handled[0]["status"]), (1, "failed"))

    def test_publish_requires_configured_upload_post(self):
        with patch("app.services.upload_post.upload_post_service") as service:
            service.is_configured.return_value = False
            code, result = self.run_cli(["publish-due"])
        self.assertEqual(code, 2)
        self.assertIn("Upload-Post is not configured", result["error"])

    def test_compile_uses_last_published_videos(self):
        items = []
        for day in range(3):
            item = shorts_queue.add(items, rendered(f"s{day}", f"/v/{day}.mp4"), NOW + timedelta(days=day))
            item["status"] = "published"
        shorts_queue.save(self.queue, items)
        compiler = MagicMock(return_value="/out/long.mp4")
        code, result = self.run_cli(["compile", "--last", "2", "--out", "long.mp4"], compiler=compiler)
        self.assertEqual(code, 0)
        compiler.assert_called_once_with(["/v/1.mp4", "/v/2.mp4"], "long.mp4")
        self.assertEqual(result["output"], "/out/long.mp4")
        compiler.side_effect = shorts_compile.CompileError("ffmpeg failed")
        code, result = self.run_cli(["compile", "a.mp4", "b.mp4", "--out", "x.mp4"], compiler=compiler)
        self.assertEqual((code, result["error"]), (1, "ffmpeg failed"))

    def test_render_shorthand_still_works_with_queue_option(self):
        code, result = self.run_cli(["Lone fact", "--out", self.tmp.name], mpt_factory=lambda s: fake_mpt())
        self.assertEqual(code, 0)
        self.assertEqual(result["subject"], "Lone fact")
        self.assertEqual(result["posting_slots"][0]["label"], "Cum 16.10 12:30")


class ExamplePlanTests(unittest.TestCase):
    def test_documented_plan_parses_with_sources(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        plan = shorts_cli._read_plan(os.path.join(root, "docs", "shorts", "plan.example.json"))
        settings = load_settings(FACTS, {})
        for entry in plan:
            request = settings.parse(entry["text"])
            self.assertTrue(request.subject and not request.warnings, entry)
            self.assertEqual(len(request.references), 1)
            self.assertNotIn("ref:", request.subject)


class SubtitleSyncTests(unittest.TestCase):
    def test_warns_only_for_elevenlabs_word_subtitles_without_whisper(self):
        facts = load_settings(FACTS, {})
        self.assertEqual(shorts_cli.subtitle_sync_warning(facts, "edge"), shorts_cli.SUBTITLE_SYNC_WARNING)
        self.assertEqual(shorts_cli.subtitle_sync_warning(facts, "Whisper "), "")
        self.assertEqual(shorts_cli.subtitle_sync_warning(load_settings({"profile": "facts"}, {}), "edge"), "")
        self.assertEqual(shorts_cli.subtitle_sync_warning(load_settings({"elevenlabs_voice": VOICE}, {}), "edge"), "")

    def test_render_result_carries_the_warning(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            code = shorts_cli.main(["Fact", "--out", tmp], section=FACTS, environ={}, now=lambda: NOW,
                                   mpt_factory=lambda s: fake_mpt(), stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertIn(shorts_cli.SUBTITLE_SYNC_WARNING, json.loads(out.getvalue())["warnings"])
        self.assertIn("warning:", err.getvalue())


class CompileTests(unittest.TestCase):
    def test_command_layout_and_validation(self):
        command = shorts_compile.build_command(["a.mp4", "b.mp4"], "out.mp4")
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=1:a=1", graph)
        self.assertIn("boxblur", graph)
        self.assertEqual(command[-1], "out.mp4")
        with self.assertRaises(shorts_compile.CompileError):
            shorts_compile.build_command(["a.mp4"], "out.mp4")

    def test_missing_clips_and_ffmpeg_failure_leave_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(shorts_compile.CompileError, "missing"):
                shorts_compile.compile_shorts([os.path.join(tmp, "x.mp4")] * 2, os.path.join(tmp, "o.mp4"))
            clips = []
            for name in "ab":
                clips.append(os.path.join(tmp, f"{name}.mp4"))
                open(clips[-1], "wb").close()
            failing = MagicMock(return_value=subprocess.CompletedProcess([], 1, "", "boom"))
            with patch.object(shorts_compile.shutil, "which", return_value="/usr/bin/ffmpeg"):
                with self.assertRaisesRegex(shorts_compile.CompileError, "boom"):
                    shorts_compile.compile_shorts(clips, os.path.join(tmp, "o.mp4"), run=failing)
            self.assertFalse(os.path.exists(os.path.join(tmp, "o.mp4")))
            with patch.object(shorts_compile.shutil, "which", return_value=None):
                with self.assertRaisesRegex(shorts_compile.CompileError, "not installed"):
                    shorts_compile.compile_shorts(clips, os.path.join(tmp, "o.mp4"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_real_compilation_is_1080p_with_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            clips = []
            for index in range(2):
                path = os.path.join(tmp, f"c{index}.mp4")
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=1",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                     "-shortest", "-c:v", "libx264", "-c:a", "aac", path],
                    check=True,
                )
                clips.append(path)
            output = shorts_compile.compile_shorts(clips, os.path.join(tmp, "long.mp4"))
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                 "-of", "json", output],
                capture_output=True, text=True, check=True,
            )
        streams = json.loads(probe.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1920, 1080))
        self.assertTrue(any(s["codec_type"] == "audio" for s in streams))


if __name__ == "__main__":
    unittest.main()
