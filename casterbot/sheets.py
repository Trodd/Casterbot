"""Fetch and parse upcoming matches from Google Sheets CSV."""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timedelta
from typing import NamedTuple

import aiohttp
from dateutil import parser as dateparser
from dateutil import tz

from . import config

log = logging.getLogger("casterbot")


class Match(NamedTuple):
    match_id: str
    match_type: str
    match_date: str
    match_time: str
    team_a: str
    team_b: str
    match_datetime: datetime


def _parse_datetime(date_str: str, time_str: str) -> datetime | None:
    """Parse date+time strings into a timezone-aware datetime."""
    try:
        combined = f"{date_str} {time_str}"
        dt = dateparser.parse(combined, fuzzy=True)
        if dt is None:
            return None
        local_tz = tz.gettz(config.TIMEZONE)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        return dt
    except Exception:
        return None


def _make_match_id(team_a: str, team_b: str, match_date: str, match_time: str) -> str:
    """Create a unique ID for a match (no external ID column available)."""
    slug = f"{team_a}_{team_b}_{match_date}_{match_time}"
    slug = re.sub(r"[^A-Za-z0-9_]", "", slug.replace(" ", "_").replace("/", ""))
    return slug[:80]


async def fetch_upcoming_matches() -> list[Match]:
    """Fetch upcoming (non-completed) matches from the published CSV."""
    matches: list[Match] = []
    now = datetime.now(tz.gettz(config.TIMEZONE))
    cutoff = now + timedelta(days=config.MATCH_LOOKAHEAD_DAYS)
    # Grace period: include matches that started recently (for Go Live / Ready buttons)
    grace_cutoff = now - timedelta(hours=config.MATCH_GRACE_HOURS)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                config.UPCOMING_MATCHES_CSV_URL,
                headers={"User-Agent": "CasterBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Sheet fetch failed with status {resp.status}")
                    return matches
                text = await resp.text()
        except Exception as e:
            log.warning(f"Sheet fetch failed: {e}")
            return matches

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return matches

    # Find header row (first row containing "Match Type" or "Team A")
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "match type" in row_lower or "team a" in row_lower:
            header_idx = i
            break

    header = [c.strip().lower() for c in rows[header_idx]]

    # Map columns
    def col(name: str) -> int:
        for i, h in enumerate(header):
            if name in h:
                return i
        return -1

    type_col = col("match type")
    date_col = col("match date")
    time_col = col("match time")
    team_a_col = col("team a")
    team_b_col = col("team b")

    # If (ET) in header, use that for time
    if time_col == -1:
        for i, h in enumerate(header):
            if "time" in h:
                time_col = i
                break

    for row in rows[header_idx + 1 :]:
        if len(row) <= max(team_a_col, team_b_col, date_col, time_col):
            continue
        match_type = row[type_col].strip() if type_col >= 0 else ""
        match_date = row[date_col].strip() if date_col >= 0 else ""
        match_time = row[time_col].strip() if time_col >= 0 else ""
        team_a = row[team_a_col].strip() if team_a_col >= 0 else ""
        team_b = row[team_b_col].strip() if team_b_col >= 0 else ""

        if not team_a or not team_b or not match_date:
            continue

        dt = _parse_datetime(match_date, match_time)
        if dt is None:
            continue

        # Include matches within grace period (after start) up to lookahead window (before start)
        if dt < grace_cutoff or dt > cutoff:
            continue

        match_id = _make_match_id(team_a, team_b, match_date, match_time)
        # Skip duplicates (same teams and time)
        if any(m.match_id == match_id for m in matches):
            log.debug(f"Skipping duplicate match: {team_a} vs {team_b} at {match_date} {match_time}")
            continue
        matches.append(
            Match(
                match_id=match_id,
                match_type=match_type,
                match_date=match_date,
                match_time=match_time,
                team_a=team_a,
                team_b=team_b,
                match_datetime=dt,
            )
        )

    # Sort by datetime
    matches.sort(key=lambda m: m.match_datetime)
    return matches
