import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.services import slack_shorts as ss
from app.services.slack_shorts_bot import ShortsBot, SlackSettings, load_settings


class FakeSlack:
    def __init__(self):
        self.calls = []
        self.fail_on = set()

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if name in self.fail_on:
            raise RuntimeError(f"{name} failed")
        return {"ok": True, "ts": f"{len(self.calls)}.0"}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **kwargs: self._record(name, kwargs)

    def named(self, name):
        return [kwargs for call, kwargs in self.calls if call == name]


class FakeApp:
    def __init__(self):
        self.handlers = {}

    def _decorator(self, kind, key):
        def register(func):
            self.handlers[(kind, key if isinstance(key, str) else "pattern")] = func
            return func

        return register

    def command(self, name):
        return self._decorator("command", name)

    def event(self, name):
        return self._decorator("event", name)

    def message(self, pattern):
        return self._decorator("message", pattern)

    def action(self, name):
        return self._decorator("action", name)


def make_bot(settings=None, mpt=None):
    if mpt is None:
        mpt = MagicMock()
        mpt.create_video.return_value = "task-1"
        mpt.wait_for_task.return_value = {
            "state": 1,
            "videos": ["/tasks/task-1/final-1.mp4"],
            "script": "Sakin bir senaryo.",
        }
        mpt.download.side_effect = lambda uri, path: path
    submitted = []
    bot = ShortsBot(
        settings or SlackSettings(allowed_bot_ids=frozenset({"BMILA"})),
        mpt,
        submit=lambda fn, *args: submitted.append((fn, args)),
        now=lambda: datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc),
    )
    return bot, mpt, submitted


def run_all(submitted):
    while submitted:
        fn, args = submitted.pop(0)
        fn(*args)


def review_body(job_id, user="UBOSS", action="short_approve"):
    return {
        "user": {"id": user},
        "channel": {"id": "C1"},
        "actions": [{"action_id": action, "value": job_id}],
        "message": {
            "ts": "9.0",
            "thread_ts": "1.0",
            "text": "İnceleme",
            "blocks": [{"type": "section"}, {"type": "actions"}],
        },
    }


class LoadSettingsTests(unittest.TestCase):
    def test_environment_overrides_config_and_lists_are_parsed(self):
        settings = load_settings(
            {"bot_token": "from-config", "allowed_channels": ["C1"], "font_name": "Charm-Bold.ttf"},
            {"SLACK_BOT_TOKEN": "from-env", "SLACK_ALLOWED_BOT_IDS": "B1, B2"},
            fallback_api_key="api-secret",
        )
        self.assertEqual(settings.bot_token, "from-env")
        self.assertEqual(settings.allowed_channels, frozenset({"C1"}))
        self.assertEqual(settings.allowed_bot_ids, frozenset({"B1", "B2"}))
        self.assertEqual(settings.mpt_api_key, "api-secret")
        self.assertEqual(settings.preset, {"font_name": "Charm-Bold.ttf", "profile": "echo"})

    def test_invalid_values_are_rejected_with_key_name(self):
        cases = [
            ({"default_language": "fr"}, "default_language"),
            ({"timezone": "Mars/Base"}, "timezone"),
            ({"posting_slots": ["24:00"]}, "posting slot"),
            ({"max_parallel_jobs": 0}, "max_parallel_jobs"),
            ({"max_parallel_jobs": "many"}, "max_parallel_jobs"),
        ]
        for section, message in cases:
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, message):
                load_settings(section, {})


