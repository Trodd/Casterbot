"""Fetch and parse upcoming matches from Google Sheets CSV."""
from __future__ import annotations  # AGENTS-AUDIT: banned by AGENTS for active Python 3.14-oriented code.

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

# === MAINTAINABILITY / AGENTS AUDIT ANNOTATIONS ===
# AGENTS violation: uses `from __future__ import annotations` in an active module.
# AGENTS violation: broad `except Exception` handlers do not re-raise, masking root causes.
# AGENTS violation: external/untrusted CSV payload is parsed without pydantic boundary validation.
# Code smell: global mutable ranking caches are process-local and unsynchronized across workers.
# Code smell: deduplication uses O(n^2) pattern (`any(...)` over growing list) on match collection.
# Code smell: logger messages use f-strings, limiting structured log key/value querying.
# AUDIT COUNTS: format gate failed for this file; ruff findings=0; pyright findings=0.
# AUDIT COUNTS: source scan found future_imports=1, broad_except=3, untyped_defs=0, dict_shapes=4.
# AUDIT SCOPE: every external CSV parse path in this file lacks pydantic boundary validation.

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
    except Exception:  # AGENTS-AUDIT: broad parse failure is swallowed without structured error propagation.
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


# ---- Rankings cache ----
_rankings: dict[str, str] = {}  # team name (lower) -> rank string
_ranked_teams_ordered: list[tuple[str, str]] = []  # ordered list of (display_name, rank)


def get_team_rank(team_name: str) -> str:
    """Return the rank string for a team, or empty string if unknown."""
    return _rankings.get(team_name.strip().lower(), "")


def get_top_teams() -> list[tuple[str, str]]:
    """Return finals-eligible teams: all Master teams + top 3 Diamond teams, in ranking order."""
    masters = [(name, rank) for name, rank in _ranked_teams_ordered if _parse_rank(rank)[0] == "master"]
    diamonds = [(name, rank) for name, rank in _ranked_teams_ordered if _parse_rank(rank)[0] == "diamond"]
    return masters + diamonds[:3]


def get_all_teams() -> list[tuple[str, str]]:
    """Return all ranked teams in ranking order."""
    return list(_ranked_teams_ordered)


# Tier -> (emoji for Discord, CSS color hex for web)
_RANK_TIERS: dict[str, tuple[str, str]] = {
    "master":   ("👑", "#a855f7"),  # purple
    "diamond":  ("💎", "#38bdf8"),  # blue
    "platinum": ("💠", "#94a3b8"),  # silver
    "gold":     ("🟡", "#facc15"),  # gold
    "silver":   ("⚪", "#cbd5e1"),  # light gray
    "bronze":   ("🟤", "#d97706"),  # amber
}


def _parse_rank(rank: str) -> tuple[str, str]:
    """Split 'Diamond 4' into ('diamond', '4'). Returns (tier_lower, number_or_empty)."""
    parts = rank.rsplit(" ", 1)
    tier = parts[0].lower()
    num = parts[1] if len(parts) == 2 and parts[1].isdigit() else ""
    return tier, num


def rank_emoji(team_name: str) -> str:
    """Return a Discord-friendly emoji string for the team's rank, e.g. '💎4'."""
    rank = get_team_rank(team_name)
    if not rank:
        return ""
    tier, num = _parse_rank(rank)
    emoji = _RANK_TIERS.get(tier, ("🔘", "#888"))[0]
    return f"{emoji}{num}"


def rank_html(team_name: str) -> str:
    """Return an HTML snippet with a colored rank symbol for the web UI."""
    rank = get_team_rank(team_name)
    if not rank:
        return ""
    tier, num = _parse_rank(rank)
    emoji, color = _RANK_TIERS.get(tier, ("●", "#888"))
    return f'<span class="team-rank" style="color:{color}" title="{rank}">{emoji}{num}</span>'


