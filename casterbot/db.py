"""Database persistence for match claims. Supports PostgreSQL (Render) and SQLite (local)."""
from __future__ import annotations  # AGENTS-AUDIT: banned by AGENTS for active Python 3.14-oriented code.

import logging
from contextlib import asynccontextmanager

from . import config

# === MAINTAINABILITY / AGENTS AUDIT ANNOTATIONS ===
# AGENTS violation: uses `from __future__ import annotations` in an active module.
# AGENTS violation: extensive use of shape-less `dict` return values instead of TypedDict/dataclass contracts.
# AGENTS violation: helper signatures use untyped variadic args (`*args`) with no explicit types.
# AGENTS violation: no Protocol abstraction for storage backend; business layer is directly DB-vendor aware.
# Code smell: module is very large and combines schema, migration, queries, and domain logic.
# Code smell: PostgreSQL and SQLite branches duplicate near-identical logic, increasing drift risk.
# Code smell: global mutable connection pool state (`_pool`) complicates lifecycle and test isolation.
# Code smell: many repeated runtime imports inside functions increase noise and maintenance overhead.
# Code smell: weak transaction boundaries across multi-step writes can leave partial state on failures.
# AUDIT COUNTS: format gate failed for this file; ruff findings=0; pyright findings=4.
# AUDIT COUNTS: source scan found future_imports=1, broad_except=0, untyped_defs=4, dict_shapes=43.
# AUDIT SCOPE: every `dict`/`list[dict]` return shape is part of the TypedDict/dataclass violation class.
# AUDIT SCOPE: `_pool = None` is the controlling site for optional-pool pyright failures.

log = logging.getLogger("casterbot.db")

# Determine backend based on DATABASE_URL
_use_pg = bool(config.DATABASE_URL)

# Connection pool (PostgreSQL only)
_pool = None  # AGENTS-AUDIT: optional global pool is the root of pyright optional-member failures.

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    simple_id INTEGER UNIQUE,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    match_date TEXT NOT NULL,
    match_time TEXT NOT NULL,
    match_timestamp INTEGER,
    week_number TEXT,
    match_type TEXT,
    message_id BIGINT,
    channel_id BIGINT,
    private_channel_id BIGINT,
    missing_since INTEGER,
    stream_channel INTEGER
);