class TriggerTests(unittest.TestCase):
    def test_slash_command_posts_anchor_and_runs_job(self):
        bot, mpt, submitted = make_bot()
        slack, respond = FakeSlack(), MagicMock()
        bot.on_command({"channel_id": "C1", "user_id": "U1", "text": "Sakin planlama"}, slack, respond)
        run_all(submitted)

        respond.assert_not_called()
        self.assertEqual(mpt.create_video.call_args.args[0]["video_subject"], "Sakin planlama")
        upload = slack.named("files_upload_v2")[0]
        self.assertEqual(upload["thread_ts"], "1.0")
        self.assertEqual(upload["file_uploads"][0]["title"], "Sakin planlama (1/1)")
        review = slack.named("chat_postMessage")[-1]
        self.assertIn("<@U1>", review["blocks"][0]["text"]["text"])
        self.assertIn("Cum 16.10 12:30", review["blocks"][0]["text"]["text"])
        self.assertIn("hazır", slack.named("chat_update")[-1]["text"])

    def test_slash_command_rejects_disallowed_channel_and_bad_request(self):
        bot, mpt, submitted = make_bot(SlackSettings(allowed_channels=frozenset({"C1"})))
        respond = MagicMock()
        bot.on_command({"channel_id": "C2", "user_id": "U1", "text": "Konu"}, FakeSlack(), respond)
        bot.on_command({"channel_id": "C1", "user_id": "U1", "text": "dil:en"}, FakeSlack(), respond)
        self.assertEqual(respond.call_count, 2)
        self.assertIn("kapalı", respond.call_args_list[0].kwargs["text"])
        self.assertEqual(submitted, [])

    def test_slash_command_explains_missing_channel_membership(self):
        bot, _, submitted = make_bot()
        slack, respond = FakeSlack(), MagicMock()
        slack.fail_on.add("chat_postMessage")
        bot.on_command({"channel_id": "C1", "user_id": "U1", "text": "Konu"}, slack, respond)
        self.assertIn("/invite", respond.call_args.kwargs["text"])
        self.assertEqual(submitted, [])

    def test_agent_messages_require_allowlisted_bot(self):
        bot, _, submitted = make_bot()
        bot.on_agent_message({"channel": "C1", "ts": "1.0", "text": "short: Konu"}, FakeSlack())
        bot.on_agent_message({"channel": "C1", "ts": "2.0", "text": "short: Konu", "bot_id": "BX"}, FakeSlack())
        self.assertEqual(submitted, [])
        bot.on_agent_message({"channel": "C1", "ts": "3.0", "text": "short: Konu", "bot_id": "BMILA"}, FakeSlack())
        self.assertEqual(len(submitted), 1)

    def test_message_and_mention_for_same_post_start_one_job(self):
        bot, _, submitted = make_bot()
        event = {"channel": "C1", "ts": "1.0", "text": "<@UBOT> short: Konu", "bot_id": "BMILA"}
        bot.on_agent_message(event, FakeSlack())
        bot.on_mention(event, FakeSlack())
        self.assertEqual(len(submitted), 1)

    def test_mention_from_unknown_bot_is_ignored_and_bad_text_is_explained(self):
        bot, _, submitted = make_bot()
        bot.on_mention({"channel": "C1", "ts": "1.0", "text": "<@UBOT> Konu", "bot_id": "BX"}, FakeSlack())
        slack = FakeSlack()
        bot.on_mention({"channel": "C1", "ts": "2.0", "thread_ts": "0.5", "text": "<@UBOT>", "user": "U1"}, slack)
        self.assertEqual(submitted, [])
        self.assertEqual(slack.named("chat_postMessage")[0]["thread_ts"], "0.5")

    def test_register_wires_every_handler(self):
        bot, _, _ = make_bot()
        app = FakeApp()
        bot.register(app)
        self.assertEqual(
            set(app.handlers),
            {("command", "/short"), ("event", "app_mention"), ("message", "pattern"),
             ("action", "short_approve"), ("action", "short_reject"), ("action", "short_regenerate")},
        )
        ack, respond = MagicMock(), MagicMock()
        app.handlers[("command", "/short")](ack, {"channel_id": "C", "user_id": "U", "text": ""}, FakeSlack(), respond)
        ack.assert_called_once()
        ack = MagicMock()
        app.handlers[("action", "short_reject")](ack, review_body("job"), FakeSlack())
        ack.assert_called_once()


