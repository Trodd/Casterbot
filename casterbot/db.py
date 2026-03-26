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
    week_number TEXT,
    match_type TEXT,
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
        # Migration: add week_number column if missing
        if "week_number" not in columns:
            await db.execute("ALTER TABLE matches ADD COLUMN week_number TEXT")
        # Migration: add match_type column if missing
        if "match_type" not in columns:
            await db.execute("ALTER TABLE matches ADD COLUMN match_type TEXT")
        # Migration: add missing_since column if missing (for delayed deletion)
        if "missing_since" not in columns:
            await db.execute("ALTER TABLE matches ADD COLUMN missing_since INTEGER")
        # Migration: add stream_channel column if missing (for multi-stream support)
        if "stream_channel" not in columns:
            await db.execute("ALTER TABLE matches ADD COLUMN stream_channel INTEGER")
        
        # Migration: create profile_pictures table if missing
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profile_pictures (
                user_id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                uploaded_at INTEGER NOT NULL
            )
        """)
        
        # Migration: create team_logos table if missing
        await db.execute("""
            CREATE TABLE IF NOT EXISTS team_logos (
                team_name TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                discord_message_id INTEGER,
                approved_by INTEGER,
                approved_at INTEGER NOT NULL
            )
        """)
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
    match_type: str | None = None,
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
            # Update match_type if provided
            if match_type:
                await db.execute(
                    "UPDATE matches SET match_type = ? WHERE match_id = ?",
                    (match_type, match_id),
                )
            await db.commit()
            return False
        simple_id = await _next_simple_id()
        await db.execute(
            """
            INSERT INTO matches (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp, match_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, simple_id, team_a, team_b, match_date, match_time, match_timestamp, match_type),
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


async def set_stream_channel(match_id: str, stream_channel: int) -> None:
    """Set the stream channel (1 or 2) for a match."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET stream_channel = ? WHERE match_id = ?",
            (stream_channel, match_id),
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
    """Get matches that need claim messages posted (no message and no private channel yet)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM matches WHERE message_id IS NULL AND private_channel_id IS NULL"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_matches_with_message() -> list[dict]:
    """Get all matches that have a posted Discord message or active private channel."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM matches WHERE message_id IS NOT NULL OR private_channel_id IS NOT NULL"
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


async def mark_match_missing(match_id: str) -> None:
    """Mark a match as missing from the sheet (sets missing_since if not already set)."""
    import time
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Only set if not already marked as missing
        await db.execute(
            "UPDATE matches SET missing_since = ? WHERE match_id = ? AND missing_since IS NULL",
            (int(time.time()), match_id),
        )
        await db.commit()


async def clear_match_missing(match_id: str) -> None:
    """Clear the missing_since flag (match reappeared on sheet)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET missing_since = NULL WHERE match_id = ?",
            (match_id,),
        )
        await db.commit()