async def fetch_rankings() -> dict[str, str]:
    """Fetch team rankings from the published CSV and update the cache."""
    global _rankings, _ranked_teams_ordered
    if not config.RANKINGS_CSV_URL:
        return _rankings

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                config.RANKINGS_CSV_URL,
                headers={"User-Agent": "CasterBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Rankings fetch failed with status {resp.status}")
                    return _rankings
                text = await resp.text()
        except Exception as e:
            log.warning(f"Rankings fetch failed: {e}")
            return _rankings

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return _rankings

    # Find header row (first row containing "Team" or "Rank")
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.strip().lower() for c in row]
        if any("team" in c or "rank" in c for c in row_lower):
            header_idx = i
            break

    header = [c.strip().lower() for c in rows[header_idx]]

    # Flexible column helper
    def col(name: str) -> int:
        for i, h in enumerate(header):
            if name in h:
                return i
        return -1

    name_col = col("team")
    rank_col = col("rank")

    if name_col == -1 or rank_col == -1:
        log.warning(f"Rankings CSV missing expected columns (found: {header})")
        return _rankings

    new_rankings: dict[str, str] = {}
    new_ordered: list[tuple[str, str]] = []
    for row in rows[header_idx + 1 :]:
        if len(row) <= max(name_col, rank_col):
            continue
        team = row[name_col].strip()
        rank = row[rank_col].strip()
        if team and rank:
            new_rankings[team.lower()] = rank
            new_ordered.append((team, rank))

    # Guard: don't overwrite a large cache with a tiny result (likely fetch failure)
    if len(new_rankings) < 5 and len(_rankings) > len(new_rankings):
        log.warning(f"Rankings fetch returned only {len(new_rankings)} teams, keeping existing {len(_rankings)}")
        return _rankings

    _rankings = new_rankings
    _ranked_teams_ordered = new_ordered
    log.info(f"Loaded {len(_rankings)} team rankings")
    return _rankings


# ---- Rosters cache ----
_rosters: dict[str, dict] = {}  # team name (lower) -> {status, players: [{name, role}], roster_count}


def get_team_roster(team_name: str) -> dict | None:
    """Return cached roster data for a team, or None."""
    return _rosters.get(team_name.strip().lower())


def get_all_rosters() -> dict[str, dict]:
    """Return all cached roster data."""
    return dict(_rosters)


def get_roster_count(team_name: str) -> int:
    """Return roster player count for a team from CSV data."""
    roster = _rosters.get(team_name.strip().lower())
    if roster:
        return roster.get("roster_count", 0)
    return 0


async def fetch_rosters() -> dict[str, dict]:
    """Fetch team rosters from the published roster CSV and update the cache."""
    global _rosters
    if not config.ROSTERS_CSV_URL:
        return _rosters

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                config.ROSTERS_CSV_URL,
                headers={"User-Agent": "CasterBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Rosters fetch failed with status {resp.status}")
                    return _rosters
                text = await resp.text()
        except Exception as e:
            log.warning(f"Rosters fetch failed: {e}")
            return _rosters

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return _rosters

    # Find header row (first row containing "Team" or "Status")
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.strip().lower() for c in row]
        if any("team" in c or "status" in c for c in row_lower):
            header_idx = i
            break

    header = [c.strip().lower() for c in rows[header_idx]]

    # Flexible column helper
    def col(name: str) -> int:
        for i, h in enumerate(header):
            if name in h:
                return i
        return -1

    team_col = col("team")
    status_col = col("status")

    if team_col == -1:
        log.warning(f"Rosters CSV missing 'Team' column (found: {header})")
        return _rosters

    new_rosters: dict[str, dict] = {}
    for row in rows[header_idx + 1 :]:
        if len(row) <= team_col:
            continue
        team = row[team_col].strip()
        if not team:
            continue

        status = row[status_col].strip() if status_col >= 0 and status_col < len(row) else ""
        players: list[dict] = []
        # Columns after Team: players (skip Status column)
        for i in range(team_col + 1, len(row)):
            if i == status_col:
                continue
            name = row[i].strip()
            if not name:
                continue
            # Detect role from name prefix like "(CC)" or "Manager"
            role = "Player"
            cleaned = name
            if name.startswith("(CC)"):
                role = "Co-Captain"
                cleaned = name[4:].strip()
            elif name.lower().endswith("-manager"):
                role = "Manager"
            elif cleaned.lower() == "captain":
                role = "Captain"
            # First player column after Team = Captain
            if not players:
                role = "Captain"
            players.append({"name": cleaned, "role": role})

        new_rosters[team.lower()] = {
            "team_name": team,
            "status": status,
            "players": players,
            "roster_count": len(players),
        }

    # Guard: don't overwrite a large cache with a tiny result (likely fetch failure)
    if len(new_rosters) < 5 and len(_rosters) > len(new_rosters):
        log.warning(f"Rosters fetch returned only {len(new_rosters)} teams, keeping existing {len(_rosters)}")
        return _rosters

    _rosters = new_rosters
    log.info(f"Loaded rosters for {len(_rosters)} teams")
    return _rosters