CREATE TABLE IF NOT EXISTS claims (
    id SERIAL PRIMARY KEY,
    match_id TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    role TEXT NOT NULL,
    slot INTEGER NOT NULL,
    UNIQUE(match_id, role, slot),
    FOREIGN KEY(match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS caster_stats (
    user_id BIGINT PRIMARY KEY,
    cast_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leaderboard_cycles (
    cycle_id SERIAL PRIMARY KEY,
    cycle_name TEXT NOT NULL,
    weeks INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leaderboard_archive (
    id SERIAL PRIMARY KEY,
    cycle_id INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    cast_count INTEGER NOT NULL,
    FOREIGN KEY(cycle_id) REFERENCES leaderboard_cycles(cycle_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_pictures (
    user_id BIGINT PRIMARY KEY,
    filename TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS team_logos (
    team_name TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    discord_message_id BIGINT,
    approved_by BIGINT,
    approved_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bracket_slots (
    slot TEXT PRIMARY KEY,
    team_a TEXT,
    team_b TEXT,
    score_a INTEGER NOT NULL DEFAULT 0,
    score_b INTEGER NOT NULL DEFAULT 0,
    winner TEXT,
    match_id TEXT,
    stream_channel INTEGER
);

CREATE TABLE IF NOT EXISTS bracket_claims (
    id SERIAL PRIMARY KEY,
    slot TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    slot_num INTEGER NOT NULL DEFAULT 1,
    UNIQUE(slot, role, slot_num)
);
"""

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    simple_id INTEGER UNIQUE,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    match_date TEXT NOT NULL,
    match_time TEXT NOT NULL,
    match_timestamp INTEGER,
    week_number TEXT,
    match_type TEXT,
    message_id INTEGER,
    channel_id INTEGER,
    private_channel_id INTEGER,
    missing_since INTEGER,
    stream_channel INTEGER
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    slot INTEGER NOT NULL,
    UNIQUE(match_id, role, slot),
    FOREIGN KEY(match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS caster_stats (
    user_id INTEGER PRIMARY KEY,
    cast_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leaderboard_cycles (
    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_name TEXT NOT NULL,
    weeks INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leaderboard_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    cast_count INTEGER NOT NULL,
    FOREIGN KEY(cycle_id) REFERENCES leaderboard_cycles(cycle_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_pictures (
    user_id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS team_logos (
    team_name TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    discord_message_id INTEGER,
    approved_by INTEGER,
    approved_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bracket_slots (
    slot TEXT PRIMARY KEY,
    team_a TEXT,
    team_b TEXT,
    score_a INTEGER NOT NULL DEFAULT 0,
    score_b INTEGER NOT NULL DEFAULT 0,
    winner TEXT,
    match_id TEXT,
    stream_channel INTEGER
);

CREATE TABLE IF NOT EXISTS bracket_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    slot_num INTEGER NOT NULL DEFAULT 1,
    UNIQUE(slot, role, slot_num)
);
"""


# ============ Connection Helpers ============


@asynccontextmanager
async def _get_pg_conn():  # AGENTS-AUDIT: async context helper lacks explicit return type.
    """Get a connection from the PostgreSQL pool."""
    async with _pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def _get_sqlite_conn():
    """Get an aiosqlite connection."""
    import aiosqlite
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def _pg_fetch(query: str, *args) -> list[dict]:
    """Execute a PostgreSQL query and return rows as dicts."""
    async with _get_pg_conn() as conn:
        rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]


async def _pg_fetchrow(query: str, *args) -> dict | None:
    """Execute a PostgreSQL query and return a single row as dict."""
    async with _get_pg_conn() as conn:
        row = await conn.fetchrow(query, *args)
        return dict(row) if row else None


async def _pg_fetchval(query: str, *args):
    """Execute a PostgreSQL query and return a single value."""
    async with _get_pg_conn() as conn:
        return await conn.fetchval(query, *args)


async def _pg_execute(query: str, *args):
    """Execute a PostgreSQL query (no return)."""
    async with _get_pg_conn() as conn:
        return await conn.execute(query, *args)


# ============ Initialization ============


async def init_db() -> None:
    """Initialize the database (create tables, run migrations)."""
    global _pool

    if _use_pg:
        import asyncpg
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
        # Create all tables
        async with _get_pg_conn() as conn:
            await conn.execute(_PG_SCHEMA)
        log.info("PostgreSQL database initialized")
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.executescript(_SQLITE_SCHEMA)
            # Run SQLite migrations
            cursor = await db.execute("PRAGMA table_info(matches)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "match_timestamp" not in columns:
                await db.execute("ALTER TABLE matches ADD COLUMN match_timestamp INTEGER")
            if "simple_id" not in columns:
                await db.execute("ALTER TABLE matches ADD COLUMN simple_id INTEGER")
                cursor = await db.execute("SELECT match_id FROM matches ORDER BY rowid")
                rows = await cursor.fetchall()
                for idx, row in enumerate(rows, start=1):
                    await db.execute("UPDATE matches SET simple_id = ? WHERE match_id = ?", (idx, row[0]))
                await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_simple_id ON matches(simple_id)")
            if "week_number" not in columns:
                await db.execute("ALTER TABLE matches ADD COLUMN week_number TEXT")
            if "match_type" not in columns:
                await db.execute("ALTER TABLE matches ADD COLUMN match_type TEXT")
            if "missing_since" not in columns:
                await db.execute("ALTER TABLE matches ADD COLUMN missing_since INTEGER")
            if "stream_channel" not in columns:
                await db.execute("ALTER TABLE matches ADD COLUMN stream_channel INTEGER")
            await db.commit()
        log.info("SQLite database initialized")


# ============ Match Functions ============


async def _next_simple_id() -> int:
    """Get the next available simple_id."""
    if _use_pg:
        val = await _pg_fetchval("SELECT MAX(simple_id) FROM matches")
        return (val or 0) + 1
    else:
        import aiosqlite
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
    match_type: str | None = None,
) -> bool:
    """Insert or update a match. Returns True if newly inserted."""
    if _use_pg:
        existing = await _pg_fetchval("SELECT 1 FROM matches WHERE match_id = $1", match_id)
        if existing:
            if match_timestamp:
                await _pg_execute(
                    "UPDATE matches SET match_timestamp = $1 WHERE match_id = $2 AND match_timestamp IS NULL",
                    match_timestamp, match_id,
                )
            if match_type:
                await _pg_execute(
                    "UPDATE matches SET match_type = $1 WHERE match_id = $2",
                    match_type, match_id,
                )
            return False
        simple_id = await _next_simple_id()
        await _pg_execute(
            """INSERT INTO matches (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp, match_type)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp, match_type,
        )
        return True
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,))
            exists = await cursor.fetchone()
            if exists:
                if match_timestamp:
                    await db.execute(
                        "UPDATE matches SET match_timestamp = ? WHERE match_id = ? AND match_timestamp IS NULL",
                        (match_timestamp, match_id),
                    )
                if match_type:
                    await db.execute(
                        "UPDATE matches SET match_type = ? WHERE match_id = ?",
                        (match_type, match_id),
                    )
                await db.commit()
                return False
            simple_id = await _next_simple_id()
            await db.execute(
                """INSERT INTO matches (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp, match_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp, match_type),
            )
            await db.commit()
            return True


async def set_message_id(match_id: str, message_id: int, channel_id: int) -> None:
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET message_id = $1, channel_id = $2 WHERE match_id = $3",
            message_id, channel_id, match_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET message_id = ?, channel_id = ? WHERE match_id = ?",
                (message_id, channel_id, match_id),
            )
            await db.commit()


async def set_private_channel(match_id: str, private_channel_id: int) -> None:
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET private_channel_id = $1 WHERE match_id = $2",
            private_channel_id, match_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET private_channel_id = ? WHERE match_id = ?",
                (private_channel_id, match_id),
            )
            await db.commit()


async def clear_private_channel(match_id: str) -> None:
    """Clear the private_channel_id for a match."""
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET private_channel_id = NULL WHERE match_id = $1", match_id
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET private_channel_id = NULL WHERE match_id = ?",
                (match_id,),
            )
            await db.commit()


async def get_match_by_channel_id(channel_id: int) -> dict | None:
    """Get a match by its private_channel_id."""
    if _use_pg:
        return await _pg_fetchrow(
            "SELECT * FROM matches WHERE private_channel_id = $1", channel_id
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM matches WHERE private_channel_id = ?", (channel_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_stream_channel(match_id: str, stream_channel: int) -> None:
    """Set the stream channel (1 or 2) for a match."""
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET stream_channel = $1 WHERE match_id = $2",
            stream_channel, match_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET stream_channel = ? WHERE match_id = ?",
                (stream_channel, match_id),
            )
            await db.commit()


async def get_match(match_id: str) -> dict | None:
    if _use_pg:
        return await _pg_fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_match_by_simple_id(simple_id: int) -> dict | None:
    """Look up a match by its simple numeric ID."""
    if _use_pg:
        return await _pg_fetchrow("SELECT * FROM matches WHERE simple_id = $1", simple_id)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM matches WHERE simple_id = ?", (simple_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_slot_holder(match_id: str, role: str, slot: int) -> int | None:
    """Get the user_id of whoever holds a slot, or None if empty."""
    if _use_pg:
        return await _pg_fetchval(
            "SELECT user_id FROM claims WHERE match_id = $1 AND role = $2 AND slot = $3",
            match_id, role, slot,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
                (match_id, role, slot),
            )
            row = await cursor.fetchone()
            return row[0] if row else None


async def claim_slot(match_id: str, user_id: int, role: str, slot: int) -> int | None:
    """Claim a slot, overriding any existing claim. Returns previous holder's user_id or None."""
    if _use_pg:
        previous = await _pg_fetchval(
            "SELECT user_id FROM claims WHERE match_id = $1 AND role = $2 AND slot = $3",
            match_id, role, slot,
        )
        await _pg_execute(
            "DELETE FROM claims WHERE match_id = $1 AND role = $2 AND slot = $3",
            match_id, role, slot,
        )
        await _pg_execute(
            "INSERT INTO claims (match_id, user_id, role, slot) VALUES ($1, $2, $3, $4)",
            match_id, user_id, role, slot,
        )
        return previous
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
                (match_id, role, slot),
            )
            row = await cursor.fetchone()
            previous_holder = row[0] if row else None
            await db.execute(
                "DELETE FROM claims WHERE match_id = ? AND role = ? AND slot = ?",
                (match_id, role, slot),
            )
            await db.execute(
                "INSERT INTO claims (match_id, user_id, role, slot) VALUES (?, ?, ?, ?)",
                (match_id, user_id, role, slot),
            )
            await db.commit()
            return previous_holder


async def unclaim_slot(match_id: str, user_id: int, role: str, slot: int) -> bool:
    """Remove a claim. Returns True if removed."""
    if _use_pg:
        result = await _pg_execute(
            "DELETE FROM claims WHERE match_id = $1 AND user_id = $2 AND role = $3 AND slot = $4",
            match_id, user_id, role, slot,
        )
        return "DELETE 0" not in result
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM claims WHERE match_id = ? AND user_id = ? AND role = ? AND slot = ?",
                (match_id, user_id, role, slot),
            )
            await db.commit()
            return cursor.rowcount > 0


async def remove_claim_by_slot(match_id: str, role: str, slot: int) -> int | None:
    """Remove a claim by slot regardless of user. Returns removed user_id or None."""
    if _use_pg:
        user_id = await _pg_fetchval(
            "SELECT user_id FROM claims WHERE match_id = $1 AND role = $2 AND slot = $3",
            match_id, role, slot,
        )
        if user_id is None:
            return None
        await _pg_execute(
            "DELETE FROM claims WHERE match_id = $1 AND role = $2 AND slot = $3",
            match_id, role, slot,
        )
        return user_id
    else:
        import aiosqlite
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
    if _use_pg:
        return await _pg_fetch("SELECT * FROM claims WHERE match_id = $1", match_id)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM claims WHERE match_id = ?", (match_id,)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_matches_without_message() -> list[dict]:
    """Get matches that need claim messages posted."""
    if _use_pg:
        return await _pg_fetch(
            "SELECT * FROM matches WHERE message_id IS NULL AND private_channel_id IS NULL"
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM matches WHERE message_id IS NULL AND private_channel_id IS NULL"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_matches_with_message() -> list[dict]:
    """Get all matches that have a posted Discord message or active private channel."""
    if _use_pg:
        return await _pg_fetch(
            "SELECT * FROM matches WHERE message_id IS NOT NULL OR private_channel_id IS NOT NULL"
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM matches WHERE message_id IS NOT NULL OR private_channel_id IS NOT NULL"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def clear_message_id(match_id: str) -> None:
    """Clear the message_id for a match."""
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET message_id = NULL, channel_id = NULL WHERE match_id = $1",
            match_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET message_id = NULL, channel_id = NULL WHERE match_id = ?",
                (match_id,),
            )
            await db.commit()


async def delete_match(match_id: str) -> None:
    """Delete a match and its claims from the database."""
    if _use_pg:
        await _pg_execute("DELETE FROM claims WHERE match_id = $1", match_id)
        await _pg_execute("DELETE FROM matches WHERE match_id = $1", match_id)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("DELETE FROM claims WHERE match_id = ?", (match_id,))
            await db.execute("DELETE FROM matches WHERE match_id = ?", (match_id,))
            await db.commit()


async def mark_match_missing(match_id: str) -> None:
    """Mark a match as missing from the sheet."""
    import time
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET missing_since = $1 WHERE match_id = $2 AND missing_since IS NULL",
            int(time.time()), match_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET missing_since = ? WHERE match_id = ? AND missing_since IS NULL",
                (int(time.time()), match_id),
            )
            await db.commit()


async def clear_match_missing(match_id: str) -> None:
    """Clear the missing_since flag."""
    if _use_pg:
        await _pg_execute(
            "UPDATE matches SET missing_since = NULL WHERE match_id = $1", match_id
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE matches SET missing_since = NULL WHERE match_id = ?",
                (match_id,),
            )
            await db.commit()


async def get_missing_since(match_id: str) -> int | None:
    """Get the timestamp when a match was first seen as missing."""
    if _use_pg:
        return await _pg_fetchval(
            "SELECT missing_since FROM matches WHERE match_id = $1", match_id
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT missing_since FROM matches WHERE match_id = ?", (match_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None


# -- Caster Leaderboard Functions --


async def increment_cast_count(user_id: int) -> int:
    """Increment cast count for a user. Returns the new count."""
    if _use_pg:
        await _pg_execute(
            """INSERT INTO caster_stats (user_id, cast_count) VALUES ($1, 1)
               ON CONFLICT(user_id) DO UPDATE SET cast_count = caster_stats.cast_count + 1""",
            user_id,
        )
        val = await _pg_fetchval("SELECT cast_count FROM caster_stats WHERE user_id = $1", user_id)
        return val or 1
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                """INSERT INTO caster_stats (user_id, cast_count) VALUES (?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET cast_count = cast_count + 1""",
                (user_id,),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT cast_count FROM caster_stats WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 1


async def get_caster_leaderboard(limit: int = 10) -> list[dict]:
    """Get top casters by cast count."""
    if _use_pg:
        return await _pg_fetch(
            "SELECT user_id, cast_count FROM caster_stats ORDER BY cast_count DESC LIMIT $1",
            limit,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT user_id, cast_count FROM caster_stats ORDER BY cast_count DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_user_cast_count(user_id: int) -> int:
    """Get a single user's cast count."""
    if _use_pg:
        val = await _pg_fetchval("SELECT cast_count FROM caster_stats WHERE user_id = $1", user_id)
        return val or 0
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT cast_count FROM caster_stats WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0


async def reset_leaderboard() -> int:
    """Reset all caster stats. Returns number of entries cleared."""
    if _use_pg:
        count = await _pg_fetchval("SELECT COUNT(*) FROM caster_stats")
        await _pg_execute("DELETE FROM caster_stats")
        return count or 0
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM caster_stats")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await db.execute("DELETE FROM caster_stats")
            await db.commit()
            return count


async def set_cast_count(user_id: int, count: int) -> None:
    """Set a user's cast count to a specific value."""
    if _use_pg:
        if count <= 0:
            await _pg_execute("DELETE FROM caster_stats WHERE user_id = $1", user_id)
        else:
            await _pg_execute(
                """INSERT INTO caster_stats (user_id, cast_count) VALUES ($1, $2)
                   ON CONFLICT(user_id) DO UPDATE SET cast_count = $2""",
                user_id, count,
            )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            if count <= 0:
                await db.execute("DELETE FROM caster_stats WHERE user_id = ?", (user_id,))
            else:
                await db.execute(
                    """INSERT INTO caster_stats (user_id, cast_count) VALUES (?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET cast_count = ?""",
                    (user_id, count, count),
                )
            await db.commit()


# -- Leaderboard Cycle Functions --


async def archive_cycle(cycle_name: str, weeks: int = 0, start_date: str = "", end_date: str = "") -> int:
    """Archive current leaderboard to a new cycle and reset. Returns the cycle_id."""
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = today
    if not start_date:
        start_date = today

    if _use_pg:
        cycle_id = await _pg_fetchval(
            """INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date)
               VALUES ($1, $2, $3, $4) RETURNING cycle_id""",
            cycle_name, weeks, start_date, end_date,
        )
        await _pg_execute(
            """INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
               SELECT $1, user_id, cast_count FROM caster_stats WHERE cast_count > 0""",
            cycle_id,
        )
        await _pg_execute("DELETE FROM caster_stats")
        await _pg_execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
        return cycle_id
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date) VALUES (?, ?, ?, ?)",
                (cycle_name, weeks, start_date, end_date),
            )
            cycle_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count) SELECT ?, user_id, cast_count FROM caster_stats WHERE cast_count > 0",
                (cycle_id,),
            )
            await db.execute("DELETE FROM caster_stats")
            await db.execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
            await db.commit()
            return cycle_id


