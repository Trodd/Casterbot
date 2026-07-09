"""Migrate local SQLite data to Render PostgreSQL.

Usage:
    python migrate_to_render.py <RENDER_DATABASE_URL>

Example:
    python migrate_to_render.py "postgresql://casterbot:PASSWORD@HOST/casterbot"

You can find your DATABASE_URL in the Render dashboard under your database's "Info" tab.
"""
from __future__ import annotations  # AGENTS-AUDIT: banned by AGENTS for active Python 3.14-oriented code.

import asyncio
import sqlite3
import sys

import asyncpg

# === MAINTAINABILITY / AGENTS AUDIT ANNOTATIONS ===
# AGENTS violation: uses `from __future__ import annotations` in a new/active module.
# AGENTS violation: heavy use of print for diagnostics instead of structured logging.
# AGENTS violation: broad exception handling patterns skip explicit re-raise in migration branches.
# Code smell: one very large function mixes schema management, extraction, transformation, and loading.
# Code smell: synchronous sqlite3 operations are embedded in async flow; this can block the event loop.
# Code smell: SQL schema is duplicated here and in db layer, creating schema drift risk.
# Code smell: per-row inserts without batched transaction strategy may degrade migration performance.
# AUDIT COUNTS: format gate failed for this file; ruff findings=1; pyright findings=0.
# AUDIT COUNTS: source scan found future_imports=1, print_calls=29, untyped_defs=0, dict_shapes=0.
# AUDIT SCOPE: every `print(...)` call in this file is part of the AGENTS diagnostics violation class.


DB_PATH = "casterbot.db"