class JobTests(unittest.TestCase):
    def test_failures_are_reported_without_leaking_internal_details(self):
        mpt = MagicMock()
        mpt.create_video.side_effect = RuntimeError("http://10.0.0.5/secret path")
        bot, _, submitted = make_bot(mpt=mpt)
        slack = FakeSlack()
        bot.on_mention({"channel": "C1", "ts": "1.0", "text": "Konu", "user": "U1"}, slack)
        run_all(submitted)
        final = slack.named("chat_update")[-1]["text"]
        self.assertIn("üretilemedi", final)
        self.assertNotIn("10.0.0.5", final)

        mpt.create_video.side_effect = ss.MptApiError("Üretim başarısız (tts)")
        bot.on_mention({"channel": "C1", "ts": "2.0", "text": "Konu", "user": "U1"}, slack)
        run_all(submitted)
        self.assertIn("tts", slack.named("chat_update")[-1]["text"])

    def test_completed_task_without_videos_is_a_failure(self):
        bot, mpt, submitted = make_bot()
        mpt.wait_for_task.return_value = {"state": 1, "videos": []}
        slack = FakeSlack()
        bot.on_mention({"channel": "C1", "ts": "1.0", "text": "Konu", "user": "U1"}, slack)
        run_all(submitted)
        self.assertEqual(slack.named("files_upload_v2"), [])
        self.assertIn("video dosyası", slack.named("chat_update")[-1]["text"])

    def test_status_update_errors_do_not_abort_the_job(self):
        bot, _, submitted = make_bot()
        slack = FakeSlack()
        slack.fail_on.add("chat_update")
        bot.on_mention({"channel": "C1", "ts": "1.0", "text": "Konu", "user": "U1"}, slack)
        run_all(submitted)
        self.assertEqual(len(slack.named("files_upload_v2")), 1)

    def test_progress_updates_are_throttled(self):
        bot, mpt, submitted = make_bot()
        # Start at 0; 10 and 20 arrive within the 10 s window, 30 after it.
        ticks = iter([0, 1, 2, 15, 15])
        bot.clock = lambda: next(ticks)

        def fake_wait(task_id, on_progress, **kwargs):
            for progress in (10, 20, 30):
                on_progress({"progress": progress})
            return {"state": 1, "videos": ["/tasks/t/final-1.mp4"]}

        mpt.wait_for_task.side_effect = fake_wait
        slack = FakeSlack()
        bot.on_mention({"channel": "C1", "ts": "1.0", "text": "Konu", "user": "U1"}, slack)
        run_all(submitted)
        progress_updates = [u["text"] for u in slack.named("chat_update") if "%" in u["text"]]
        self.assertEqual(len(progress_updates), 1)
        self.assertIn("%30", progress_updates[0])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.bot, self.mpt, self.submitted = make_bot(
            SlackSettings(approver_user_ids=frozenset({"UBOSS"}))
        )
        self.bot.on_mention({"channel": "C1", "ts": "1.0", "text": "Konu dil:en", "user": "U1"}, FakeSlack())
        self.job_id = list(self.bot._jobs)[0]
        self.submitted.clear()

    def test_unauthorized_user_cannot_decide(self):
        slack = FakeSlack()
        self.bot.on_approve(review_body(self.job_id, user="UINTERN"), slack)
        self.assertEqual(slack.named("chat_update"), [])
        self.assertIn("yetkiniz yok", slack.named("chat_postEphemeral")[0]["text"])

    def test_approve_removes_buttons_and_records_decision(self):
        slack = FakeSlack()
        self.bot.on_approve(review_body(self.job_id), slack)
        blocks = slack.named("chat_update")[0]["blocks"]
        self.assertNotIn("actions", [b["type"] for b in blocks])
        self.assertIn("<@UBOSS> onayladı", blocks[-1]["elements"][0]["text"])

    def test_regenerate_reuses_request_once(self):
        slack = FakeSlack()
        self.bot.on_regenerate(review_body(self.job_id), slack)
        self.bot.on_regenerate(review_body(self.job_id), slack)
        self.assertEqual(len(self.submitted), 1)
        self.assertIn("zaten verildi", slack.named("chat_postEphemeral")[0]["text"])
        new_job = self.bot._jobs[self.submitted[0][1][1]]
        self.assertEqual(new_job.request.language, "en")
        self.assertEqual(new_job.requester, "UBOSS")

    def test_regenerate_for_forgotten_job_asks_to_resend(self):
        slack = FakeSlack()
        self.bot.on_regenerate(review_body("unknown-job"), slack)
        self.assertEqual(self.submitted, [])
        self.assertIn("tekrar gönderin", slack.named("chat_postMessage")[0]["text"])

    def test_job_memory_is_bounded(self):
        bot = ShortsBot(SlackSettings(), MagicMock(), submit=lambda *a: None, max_remembered_jobs=3)
        for i in range(5):
            bot.on_mention({"channel": "C1", "ts": f"{i}.0", "text": "Konu", "user": "U1"}, FakeSlack())
        self.assertEqual(len(bot._jobs), 3)


if __name__ == "__main__":
    unittest.main()