async def start_cycle(cycle_name: str, weeks: int) -> dict:
    """Start a new active cycle. Archives current leaderboard first if there's data."""
    from datetime import datetime, timedelta

    today = datetime.now()
    start_date = today.strftime("%Y-%m-%d")
    end_date = (today + timedelta(weeks=weeks)).strftime("%Y-%m-%d")

    if _use_pg:
        has_stats = await _pg_fetchval("SELECT COUNT(*) FROM caster_stats WHERE cast_count > 0")
        old_cycle_name = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_name'")

        archived_id = None
        if has_stats and old_cycle_name:
            old_start = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_start'") or start_date
            old_weeks_val = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_weeks'")
            old_weeks = int(old_weeks_val) if old_weeks_val else weeks

            archived_id = await _pg_fetchval(
                """INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date)
                   VALUES ($1, $2, $3, $4) RETURNING cycle_id""",
                old_cycle_name, old_weeks, old_start, start_date,
            )
            await _pg_execute(
                """INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
                   SELECT $1, user_id, cast_count FROM caster_stats WHERE cast_count > 0""",
                archived_id,
            )
            await _pg_execute("DELETE FROM caster_stats")

        await _pg_execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
        await _pg_execute("INSERT INTO settings (key, value) VALUES ('active_cycle_name', $1) ON CONFLICT(key) DO UPDATE SET value = $1", cycle_name)
        await _pg_execute("INSERT INTO settings (key, value) VALUES ('active_cycle_weeks', $1) ON CONFLICT(key) DO UPDATE SET value = $1", str(weeks))
        await _pg_execute("INSERT INTO settings (key, value) VALUES ('active_cycle_start', $1) ON CONFLICT(key) DO UPDATE SET value = $1", start_date)
        await _pg_execute("INSERT INTO settings (key, value) VALUES ('active_cycle_end', $1) ON CONFLICT(key) DO UPDATE SET value = $1", end_date)

        return {"name": cycle_name, "weeks": weeks, "start_date": start_date, "end_date": end_date, "archived_id": archived_id}
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM caster_stats WHERE cast_count > 0")
            row = await cursor.fetchone()
            has_stats = row[0] > 0

            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_name'")
            row = await cursor.fetchone()
            old_cycle_name = row[0] if row else None

            archived_id = None
            if has_stats and old_cycle_name:
                cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_start'")
                row = await cursor.fetchone()
                old_start = row[0] if row else start_date

                cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_weeks'")
                row = await cursor.fetchone()
                old_weeks = int(row[0]) if row else weeks

                cursor = await db.execute(
                    "INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date) VALUES (?, ?, ?, ?)",
                    (old_cycle_name, old_weeks, old_start, start_date),
                )
                archived_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count) SELECT ?, user_id, cast_count FROM caster_stats WHERE cast_count > 0",
                    (archived_id,),
                )
                await db.execute("DELETE FROM caster_stats")

            await db.execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_name', ?)", (cycle_name,))
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_weeks', ?)", (str(weeks),))
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_start', ?)", (start_date,))
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_end', ?)", (end_date,))
            await db.commit()

            return {"name": cycle_name, "weeks": weeks, "start_date": start_date, "end_date": end_date, "archived_id": archived_id}