# ---- Cooldown list cache ----
_cooldowns: set[str] = set()  # normalized (lowercased) player names on cooldown
_cooldown_ids: set[int] = set()  # resolved Discord IDs of players on cooldown


def is_player_on_cooldown(name: str) -> bool:
    """Return True if the given player name appears on the cooldown list."""
    return is_on_cooldown(name=name)


def is_on_cooldown(name: str | None = None, discord_id: int | None = None) -> bool:
    """Return True if a player is on cooldown, matching by Discord ID and/or name."""
    if discord_id is not None and discord_id in _cooldown_ids:
        return True
    if name:
        normalized = name.strip().lstrip("@").lower()
        if normalized in _cooldowns:
            return True
        resolved = resolve_discord_id(normalized)
        if resolved is not None and resolved in _cooldown_ids:
            return True
    return False


def get_cooldown_names() -> set[str]:
    """Return a copy of all cooldown player names (lowercased)."""
    return set(_cooldowns)


async def fetch_cooldowns() -> set[str]:
    """Fetch the cooldown list from the published CSV and update the cache."""
    global _cooldowns, _cooldown_ids
    if not config.COOLDOWN_CSV_URL:
        return _cooldowns

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                config.COOLDOWN_CSV_URL,
                headers={"User-Agent": "CasterBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Cooldowns fetch failed with status {resp.status}")
                    return _cooldowns
                text = await resp.text()
        except Exception as e:
            log.warning(f"Cooldowns fetch failed: {e}")
            return _cooldowns

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return _cooldowns

    # Find the header row (first row that mentions names/players/cooldown)
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.strip().lower() for c in row]
        if any("name" in c or "player" in c or "cooldown" in c for c in row_lower):
            header_idx = i
            break

    header = [c.strip().lower() for c in rows[header_idx]]

    def col(name: str) -> int:
        for i, h in enumerate(header):
            if name in h:
                return i
        return -1

    player_col = col("player")
    if player_col == -1:
        player_col = col("name")
    if player_col == -1:
        player_col = 0  # Fall back to the first column

    new_cooldowns: set[str] = set()
    for row in rows[header_idx + 1 :]:
        if player_col >= len(row):
            continue
        name = row[player_col].strip()
        if not name:
            continue
        new_cooldowns.add(name.lstrip("@").lower())

    new_cooldown_ids: set[int] = set()
    for name in new_cooldowns:
        resolved = _player_history.get(name.lower())
        if resolved is not None:
            new_cooldown_ids.add(resolved)

    _cooldowns = new_cooldowns
    _cooldown_ids = new_cooldown_ids
    log.info(f"Loaded {len(_cooldowns)} cooldown players ({len(_cooldown_ids)} matched to Discord IDs)")
    return _cooldowns


# ---- Team roles cache (long-format sheet: Team Name, Player Name, Captain, Co-Captain, Region) ----
_team_roles: dict[str, list[dict]] = {}  # team name (lower) -> [{name, role, region}]


def get_team_players(team_name: str) -> list[dict]:
    """Return the roster players for a team from the team roles sheet."""
    return list(_team_roles.get(team_name.strip().lower(), []))


def get_all_team_roles() -> dict[str, list[dict]]:
    """Return all team rosters from the team roles sheet."""
    return {team: list(players) for team, players in _team_roles.items()}


