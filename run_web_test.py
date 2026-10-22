"""Standalone web server for local UI testing (no Discord bot)."""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Safety: never touch the production database during a local web test.
# Force local SQLite regardless of what .env contains.
os.environ["DATABASE_URL"] = ""

# Safety: force the OAuth callback back to localhost, not the production
# https://casterbot.mooo.com URL from .env.
os.environ["WEB_PUBLIC_URL"] = "http://localhost:8080"

# Enable the local dev bypass so role checks pass without a live Discord bot.
# Production never sets this.
os.environ["DEV_MODE"] = "1"

from casterbot import config, db
from casterbot.web import create_app
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("web-test")


async def main() -> None:
    # Init the DB (needed for the web handlers)
    await db.init_db()
    log.info("DB initialized")

    app = create_app(bot=None)
    runner = web.AppRunner(app)
    await runner.setup()

    host = "0.0.0.0"
    port = 8080
    site = web.TCPSite(runner, host, port)
    await site.start()

    log.info(f"Web server running at http://localhost:{port}")
    log.info("Press Ctrl+C to stop")

    # Keep running
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped")
