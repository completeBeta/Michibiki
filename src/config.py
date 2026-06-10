"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    suwayomi_url: str
    anilist_token: str
    poll_interval_seconds: int = 43200
    dry_run: bool = False
    backup_dir: str = "/app/backups"
    populate_suwayomi: bool = True
    clear_suwayomi_first: bool = False


def load_config() -> Config:
    token = os.getenv("ANILIST_TOKEN")
    if not token:
        raise ValueError("ANILIST_TOKEN environment variable is required")

    suwayomi_url = os.getenv("SUWAYOMI_URL")
    if not suwayomi_url:
        raise ValueError("SUWAYOMI_URL environment variable is required")

    return Config(
        suwayomi_url=suwayomi_url,
        anilist_token=token,
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "43200")),
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        backup_dir=os.getenv("BACKUP_DIR", "/app/backups"),
        populate_suwayomi=os.getenv("POPULATE_SUWAYOMI", "true").lower() == "true",
        clear_suwayomi_first=os.getenv("CLEAR_SUWAYOMI_FIRST", "false").lower() == "true",
    )