async def get_active_cycle() -> dict | None:
    """Get the currently active cycle, or None."""
    if _use_pg:
        name = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_name'")
        if not name:
            return None
        weeks_val = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_weeks'")
        start_date = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_start'")
        end_date = await _pg_fetchval("SELECT value FROM settings WHERE key = 'active_cycle_end'")
        return {"name": name, "weeks": int(weeks_val) if weeks_val else 0, "start_date": start_date or "", "end_date": end_date or ""}
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_name'")
            row = await cursor.fetchone()
            if not row:
                return None
            name = row[0]
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_weeks'")
            row = await cursor.fetchone()
            weeks = int(row[0]) if row else 0
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_start'")
            row = await cursor.fetchone()
            start_date = row[0] if row else ""
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_end'")
            row = await cursor.fetchone()
            end_date = row[0] if row else ""
            return {"name": name, "weeks": weeks, "start_date": start_date, "end_date": end_date}


async def end_active_cycle() -> int | None:
    """End the active cycle now and archive. Returns cycle_id or None."""
    from datetime import datetime

    active = await get_active_cycle()
    if not active:
        return None

    today = datetime.now().strftime("%Y-%m-%d")

    if _use_pg:
        cycle_id = await _pg_fetchval(
            """INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date)
               VALUES ($1, $2, $3, $4) RETURNING cycle_id""",
            active["name"], active["weeks"], active["start_date"], today,
        )
        await _pg_execute(
            """INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
               SELECT $1, user_id, cast_count FROM caster_stats WHERE cast_count > 0""",
            cycle_id,
        )
        await _pg_execute("DELETE FROM caster_stats")
        await _pg_execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
        return cycle_id
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date) VALUES (?, ?, ?, ?)",
                (active["name"], active["weeks"], active["start_date"], today),
            )
            cycle_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count) SELECT ?, user_id, cast_count FROM caster_stats WHERE cast_count > 0",
                (cycle_id,),
            )
            await db.execute("DELETE FROM caster_stats")
            await db.execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
            await db.commit()
            return cycle_id


