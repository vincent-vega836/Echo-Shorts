"""Render an ECHO YouTube Short for an agent such as Mila; see app/services/shorts_cli.py."""

import sys

from app.config import config
from app.services.shorts_cli import main

if __name__ == "__main__":
    sys.exit(main(section=config.slack, fallback_api_key=config.app.get("api_key", "")))
