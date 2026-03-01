"""SQLite persistence for match claims."""
from __future__ import annotations

import aiosqlite

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    simple_id INTEGER UNIQUE,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    match_date TEXT NOT NULL,
    match_time TEXT NOT NULL,
    match_timestamp INTEGER,
    message_id INTEGER,
    channel_id INTEGER,
    private_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,  -- 'caster' or 'camop'
    slot INTEGER NOT NULL,  -- 1-3 for casters, 1 for camop
    UNIQUE(match_id, role, slot),
    FOREIGN KEY(match_id) REFERENCES matches(match_id)
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Migration: add match_timestamp column if missing
        cursor = await db.execute("PRAGMA table_info(matches)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "match_timestamp" not in columns:
            await db.execute("ALTER TABLE matches ADD COLUMN match_timestamp INTEGER")
        # Migration: add simple_id column if missing
        if "simple_id" not in columns:
            await db.execute("ALTER TABLE matches ADD COLUMN simple_id INTEGER")
            # Assign simple_ids to existing matches
            cursor = await db.execute("SELECT match_id FROM matches ORDER BY rowid")
            rows = await cursor.fetchall()
            for idx, row in enumerate(rows, start=1):
                await db.execute("UPDATE matches SET simple_id = ? WHERE match_id = ?", (idx, row[0]))
            # Create unique index
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_simple_id ON matches(simple_id)")
        await db.commit()


async def _next_simple_id() -> int:
    """Get the next available simple_id."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("SELECT MAX(simple_id) FROM matches")
        row = await cursor.fetchone()
        return (row[0] or 0) + 1


async def upsert_match(
    match_id: str,
    team_a: str,
    team_b: str,
    match_date: str,
    match_time: str,
    match_timestamp: int | None = None,
) -> bool:
    """Insert or update a match. Returns True if newly inserted."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
        )
        exists = await cursor.fetchone()
        if exists:
            # Update timestamp if missing
            if match_timestamp:
                await db.execute(
                    "UPDATE matches SET match_timestamp = ? WHERE match_id = ? AND match_timestamp IS NULL",
                    (match_timestamp, match_id),
                )
                await db.commit()
            return False
        simple_id = await _next_simple_id()
        await db.execute(
            """
            INSERT INTO matches (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp),
        )
        await db.commit()
        return True


async def set_message_id(match_id: str, message_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET message_id = ?, channel_id = ? WHERE match_id = ?",
            (message_id, channel_id, match_id),
        )
        await db.commit()


async def set_private_channel(match_id: str, private_channel_id: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET private_channel_id = ? WHERE match_id = ?",
            (private_channel_id, match_id),
        )
        await db.commit()


async def clear_private_channel(match_id: str) -> None:
    """Clear the private_channel_id for a match (e.g., if channel was deleted)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET private_channel_id = NULL WHERE match_id = ?",
            (match_id,),
        )
        await db.commit()


async def get_match(match_id: str) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_match_by_simple_id(simple_id: int) -> dict | None:
    """Look up a match by its simple numeric ID."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM matches WHERE simple_id = ?", (simple_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_slot_holder(match_id: str, role: str, slot: int) -> int | None:
    """Get the user_id of whoever holds a slot, or None if empty."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
            (match_id, role, slot),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def claim_slot(match_id: str, user_id: int, role: str, slot: int) -> int | None:
    """Claim a slot, overriding any existing claim. Returns previous holder's user_id or None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Get current holder before replacing
        cursor = await db.execute(
            "SELECT user_id FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
            (match_id, role, slot),
        )
        row = await cursor.fetchone()
        previous_holder = row[0] if row else None
        
        # Replace the claim
        await db.execute(
            "DELETE FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
            (match_id, role, slot),
        )
        await db.execute(
            """
            INSERT INTO claims (match_id, user_id, role, slot)
            VALUES (?, ?, ?, ?)
            """,
            (match_id, user_id, role, slot),
        )
        await db.commit()
        return previous_holder


async def unclaim_slot(match_id: str, user_id: int, role: str, slot: int) -> bool:
    """Remove a claim. Returns True if removed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """
            DELETE FROM claims
            WHERE match_id = ? AND user_id = ? AND role = ? AND slot = ?
            """,
            (match_id, user_id, role, slot),
        )
        await db.commit()
        return cursor.rowcount > 0


async def remove_claim_by_slot(match_id: str, role: str, slot: int) -> int | None:
    """Remove a claim by slot regardless of user. Returns removed user_id or None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
            (match_id, role, slot),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        user_id = row[0]
        await db.execute(
            "DELETE FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
            (match_id, role, slot),
        )
        await db.commit()
        return user_id


async def get_claims(match_id: str) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claims WHERE match_id = ?", (match_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_matches_without_message() -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM matches WHERE message_id IS NULL"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_matches_with_message() -> list[dict]:
    """Get all matches that have a posted Discord message."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM matches WHERE message_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def clear_message_id(match_id: str) -> None:
    """Clear the message_id for a match (e.g., if message was deleted)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET message_id = NULL, channel_id = NULL WHERE match_id = ?",
            (match_id,),
        )
        await db.commit()


async def delete_match(match_id: str) -> None:
    """Delete a match and its claims from the database."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM claims WHERE match_id = ?", (match_id,))
        await db.execute("DELETE FROM matches WHERE match_id = ?", (match_id,))
        await db.commit()
