import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.models.schema import TaskVideoRequest
from app.services import slack_shorts as ss


class ParseRequestTests(unittest.TestCase):
    def test_slash_command_with_inline_options(self):
        request = ss.parse_request("dil:en ses:erkek Calm releases adet:2")
        self.assertEqual(request.subject, "Calm releases")
        self.assertEqual(request.language, "en")
        self.assertEqual(request.gender, "male")
        self.assertEqual(request.count, 2)
        self.assertEqual(request.script, "")

    def test_agent_message_with_header_lines_and_script(self):
        text = (
            "<@U123> short: Sakin yazılım planlaması\n"
            "dil: tr\nformat: dikey\n---\nİlk cümle.\nİkinci cümle."
        )
        request = ss.parse_request(text)
        self.assertEqual(request.subject, "Sakin yazılım planlaması")
        self.assertEqual(request.aspect, "9:16")
        self.assertEqual(request.script, "İlk cümle.\nİkinci cümle.")

    def test_script_only_uses_first_line_as_subject(self):
        request = ss.parse_request("short:\n---\nBaşlık satırı\nDevamı")
        self.assertEqual(request.subject, "Başlık satırı")

    def test_invalid_options_fall_back_with_warnings(self):
        request = ss.parse_request("Konu dil:fr adet:çok format:dairesel ses:robot")
        self.assertEqual((request.language, request.count, request.aspect, request.gender),
                         ("tr", 1, "9:16", "female"))
        self.assertEqual(len(request.warnings), 4)

    def test_count_is_clamped_to_api_limit(self):
        self.assertEqual(ss.parse_request("Konu adet:50").count, 5)

    def test_empty_request_is_rejected(self):
        with self.assertRaises(ss.ShortRequestError):
            ss.parse_request("short:  dil:en")


class PayloadTests(unittest.TestCase):
    def test_generated_script_payload_is_valid_api_request(self):
        payload = ss.build_video_payload(ss.parse_request("Sakin planlama ses:erkek"))
        self.assertEqual(payload["voice_name"], "tr-TR-AhmetNeural-Male")
        self.assertEqual(payload["video_script_prompt"], ss.ECHO_SCRIPT_PROMPT)
        self.assertNotIn("video_source", payload)
        TaskVideoRequest(**payload)

    def test_given_script_skips_brand_prompt_and_honors_source(self):
        request = ss.parse_request("Konu kaynak:pixabay\n---\nMetin")
        payload = ss.build_video_payload(request, {"font_name": "Charm-Bold.ttf"})
        self.assertNotIn("video_script_prompt", payload)
        self.assertEqual(payload["video_source"], "pixabay")
        self.assertEqual(payload["font_name"], "Charm-Bold.ttf")
        TaskVideoRequest(**payload)

    def test_preset_font_exists(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        font = ss.build_video_payload(ss.ShortRequest(subject="x"))["font_name"]
        self.assertTrue(os.path.exists(os.path.join(root, "resource", "fonts", font)))


class PostingSlotTests(unittest.TestCase):
    def test_slots_skip_past_and_too_soon_times(self):
        # 2026-10-16 18:45 in Istanbul (UTC+3); 19:00 is within the 30 min lead.
        now = datetime(2026, 10, 16, 15, 45, tzinfo=timezone.utc)
        slots = ss.next_posting_slots(now)
        self.assertEqual([ss.format_slot(s) for s in slots],
                         ["Cum 16.10 21:00", "Cmt 17.10 12:30", "Cmt 17.10 19:00"])

    def test_invalid_slot_raises(self):
        with self.assertRaises(ValueError):
            ss.next_posting_slots(datetime.now(timezone.utc), ["25:00"])


class MetadataTests(unittest.TestCase):
    def test_title_is_truncated_and_tagged(self):
        meta = ss.youtube_metadata(ss.ShortRequest(subject="a" * 120), "Senaryo")
        self.assertTrue(meta["title"].endswith("... #Shorts"))
        self.assertLessEqual(len(meta["title"]), 100)
        self.assertIn("madebyecho.xyz", meta["description"])


def _response(status=200, data=None, chunks=()):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = data if data is not None else {}
    response.iter_content.return_value = list(chunks)
    response.__enter__.return_value = response
    return response


class MptClientTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.headers = {}
        self.client = ss.MptClient("http://api:8080/", "secret", session=self.session)

    def test_api_key_header_and_create(self):
        self.session.post.return_value = _response(data={"data": {"task_id": "t1"}})
        self.assertEqual(self.client.create_video({"video_subject": "x"}), "t1")
        self.assertEqual(self.session.headers["x-api-key"], "secret")
        self.assertEqual(self.session.post.call_args.args[0], "http://api:8080/api/v1/videos")

    def test_http_error_is_reported(self):
        self.session.post.return_value = _response(429, {"message": "queue full"})
        with self.assertRaisesRegex(ss.MptApiError, "queue full"):
            self.client.create_video({})

    def test_wait_reports_progress_then_completes(self):
        self.session.get.side_effect = [
            _response(data={"data": {"state": 4, "progress": 10}}),
            _response(data={"data": {"state": 4, "progress": 10}}),
            _response(data={"data": {"state": 4, "progress": 50}}),
            _response(data={"data": {"state": 1, "videos": ["v"]}}),
        ]
        seen = []
        task = self.client.wait_for_task(
            "t1", on_progress=lambda t: seen.append(t["progress"]), sleep=lambda _: None
        )
        self.assertEqual(task["videos"], ["v"])
        self.assertEqual(seen, [10, 50])

    def test_wait_raises_on_failure_and_timeout(self):
        self.session.get.return_value = _response(
            data={"data": {"state": -1, "failed_stage": "tts", "error": "boom"}}
        )
        with self.assertRaisesRegex(ss.MptApiError, "tts"):
            self.client.wait_for_task("t1", sleep=lambda _: None)

        self.session.get.return_value = _response(data={"data": {"state": 4}})
        ticks = iter([0, 0, 100])
        with self.assertRaisesRegex(ss.MptApiError, "Zaman aşımı"):
            self.client.wait_for_task("t1", timeout=10, sleep=lambda _: None,
                                      clock=lambda: next(ticks))

    def test_download_rebases_public_endpoint_onto_api_url(self):
        self.session.get.return_value = _response(chunks=[b"mp4"])
        with tempfile.TemporaryDirectory() as tmp:
            path = self.client.download(
                "https://public.example/tasks/t1/final-1.mp4", os.path.join(tmp, "a.mp4")
            )
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"mp4")
        self.assertEqual(self.session.get.call_args.args[0], "http://api:8080/tasks/t1/final-1.mp4")

    def test_download_rejects_paths_outside_tasks(self):
        for uri in ("https://evil.example/api/v1/x", "/tasks/../config.toml"):
            with self.assertRaises(ss.MptApiError):
                self.client.download(uri, "unused")
        self.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