async def migrate(database_url: str) -> None:
    print(f"Connecting to PostgreSQL...")  # AGENTS-AUDIT: print diagnostics violate logging/structlog rule.
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

    print(f"Reading local SQLite database: {DB_PATH}")
    conn_sqlite = sqlite3.connect(DB_PATH)
    conn_sqlite.row_factory = sqlite3.Row

    async with pool.acquire() as pg:
        # Create tables first
        await pg.execute("""
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
        """)
        print("Tables created.")

        # --- Migrate matches ---
        rows = conn_sqlite.execute("SELECT * FROM matches").fetchall()
        if rows:
            print(f"  Migrating {len(rows)} matches...")
            for row in rows:
                await pg.execute("""
                    INSERT INTO matches (match_id, simple_id, team_a, team_b, match_date, match_time,
                                         match_timestamp, week_number, match_type, message_id,
                                         channel_id, private_channel_id, missing_since, stream_channel)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT (match_id) DO NOTHING
                """,
                    row["match_id"], row["simple_id"], row["team_a"], row["team_b"],
                    row["match_date"], row["match_time"], row["match_timestamp"],
                    row["week_number"], row["match_type"], row["message_id"],
                    row["channel_id"], row["private_channel_id"],
                    row["missing_since"] if "missing_since" in row.keys() else None,
                    row["stream_channel"] if "stream_channel" in row.keys() else None,
                )
        else:
            print("  No matches to migrate.")

        # --- Migrate claims ---
        rows = conn_sqlite.execute("SELECT * FROM claims").fetchall()
        if rows:
            print(f"  Migrating {len(rows)} claims...")
            skipped = 0
            for row in rows:
                try:
                    await pg.execute("""
                        INSERT INTO claims (match_id, user_id, role, slot)
                        VALUES ($1,$2,$3,$4)
                        ON CONFLICT (match_id, role, slot) DO NOTHING
                    """, row["match_id"], row["user_id"], row["role"], row["slot"])
                except asyncpg.exceptions.ForeignKeyViolationError:
                    skipped += 1
            if skipped:
                print(f"    (skipped {skipped} orphaned claims)")
        else:
            print("  No claims to migrate.")

        # --- Migrate caster_stats ---
        rows = conn_sqlite.execute("SELECT * FROM caster_stats").fetchall()
        if rows:
            print(f"  Migrating {len(rows)} caster stats...")
            for row in rows:
                await pg.execute("""
                    INSERT INTO caster_stats (user_id, cast_count)
                    VALUES ($1,$2)
                    ON CONFLICT (user_id) DO UPDATE SET cast_count = $2
                """, row["user_id"], row["cast_count"])
        else:
            print("  No caster stats to migrate.")

        # --- Migrate leaderboard_cycles ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM leaderboard_cycles").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} leaderboard cycles...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO leaderboard_cycles (cycle_id, cycle_name, weeks, start_date, end_date)
                        VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (cycle_id) DO NOTHING
                    """, row["cycle_id"], row["cycle_name"], row["weeks"],
                        row["start_date"], row["end_date"])
                # Reset sequence
                await pg.execute("SELECT setval('leaderboard_cycles_cycle_id_seq', (SELECT COALESCE(MAX(cycle_id),0) FROM leaderboard_cycles))")
        except sqlite3.OperationalError:
            print("  No leaderboard_cycles table in SQLite (skipping).")

        # --- Migrate leaderboard_archive ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM leaderboard_archive").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} leaderboard archive entries...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO leaderboard_archive (cycle_id, user_id, cast_count)
                        VALUES ($1,$2,$3)
                    """, row["cycle_id"], row["user_id"], row["cast_count"])
        except sqlite3.OperationalError:
            print("  No leaderboard_archive table in SQLite (skipping).")

        # --- Migrate settings ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM settings").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} settings...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO settings (key, value)
                        VALUES ($1,$2)
                        ON CONFLICT (key) DO UPDATE SET value = $2
                    """, row["key"], row["value"])
        except sqlite3.OperationalError:
            print("  No settings table in SQLite (skipping).")

        # --- Migrate profile_pictures ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM profile_pictures").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} profile pictures...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO profile_pictures (user_id, filename, uploaded_at)
                        VALUES ($1,$2,$3)
                        ON CONFLICT (user_id) DO NOTHING
                    """, row["user_id"], row["filename"], row["uploaded_at"])
        except sqlite3.OperationalError:
            print("  No profile_pictures table in SQLite (skipping).")

        # --- Migrate team_logos ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM team_logos").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} team logos...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO team_logos (team_name, filename, discord_message_id, approved_by, approved_at)
                        VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (team_name) DO NOTHING
                    """, row["team_name"], row["filename"], row["discord_message_id"],
                        row["approved_by"], row["approved_at"])
        except sqlite3.OperationalError:
            print("  No team_logos table in SQLite (skipping).")

        # --- Migrate bracket_slots ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM bracket_slots").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} bracket slots...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO bracket_slots (slot, team_a, team_b, score_a, score_b, winner, match_id, stream_channel)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT (slot) DO NOTHING
                    """, row["slot"], row["team_a"], row["team_b"],
                        row["score_a"], row["score_b"], row["winner"],
                        row["match_id"], row["stream_channel"] if "stream_channel" in row.keys() else None)
        except sqlite3.OperationalError:
            print("  No bracket_slots table in SQLite (skipping).")

        # --- Migrate bracket_claims ---
        try:
            rows = conn_sqlite.execute("SELECT * FROM bracket_claims").fetchall()
            if rows:
                print(f"  Migrating {len(rows)} bracket claims...")
                for row in rows:
                    await pg.execute("""
                        INSERT INTO bracket_claims (slot, user_id, display_name, role, slot_num)
                        VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (slot, role, slot_num) DO NOTHING
                    """, row["slot"], row["user_id"], row["display_name"],
                        row["role"], row["slot_num"])
        except sqlite3.OperationalError:
            print("  No bracket_claims table in SQLite (skipping).")

    conn_sqlite.close()
    await pool.close()
    print("\n✓ Migration complete! All data transferred to Render PostgreSQL.")
    print("\nNOTE: For profile pics and team logos FILES, you'll need to upload them")
    print("to the Render disk via SSH or use 'render ssh' to scp them to /data/")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_to_render.py <DATABASE_URL>")
        print("\nFind your DATABASE_URL in Render dashboard → your database → Info → External Database URL")
        sys.exit(1)

    database_url = sys.argv[1]
    asyncio.run(migrate(database_url))
