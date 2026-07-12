"""Standalone web server for local UI testing (no Discord bot)."""
import asyncio
import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