async def check_cycle_end() -> int | None:
    """Check if active cycle has ended. If so, archive it. Returns cycle_id if archived."""
    from datetime import datetime

    active = await get_active_cycle()
    if not active or not active["end_date"]:
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    if today >= active["end_date"]:
        return await end_active_cycle()
    return None


async def get_cycles() -> list[dict]:
    """Get all archived cycles, ordered by most recent first."""
    if _use_pg:
        return await _pg_fetch(
            "SELECT cycle_id, cycle_name, weeks, start_date, end_date FROM leaderboard_cycles ORDER BY cycle_id DESC"
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT cycle_id, cycle_name, weeks, start_date, end_date FROM leaderboard_cycles ORDER BY cycle_id DESC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_cycle_leaderboard(cycle_id: int) -> list[dict]:
    """Get the leaderboard for a specific archived cycle."""
    if _use_pg:
        return await _pg_fetch(
            "SELECT user_id, cast_count FROM leaderboard_archive WHERE cycle_id = $1 ORDER BY cast_count DESC",
            cycle_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT user_id, cast_count FROM leaderboard_archive WHERE cycle_id = ? ORDER BY cast_count DESC",
                (cycle_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_cycle_by_id(cycle_id: int) -> dict | None:
    """Get a specific cycle's metadata."""
    if _use_pg:
        return await _pg_fetchrow(
            "SELECT cycle_id, cycle_name, weeks, start_date, end_date FROM leaderboard_cycles WHERE cycle_id = $1",
            cycle_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT cycle_id, cycle_name, weeks, start_date, end_date FROM leaderboard_cycles WHERE cycle_id = ?",
                (cycle_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None


# -- Settings Functions --


async def set_setting(key: str, value: str) -> None:
    """Set a setting value."""
    if _use_pg:
        await _pg_execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT(key) DO UPDATE SET value = $2",
            key, value,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                (key, value, value),
            )
            await db.commit()


async def get_setting(key: str) -> str | None:
    """Get a setting value."""
    if _use_pg:
        return await _pg_fetchval("SELECT value FROM settings WHERE key = $1", key)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None


# -- Web View Functions --


async def get_all_matches_sorted_by_time() -> list[dict]:
    """Get all matches with claims, sorted by match_timestamp (ascending)."""
    if _use_pg:
        return await _pg_fetch(
            """SELECT * FROM matches
               WHERE message_id IS NOT NULL OR private_channel_id IS NOT NULL
               ORDER BY match_timestamp ASC NULLS LAST"""
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM matches
                   WHERE message_id IS NOT NULL OR private_channel_id IS NOT NULL
                   ORDER BY match_timestamp ASC NULLS LAST"""
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# -- Profile Picture Functions --


async def get_profile_picture(user_id: int) -> str | None:
    """Get the custom profile picture filename for a user, or None if not set."""
    if _use_pg:
        return await _pg_fetchval("SELECT filename FROM profile_pictures WHERE user_id = $1", user_id)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT filename FROM profile_pictures WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_profile_picture(user_id: int, filename: str) -> None:
    """Set a custom profile picture for a user."""
    import time
    if _use_pg:
        now = int(time.time())
        await _pg_execute(
            """INSERT INTO profile_pictures (user_id, filename, uploaded_at) VALUES ($1, $2, $3)
               ON CONFLICT(user_id) DO UPDATE SET filename = $2, uploaded_at = $3""",
            user_id, filename, now,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                """INSERT INTO profile_pictures (user_id, filename, uploaded_at) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET filename = ?, uploaded_at = ?""",
                (user_id, filename, int(time.time()), filename, int(time.time()))
            )
            await db.commit()


async def delete_profile_picture(user_id: int) -> str | None:
    """Delete a user's custom profile picture. Returns the old filename or None."""
    if _use_pg:
        filename = await _pg_fetchval("SELECT filename FROM profile_pictures WHERE user_id = $1", user_id)
        if filename:
            await _pg_execute("DELETE FROM profile_pictures WHERE user_id = $1", user_id)
        return filename
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT filename FROM profile_pictures WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                await db.execute("DELETE FROM profile_pictures WHERE user_id = ?", (user_id,))
                await db.commit()
                return row[0]
            return None


# ============ Team Logos ============


async def get_team_logo(team_name: str) -> dict | None:
    """Get the approved logo for a team."""
    if _use_pg:
        return await _pg_fetchrow(
            "SELECT * FROM team_logos WHERE LOWER(team_name) = LOWER($1)", team_name
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM team_logos WHERE team_name = ? COLLATE NOCASE", (team_name,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_team_logos() -> list[dict]:
    """Get all approved team logos."""
    if _use_pg:
        return await _pg_fetch("SELECT * FROM team_logos ORDER BY team_name")
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM team_logos ORDER BY team_name")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def set_team_logo(team_name: str, filename: str, discord_message_id: int, approved_by: int) -> None:
    """Set/update a team's approved logo."""
    import time
    if _use_pg:
        now = int(time.time())
        await _pg_execute(
            """INSERT INTO team_logos (team_name, filename, discord_message_id, approved_by, approved_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(team_name) DO UPDATE SET filename = $2, discord_message_id = $3, approved_by = $4, approved_at = $5""",
            team_name, filename, discord_message_id, approved_by, now,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                """INSERT INTO team_logos (team_name, filename, discord_message_id, approved_by, approved_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(team_name) DO UPDATE SET filename = ?, discord_message_id = ?, approved_by = ?, approved_at = ?""",
                (team_name, filename, discord_message_id, approved_by, int(time.time()),
                 filename, discord_message_id, approved_by, int(time.time()))
            )
            await db.commit()


async def delete_team_logo(team_name: str) -> str | None:
    """Delete a team's logo. Returns the old filename or None."""
    if _use_pg:
        filename = await _pg_fetchval(
            "SELECT filename FROM team_logos WHERE LOWER(team_name) = LOWER($1)", team_name
        )
        if filename:
            await _pg_execute("DELETE FROM team_logos WHERE LOWER(team_name) = LOWER($1)", team_name)
        return filename
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT filename FROM team_logos WHERE team_name = ? COLLATE NOCASE", (team_name,)
            )
            row = await cursor.fetchone()
            if row:
                await db.execute("DELETE FROM team_logos WHERE team_name = ? COLLATE NOCASE", (team_name,))
                await db.commit()
                return row[0]
            return None


async def rename_team_logo(old_team_name: str, new_team_name: str) -> bool:
    """Rename a team's logo to a new team name. Returns True on success."""
    if _use_pg:
        filename = await _pg_fetchval(
            "SELECT filename FROM team_logos WHERE LOWER(team_name) = LOWER($1)", old_team_name
        )
        if not filename:
            return False
        await _pg_execute("DELETE FROM team_logos WHERE LOWER(team_name) = LOWER($1)", new_team_name)
        await _pg_execute(
            "UPDATE team_logos SET team_name = $1 WHERE LOWER(team_name) = LOWER($2)",
            new_team_name, old_team_name,
        )
        return True
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT filename FROM team_logos WHERE team_name = ? COLLATE NOCASE", (old_team_name,)
            )
            row = await cursor.fetchone()
            if not row:
                return False
            await db.execute("DELETE FROM team_logos WHERE team_name = ? COLLATE NOCASE", (new_team_name,))
            await db.execute(
                "UPDATE team_logos SET team_name = ? WHERE team_name = ? COLLATE NOCASE",
                (new_team_name, old_team_name)
            )
            await db.commit()
            return True


# ============ Finals Bracket ============


async def get_bracket_slot(slot: str) -> dict | None:
    """Get a single bracket slot."""
    if _use_pg:
        return await _pg_fetchrow("SELECT * FROM bracket_slots WHERE slot = $1", slot)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM bracket_slots WHERE slot = ?", (slot,))
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_bracket_slots() -> dict[str, dict]:
    """Get all bracket slots as {slot_name: data}."""
    if _use_pg:
        rows = await _pg_fetch("SELECT * FROM bracket_slots")
        return {r["slot"]: r for r in rows}
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM bracket_slots")
            rows = await cursor.fetchall()
            return {row["slot"]: dict(row) for row in rows}


async def set_bracket_slot(slot: str, team_a: str | None, team_b: str | None,
                           winner: str | None = None, match_id: str | None = None) -> None:
    """Set or update a bracket slot."""
    if _use_pg:
        await _pg_execute(
            """INSERT INTO bracket_slots (slot, team_a, team_b, winner, match_id)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(slot) DO UPDATE SET team_a = $2, team_b = $3, winner = $4, match_id = $5""",
            slot, team_a, team_b, winner, match_id,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                """INSERT INTO bracket_slots (slot, team_a, team_b, winner, match_id)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slot) DO UPDATE SET team_a = ?, team_b = ?, winner = ?, match_id = ?""",
                (slot, team_a, team_b, winner, match_id, team_a, team_b, winner, match_id)
            )
            await db.commit()


async def clear_bracket_slot(slot: str) -> None:
    """Clear a bracket slot."""
    if _use_pg:
        await _pg_execute("DELETE FROM bracket_slots WHERE slot = $1", slot)
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("DELETE FROM bracket_slots WHERE slot = ?", (slot,))
            await db.commit()


async def set_bracket_stream_channel(slot: str, stream_channel: int | None) -> None:
    """Set the stream channel for a bracket slot."""
    if _use_pg:
        await _pg_execute(
            "UPDATE bracket_slots SET stream_channel = $1 WHERE slot = $2",
            stream_channel, slot,
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE bracket_slots SET stream_channel = ? WHERE slot = ?",
                (stream_channel, slot),
            )
            await db.commit()


async def clear_all_bracket_slots() -> None:
    """Clear all bracket slots (reset bracket)."""
    if _use_pg:
        await _pg_execute("DELETE FROM bracket_slots")
        await _pg_execute("DELETE FROM bracket_claims")
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("DELETE FROM bracket_slots")
            await db.execute("DELETE FROM bracket_claims")
            await db.commit()


# ---- Bracket Claims ----


async def get_bracket_claims(slot: str) -> list[dict]:
    """Get all claims for a bracket slot."""
    if _use_pg:
        return await _pg_fetch(
            "SELECT * FROM bracket_claims WHERE slot = $1 ORDER BY role, slot_num", slot
        )
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM bracket_claims WHERE slot = ? ORDER BY role, slot_num", (slot,)
            )
            return [dict(r) for r in await cursor.fetchall()]


async def get_all_bracket_claims() -> dict[str, list[dict]]:
    """Get all bracket claims grouped by slot."""
    if _use_pg:
        rows = await _pg_fetch("SELECT * FROM bracket_claims ORDER BY slot, role, slot_num")
        result: dict[str, list[dict]] = {}
        for r in rows:
            result.setdefault(r["slot"], []).append(r)
        return result
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM bracket_claims ORDER BY slot, role, slot_num"
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            result: dict[str, list[dict]] = {}
            for r in rows:
                result.setdefault(r["slot"], []).append(r)
            return result


async def claim_bracket_slot(slot: str, user_id: int, display_name: str,
                             role: str, slot_num: int) -> int | None:
    """Claim a bracket slot. Returns previous holder user_id or None."""
    if _use_pg:
        previous = await _pg_fetchval(
            "SELECT user_id FROM bracket_claims WHERE slot = $1 AND role = $2 AND slot_num = $3",
            slot, role, slot_num,
        )
        await _pg_execute(
            "DELETE FROM bracket_claims WHERE slot = $1 AND role = $2 AND slot_num = $3",
            slot, role, slot_num,
        )
        await _pg_execute(
            "INSERT INTO bracket_claims (slot, user_id, display_name, role, slot_num) VALUES ($1, $2, $3, $4, $5)",
            slot, user_id, display_name, role, slot_num,
        )
        return previous
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM bracket_claims WHERE slot = ? AND role = ? AND slot_num = ?",
                (slot, role, slot_num),
            )
            row = await cursor.fetchone()
            previous = row[0] if row else None
            await conn.execute(
                "DELETE FROM bracket_claims WHERE slot = ? AND role = ? AND slot_num = ?",
                (slot, role, slot_num),
            )
            await conn.execute(
                "INSERT INTO bracket_claims (slot, user_id, display_name, role, slot_num) VALUES (?, ?, ?, ?, ?)",
                (slot, user_id, display_name, role, slot_num),
            )
            await conn.commit()
            return previous


async def unclaim_bracket_slot(slot: str, user_id: int, role: str, slot_num: int) -> bool:
    """Remove a bracket claim. Returns True if removed."""
    if _use_pg:
        result = await _pg_execute(
            "DELETE FROM bracket_claims WHERE slot = $1 AND user_id = $2 AND role = $3 AND slot_num = $4",
            slot, user_id, role, slot_num,
        )
        return "DELETE 0" not in result
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            cursor = await conn.execute(
                "DELETE FROM bracket_claims WHERE slot = ? AND user_id = ? AND role = ? AND slot_num = ?",
                (slot, user_id, role, slot_num),
            )
            await conn.commit()
            return cursor.rowcount > 0


async def unclaim_bracket_slot_admin(slot: str, role: str, slot_num: int) -> int | None:
    """Admin: remove a bracket claim regardless of user. Returns removed user_id."""
    if _use_pg:
        uid = await _pg_fetchval(
            "SELECT user_id FROM bracket_claims WHERE slot = $1 AND role = $2 AND slot_num = $3",
            slot, role, slot_num,
        )
        if uid is None:
            return None
        await _pg_execute(
            "DELETE FROM bracket_claims WHERE slot = $1 AND role = $2 AND slot_num = $3",
            slot, role, slot_num,
        )
        return uid
    else:
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM bracket_claims WHERE slot = ? AND role = ? AND slot_num = ?",
                (slot, role, slot_num),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            uid = row[0]
            await conn.execute(
                "DELETE FROM bracket_claims WHERE slot = ? AND role = ? AND slot_num = ?",
                (slot, role, slot_num),
            )
            await conn.commit()
            return uid
