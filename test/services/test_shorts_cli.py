import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.services import shorts_cli
from app.services import slack_shorts as ss

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)


def fake_mpt(task):
    mpt = MagicMock()
    mpt.create_video.return_value = "task-1"
    mpt.wait_for_task.side_effect = lambda task_id, on_progress, **kw: (on_progress({"progress": 40}), task)[1]

    def download(uri, path):
        with open(path, "wb") as f:
            f.write(b"mp4")
        return path

    mpt.download.side_effect = download
    return mpt


def run(argv, mpt=None, section=None, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdin", io.StringIO(stdin or "")):
        code = shorts_cli.main(
            argv,
            section=section or {},
            environ={},
            mpt_factory=(lambda settings: mpt) if mpt else None,
            now=lambda: NOW,
            stdout=out,
            stderr=err,
        )
    return code, json.loads(out.getvalue()), err.getvalue()


class ShortsCliTests(unittest.TestCase):
    def test_renders_and_prints_publish_ready_summary(self):
        mpt = fake_mpt({"state": 1, "videos": ["/tasks/task-1/final-1.mp4", "/tasks/task-1/final-2.mp4"],
                        "script": "Sakin bir senaryo."})
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "nested")
            code, result, err = run(["--out", out_dir, "short: Sakin planlama dil:en adet:2"], mpt)
            self.assertEqual(code, 0)
            self.assertEqual(len(result["videos"]), 2)
            for path in result["videos"]:
                self.assertTrue(os.path.isabs(path) and os.path.exists(path))
        payload = mpt.create_video.call_args.args[0]
        self.assertEqual((payload["video_language"], payload["video_count"]), ("en", 2))
        self.assertEqual(result["title"], "Sakin planlama #Shorts")
        self.assertEqual(result["script"], "Sakin bir senaryo.")
        self.assertEqual(result["posting_slots"][0]["label"], "Cum 16.10 12:30")
        self.assertIn("40%", err)

    def test_reads_multiline_request_from_stdin_and_applies_preset(self):
        mpt = fake_mpt({"state": 1, "videos": ["/tasks/t/final-1.mp4"]})
        with tempfile.TemporaryDirectory() as tmp:
            code, result, _ = run(
                ["--out", tmp], mpt, section={"font_name": "Charm-Bold.ttf"},
                stdin="short: Konu\nses: erkek\n---\nHazır metin.",
            )
        self.assertEqual(code, 0)
        payload = mpt.create_video.call_args.args[0]
        self.assertEqual(payload["video_script"], "Hazır metin.")
        self.assertEqual(payload["font_name"], "Charm-Bold.ttf")
        self.assertEqual(result["script"], "Hazır metin.")

    def test_invalid_request_and_settings_exit_2_without_rendering(self):
        mpt = fake_mpt({})
        code, result, _ = run(["dil:en"], mpt)
        self.assertEqual(code, 2)
        self.assertIn("Konu", result["error"])
        code, result, _ = run(["Konu"], mpt, section={"timezone": "Mars/Base"})
        self.assertEqual(code, 2)
        self.assertIn("timezone", result["error"])
        mpt.create_video.assert_not_called()

    def test_render_failures_exit_1(self):
        mpt = fake_mpt({"state": 1, "videos": []})
        with tempfile.TemporaryDirectory() as tmp:
            code, result, _ = run(["--out", tmp, "Konu"], mpt)
        self.assertEqual(code, 1)
        self.assertIn("video dosyası", result["error"])

        mpt.create_video.side_effect = ss.requests.ConnectionError()
        code, result, _ = run(["Konu"], mpt)
        self.assertEqual(code, 1)
        self.assertIn("ulaşılamadı", result["error"])

    def test_default_client_uses_settings(self):
        with patch.object(ss, "MptClient") as client_cls:
            client_cls.return_value.create_video.side_effect = ss.MptApiError("down")
            code, result, _ = run(["Konu"], section={"mpt_api_url": "http://api:8080", "mpt_api_key": "k"})
        client_cls.assert_called_once_with("http://api:8080", "k")
        self.assertEqual((code, result["error"]), (1, "down"))


if __name__ == "__main__":
    unittest.main()