async def get_missing_since(match_id: str) -> int | None:
    """Get the timestamp when a match was first seen as missing."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT missing_since FROM matches WHERE match_id = ?", (match_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


# -- Caster Leaderboard Functions --


async def increment_cast_count(user_id: int) -> int:
    """Increment cast count for a user. Returns the new count."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Upsert: insert or update
        await db.execute(
            """
            INSERT INTO caster_stats (user_id, cast_count)
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET cast_count = cast_count + 1
            """,
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
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT cast_count FROM caster_stats WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def reset_leaderboard() -> int:
    """Reset all caster stats. Returns number of entries cleared."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM caster_stats")
        row = await cursor.fetchone()
        count = row[0] if row else 0
        await db.execute("DELETE FROM caster_stats")
        await db.commit()
        return count


async def set_cast_count(user_id: int, count: int) -> None:
    """Set a user's cast count to a specific value."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        if count <= 0:
            # Remove entry if count is 0 or negative
            await db.execute("DELETE FROM caster_stats WHERE user_id = ?", (user_id,))
        else:
            await db.execute(
                """
                INSERT INTO caster_stats (user_id, cast_count)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET cast_count = ?
                """,
                (user_id, count, count),
            )
        await db.commit()


# -- Leaderboard Cycle Functions --


async def archive_cycle(cycle_name: str, weeks: int = 0, start_date: str = "", end_date: str = "") -> int:
    """Archive current leaderboard to a new cycle and reset. Returns the cycle_id."""
    from datetime import datetime
    
    # Auto-set today's date as end date if not provided
    today = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = today
    if not start_date:
        start_date = today
    
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Create the cycle record
        cursor = await db.execute(
            """
            INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date)
            VALUES (?, ?, ?, ?)
            """,
            (cycle_name, weeks, start_date, end_date),
        )
        cycle_id = cursor.lastrowid
        
        # Copy all current stats to archive
        await db.execute(
            """
            INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
            SELECT ?, user_id, cast_count FROM caster_stats WHERE cast_count > 0
            """,
            (cycle_id,),
        )
        
        # Reset current leaderboard
        await db.execute("DELETE FROM caster_stats")
        
        # Clear active cycle
        await db.execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
        
        await db.commit()
        return cycle_id


async def start_cycle(cycle_name: str, weeks: int) -> dict:
    """Start a new active cycle. Archives current leaderboard first if there's data."""
    from datetime import datetime, timedelta
    
    today = datetime.now()
    start_date = today.strftime("%Y-%m-%d")
    end_date = (today + timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Check if there's existing stats to archive
        cursor = await db.execute("SELECT COUNT(*) FROM caster_stats WHERE cast_count > 0")
        row = await cursor.fetchone()
        has_stats = row[0] > 0
        
        # Check if there's an active cycle
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_name'")
        row = await cursor.fetchone()
        old_cycle_name = row[0] if row else None
        
        # Archive if there's data
        archived_id = None
        if has_stats and old_cycle_name:
            # Get old cycle dates
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_start'")
            row = await cursor.fetchone()
            old_start = row[0] if row else start_date
            
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'active_cycle_weeks'")
            row = await cursor.fetchone()
            old_weeks = int(row[0]) if row else weeks
            
            cursor = await db.execute(
                """
                INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date)
                VALUES (?, ?, ?, ?)
                """,
                (old_cycle_name, old_weeks, old_start, start_date),
            )
            archived_id = cursor.lastrowid
            
            await db.execute(
                """
                INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
                SELECT ?, user_id, cast_count FROM caster_stats WHERE cast_count > 0
                """,
                (archived_id,),
            )
            await db.execute("DELETE FROM caster_stats")
        
        # Set new active cycle
        await db.execute("DELETE FROM settings WHERE key LIKE 'active_cycle_%'")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_name', ?)", (cycle_name,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_weeks', ?)", (str(weeks),))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_start', ?)", (start_date,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_cycle_end', ?)", (end_date,))
        
        await db.commit()
        
        return {
            "name": cycle_name,
            "weeks": weeks,
            "start_date": start_date,
            "end_date": end_date,
            "archived_id": archived_id,
        }


async def get_active_cycle() -> dict | None:
    """Get the currently active cycle, or None."""
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
        
        return {
            "name": name,
            "weeks": weeks,
            "start_date": start_date,
            "end_date": end_date,
        }


async def end_active_cycle() -> int | None:
    """End the active cycle now and archive. Returns cycle_id or None."""
    from datetime import datetime
    
    active = await get_active_cycle()
    if not active:
        return None
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Archive
        cursor = await db.execute(
            """
            INSERT INTO leaderboard_cycles (cycle_name, weeks, start_date, end_date)
            VALUES (?, ?, ?, ?)
            """,
            (active["name"], active["weeks"], active["start_date"], today),
        )
        cycle_id = cursor.lastrowid
        
        await db.execute(
            """
            INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
            SELECT ?, user_id, cast_count FROM caster_stats WHERE cast_count > 0
            """,
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
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT cycle_id, cycle_name, weeks, start_date, end_date FROM leaderboard_cycles ORDER BY cycle_id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_cycle_leaderboard(cycle_id: int) -> list[dict]:
    """Get the leaderboard for a specific archived cycle."""
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
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = ?
            """,
            (key, value, value),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    """Get a setting value."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


# -- Web View Functions --


async def get_all_matches_sorted_by_time() -> list[dict]:
    """Get all matches with claims, sorted by match_timestamp (ascending - upcoming first)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM matches 
            WHERE message_id IS NOT NULL OR private_channel_id IS NOT NULL
            ORDER BY match_timestamp ASC NULLS LAST
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# -- Profile Picture Functions --


async def get_profile_picture(user_id: int) -> str | None:
    """Get the custom profile picture filename for a user, or None if not set."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT filename FROM profile_pictures WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_profile_picture(user_id: int, filename: str) -> None:
    """Set a custom profile picture for a user."""
    import time
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO profile_pictures (user_id, filename, uploaded_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET filename = ?, uploaded_at = ?
            """,
            (user_id, filename, int(time.time()), filename, int(time.time()))
        )
        await db.commit()


async def delete_profile_picture(user_id: int) -> str | None:
    """Delete a user's custom profile picture. Returns the old filename or None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT filename FROM profile_pictures WHERE user_id = ?",
            (user_id,)
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
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM team_logos WHERE team_name = ? COLLATE NOCASE",
            (team_name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_team_logos() -> list[dict]:
    """Get all approved team logos."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM team_logos ORDER BY team_name")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def set_team_logo(team_name: str, filename: str, discord_message_id: int, approved_by: int) -> None:
    """Set/update a team's approved logo."""
    import time
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO team_logos (team_name, filename, discord_message_id, approved_by, approved_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(team_name) DO UPDATE SET 
                filename = ?, discord_message_id = ?, approved_by = ?, approved_at = ?
            """,
            (team_name, filename, discord_message_id, approved_by, int(time.time()),
             filename, discord_message_id, approved_by, int(time.time()))
        )
        await db.commit()


async def delete_team_logo(team_name: str) -> str | None:
    """Delete a team's logo. Returns the old filename or None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT filename FROM team_logos WHERE team_name = ? COLLATE NOCASE",
            (team_name,)
        )
        row = await cursor.fetchone()
        if row:
            await db.execute("DELETE FROM team_logos WHERE team_name = ? COLLATE NOCASE", (team_name,))
            await db.commit()
            return row[0]
        return None


async def rename_team_logo(old_team_name: str, new_team_name: str) -> bool:
    """Rename a team's logo to a new team name. Returns True on success."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Check if old team has a logo
        cursor = await db.execute(
            "SELECT filename FROM team_logos WHERE team_name = ? COLLATE NOCASE",
            (old_team_name,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        
        # Delete any existing logo for the new team name
        await db.execute("DELETE FROM team_logos WHERE team_name = ? COLLATE NOCASE", (new_team_name,))
        
        # Rename the team
        await db.execute(
            "UPDATE team_logos SET team_name = ? WHERE team_name = ? COLLATE NOCASE",
            (new_team_name, old_team_name)
        )
        await db.commit()
        return True
