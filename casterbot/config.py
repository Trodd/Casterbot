"""Load configuration from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _get(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


def _int(key: str, default: int = 0) -> int:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


# Discord
DISCORD_TOKEN: str = _get("DISCORD_TOKEN", required=True)
GUILD_ID: int = _int("GUILD_ID")

# League branding
LEAGUE_NAME: str = _get("LEAGUE_NAME", "Echo Master League")

CLAIM_CHANNEL_ID: int = _int("CLAIM_CHANNEL_ID")
PRIVATE_CATEGORY_ID: int = _int("PRIVATE_CATEGORY_ID")

REQUIRE_CLAIM_ROLE: bool = _bool("REQUIRE_CLAIM_ROLE", False)
CLAIM_ELIGIBLE_ROLE_ID: int = _int("CLAIM_ELIGIBLE_ROLE_ID")

CASTER_ROLE_ID: int = _int("CASTER_ROLE_ID")
CAMOP_ROLE_ID: int = _int("CAMOP_ROLE_ID")
CASTER_TRAINING_ROLE_ID: int = _int("CASTER_TRAINING_ROLE_ID")
CAMOP_TRAINING_ROLE_ID: int = _int("CAMOP_TRAINING_ROLE_ID")
STAFF_ROLE_ID: int = _int("STAFF_ROLE_ID")

# Live announcement
LIVE_ANNOUNCEMENT_CHANNEL_ID: int = _int("LIVE_ANNOUNCEMENT_CHANNEL_ID")
LIVE_PING_ROLE_ID: int = _int("LIVE_PING_ROLE_ID")
TWITCH_URL: str = _get("TWITCH_URL", "https://www.twitch.tv/echomasterleague")
TWITCH_URL_2: str = _get("TWITCH_URL_2", "https://www.twitch.tv/echomasterleague_2")

# Stream channel options: maps channel number to (label, URL)
STREAM_CHANNELS: dict = {
    1: ("Channel 1", TWITCH_URL),
    2: ("Channel 2", TWITCH_URL_2),
}

# Transcript channel (optional - logs deleted private channels)
TRANSCRIPT_CHANNEL_ID: int = _int("TRANSCRIPT_CHANNEL_ID")

# Data sources
UPCOMING_MATCHES_CSV_URL: str = _get(
    "UPCOMING_MATCHES_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTIvdXGBkVZB5ZFdMVUCZqe8e8DbOj6UbSAeqBP0uzYAY5Z1q37c-ZVG7iV96_cOlX-0jsgNLYXfe6B/pub?gid=881384435&single=true&output=csv",
)
ROSTERS_CSV_URL: str = _get("ROSTERS_CSV_URL", "")

# Behavior
MATCH_LOOKAHEAD_DAYS: int = _int("MATCH_LOOKAHEAD_DAYS", 14)
MATCH_GRACE_HOURS: int = _int("MATCH_GRACE_HOURS", 4)  # Keep matches for X hours after start time
SYNC_INTERVAL_SECONDS: int = _int("SYNC_INTERVAL_SECONDS", 300)
TIMEZONE: str = _get("TIMEZONE", "US/Eastern")

# Web server (optional)
WEB_ENABLED: bool = _bool("WEB_ENABLED", False)
WEB_HOST: str = _get("WEB_HOST", "0.0.0.0")
WEB_PORT: int = _int("WEB_PORT", 8080)
WEB_PUBLIC_URL: str = _get("WEB_PUBLIC_URL", "")  # e.g., "http://yourdomain.com:8080"

# SSL/HTTPS (optional - provide paths to cert and key files)
WEB_SSL_CERT: str = _get("WEB_SSL_CERT", "")  # Path to SSL certificate file (.pem or .crt)
WEB_SSL_KEY: str = _get("WEB_SSL_KEY", "")    # Path to SSL private key file (.pem or .key)

# Discord OAuth2 (for web login - optional)
DISCORD_CLIENT_ID: str = _get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET: str = _get("DISCORD_CLIENT_SECRET", "")

# Web admin role (users with this role can access the admin tab)
WEB_LEAD_ROLE_ID: int = _int("WEB_LEAD_ROLE_ID")

# Database path (SQLite)
DB_PATH: Path = Path(__file__).resolve().parent.parent / "casterbot.db"