async def fetch_team_roles() -> dict[str, list[dict]]:
    """Fetch the team roles sheet (long format) and update the cache."""
    global _team_roles
    if not config.TEAM_ROLES_CSV_URL:
        return _team_roles

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                config.TEAM_ROLES_CSV_URL,
                headers={"User-Agent": "CasterBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Team roles fetch failed with status {resp.status}")
                    return _team_roles
                text = await resp.text()
        except Exception as e:
            log.warning(f"Team roles fetch failed: {e}")
            return _team_roles

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return _team_roles

    # Find the header row (must mention a team or player column)
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.strip().lower() for c in row]
        if any("team" in c or "player" in c for c in row_lower):
            header_idx = i
            break

    header = [c.strip().lower() for c in rows[header_idx]]

    def col(name: str) -> int:
        for i, h in enumerate(header):
            if name in h:
                return i
        return -1

    team_col = col("team")
    player_col = col("player")
    if team_col == -1 or player_col == -1:
        log.warning(f"Team roles CSV missing Team/Player columns (found: {header})")
        return _team_roles

    # Captain and Co-Captain are Yes/No flags. Match exact headers first so
    # "co-captain" isn't picked up by a loose "captain" search.
    captain_col = -1
    cocaptain_col = -1
    for i, h in enumerate(header):
        if h == "captain":
            captain_col = i
        elif h in ("co-captain", "co captain", "cocaptain", "co_captain"):
            cocaptain_col = i
    if captain_col == -1:
        for i, h in enumerate(header):
            if "captain" in h and "co" not in h:
                captain_col = i
                break

    region_col = col("region")

    new_roles: dict[str, list[dict]] = {}
    for row in rows[header_idx + 1 :]:
        if len(row) <= max(team_col, player_col):
            continue
        team = row[team_col].strip()
        player = row[player_col].strip()
        if not team or not player:
            continue
        is_captain = captain_col >= 0 and captain_col < len(row) and row[captain_col].strip().lower() in ("yes", "y", "true", "1")
        is_cocaptain = cocaptain_col >= 0 and cocaptain_col < len(row) and row[cocaptain_col].strip().lower() in ("yes", "y", "true", "1")
        role = "Captain" if is_captain else ("Co-Captain" if is_cocaptain else "Player")
        region = row[region_col].strip() if region_col >= 0 and region_col < len(row) else ""
        new_roles.setdefault(team.lower(), []).append({"name": player, "role": role, "region": region})

    # Guard: don't wipe a larger cache with a tiny result (likely fetch failure)
    if len(new_roles) < 5 and len(_team_roles) > len(new_roles):
        log.warning(f"Team roles fetch returned only {len(new_roles)} teams, keeping existing {len(_team_roles)}")
        return _team_roles

    _team_roles = new_roles
    log.info(f"Loaded team roles for {len(_team_roles)} teams")
    return _team_roles


# ---- Player name history cache ----
# Maps every known name (original + alts) to a player's Discord ID so players can
# be identified across sheets even when names don't match Discord exactly.
_player_history: dict[str, int] = {}  # normalized name -> Discord ID
_player_ids: set[int] = set()  # all known Discord IDs


def resolve_discord_id(name: str) -> int | None:
    """Resolve a player name (original or alias) to their Discord ID."""
    if not name:
        return None
    return _player_history.get(name.strip().lstrip("@").lower())


def get_known_player_ids() -> set[int]:
    """Return a copy of all known player Discord IDs."""
    return set(_player_ids)


async def fetch_player_history() -> dict[str, int]:
    """Fetch the player name history sheet and update the cache."""
    global _player_history, _player_ids
    if not config.PLAYER_HISTORY_CSV_URL:
        return _player_history

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                config.PLAYER_HISTORY_CSV_URL,
                headers={"User-Agent": "CasterBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Player history fetch failed with status {resp.status}")
                    return _player_history
                text = await resp.text()
        except Exception as e:
            log.warning(f"Player history fetch failed: {e}")
            return _player_history

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return _player_history

    # Find the header row (must mention Discord and a name column)
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.strip().lower() for c in row]
        if any("discord" in c or "player" in c or "name" in c for c in row_lower):
            header_idx = i
            break

    header = [c.strip().lower() for c in rows[header_idx]]

    def col(name: str) -> int:
        for i, h in enumerate(header):
            if name in h:
                return i
        return -1

    discord_col = col("discord")
    if discord_col == -1:
        discord_col = col("id")
    name_col = col("player")
    if name_col == -1:
        name_col = col("name")

    if discord_col == -1 or name_col == -1:
        log.warning(f"Player history CSV missing Discord ID/Player Name columns (found: {header})")
        return _player_history

    new_history: dict[str, int] = {}
    new_ids: set[int] = set()
    for row in rows[header_idx + 1 :]:
        if len(row) <= discord_col:
            continue
        discord_id_str = row[discord_col].strip()
        if not discord_id_str.isdigit():
            continue
        discord_id = int(discord_id_str)
        names: list[str] = []
        for i in range(name_col, len(row)):
            cell = row[i].strip()
            if cell:
                names.append(cell)
        if not names:
            continue
        new_ids.add(discord_id)
        for name in names:
            new_history[name.lstrip("@").lower()] = discord_id

    # Guard against wiping a larger cache with a tiny result (likely fetch failure)
    if len(new_history) < 5 and len(_player_history) > len(new_history):
        log.warning(f"Player history fetch returned only {len(new_history)} names, keeping existing {len(_player_history)}")
        return _player_history

    _player_history = new_history
    _player_ids = new_ids
    log.info(f"Loaded {len(_player_history)} player name aliases for {len(_player_ids)} players")
    return _player_history
