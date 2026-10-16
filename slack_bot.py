"""Slack bot that turns Slack requests into YouTube Shorts via the MPT API.

Run the API first (`python main.py`), then:

    uv sync --extra slack
    SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... uv run python slack_bot.py

The bot uses Socket Mode, so it needs no public URL. See docs/slack-mila.md.

Publishing is deliberately left to a human: the bot posts the finished video,
draft YouTube metadata and suggested posting slots, and a person approves it.
Agents such as Mila never receive account credentials.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from app.config import config
from app.services.slack_shorts import MptClient
from app.services.slack_shorts_bot import ShortsBot, load_settings


def main() -> int:
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        logger.error("slack-bolt is not installed. Run: uv sync --extra slack")
        return 1

    try:
        settings = load_settings(config.slack, os.environ, config.app.get("api_key", ""))
    except ValueError as exc:
        logger.error(f"invalid Slack configuration: {exc}")
        return 1
    if not settings.bot_token or not settings.app_token:
        logger.error("SLACK_BOT_TOKEN and SLACK_APP_TOKEN (or [slack] bot_token/app_token) are required")
        return 1

    mpt = MptClient(settings.mpt_api_url, settings.mpt_api_key)
    if not mpt.ping():
        # Not fatal: the API may start after the bot (e.g. docker compose).
        logger.warning(f"MPT API is not reachable at {settings.mpt_api_url}; jobs will fail until it is")
    if not settings.allowed_channels:
        logger.warning("[slack].allowed_channels is empty; the bot accepts requests in every channel it joins")

    executor = ThreadPoolExecutor(
        max_workers=settings.max_parallel_jobs, thread_name_prefix="slack-short"
    )
    app = App(token=settings.bot_token)
    ShortsBot(settings, mpt, executor.submit).register(app)

    logger.info("Echo Shorts Slack bot starting (Socket Mode)")
    try:
        SocketModeHandler(app, settings.app_token).start()
    finally:
        # Let running renders finish posting their result before exiting.
        executor.shutdown(wait=True, cancel_futures=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
