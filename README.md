# casterbot

Discord bot that posts upcoming match claim messages (caster/cam-op/sideline buttons) and creates a private match channel once staffing is filled. Includes a web interface for schedule viewing, claiming, and administration.

## Features

### Claim System

- **Caster 1 & 2**: Primary caster slots
- **Cam Op**: Camera operator slot
- **Sideline**: Sideline reporter slot
- **Unclaim**: Remove yourself from any claimed slots
- One-click claiming with role-based restrictions (optional)

### Web Interface

- **Broadcast Hub**: View all upcoming matches with claim status
- **Discord OAuth2 Login**: Claim/unclaim matches directly from the web
- **My Claims Filter**: Filter to show only matches you've claimed
- **Leaderboard Tab**: View cast counts with cycle/season history
- **Admin Panel**: Execute admin commands from the web (role-restricted)
- **PWA Support**: Install as an app on Android/iOS for quick access

### Private Match Channels

- Auto-creates a private channel for the crew when channel is created
- Visible only to claimed casters, cam ops, sideline, and staff
- Includes a **Close Channel** button (2-step confirmation, casters/staff only)
- Auto-generates and posts **transcripts** to a designated channel when closed
- Updates leaderboard counts when channel is closed

### Broadcast Controls

All broadcast control buttons have **2-step confirmation** to prevent accidental clicks:

- **Create Channel**: Creates the private match channel (requires at least 1 caster + 1 cam op)
- **Crew Ready**: Pings team roles in the private channel to let them know the crew is ready
- **Go Live**: Posts a live announcement to the designated channel with team mentions and live ping role

### Leaderboard

- Tracks cast counts for casters, cam ops, and sideline crew
- Only counts matches that go through the full workflow (channel closed properly)
- Cycle/season archiving with historical leaderboard viewing
- Admin commands to edit or reset counts

### Safety Features

- **5-minute delay**: Matches must be missing from the sheet for 5 minutes before being auto-deleted (prevents false positives from fetch failures)
- **Active channel protection**: Matches with an active private channel are never auto-deleted
- **Zero-match protection**: If sheet returns 0 matches but DB has existing matches, assumes fetch failure and skips all deletions

### Auto-Sync

- Fetches matches from a published Google Sheet CSV
- Configurable sync interval (default: 5 minutes)
- Configurable lookahead (default: 14 days)

## Setup

1. Create a Discord application + bot, invite it with permissions:
   - Manage Channels
   - Manage Roles (optional)
   - Read/Send Messages
   - Create Private Threads/Channels

2. For web OAuth2 login, add a redirect URI in the Discord Developer Portal:
   - `http://your-domain:8080/callback` (or your configured port)

3. Copy `.env.example` to `.env` and fill in IDs.

4. Create a virtual environment and install deps:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

1. Run:

```bash
python -m casterbot
```

The web interface will be available at `http://localhost:8080` (or your configured `WEB_PORT`).

## Commands

### Match Management

- `/sync_matches` — Manually sync upcoming matches from the sheet
- `/match_status <match_id>` — Show claim status for a match
- `/force_channel <match_id>` — Force create the private channel for a match (admin)
- `/refresh_messages` — Refresh all claim messages (updates UI)
- `/manage_claim <match_id> <action> <role> <slot> [user]` — Add or remove a user from a match slot (admin)

### Leaderboard

- `/leaderboard` — Show the caster leaderboard (includes cam ops and sideline)
- `/edit_leaderboard <user> <count>` — Edit a user's cast count (admin)
- `/reset_leaderboard` — Reset the entire caster leaderboard (admin)
- `/start_cycle <name> [weeks] [start_date] [end_date]` — Archive current leaderboard and start a new cycle
- `/view_cycles` — View all archived cycles
- `/view_cycle <cycle_id>` — View leaderboard for a specific archived cycle

### Settings

- `/set_week <season> <week>` — Set the current season and week number

### Fun

- `/margarita` — Request a margarita from the margarita machine 🍹

## Web Admin Panel

The admin panel (visible to users with `WEB_LEAD_ROLE_ID`) provides:

- **Sync Matches** — Pull latest from Google Sheet
- **Refresh Messages** — Update Discord claim embeds
- **Force Create Channel** — Create match channel bypassing requirements
- **Set Season/Week** — Update displayed season and week
- **Edit Cast Count** — Manually set a user's cast count
- **Reset Leaderboard** — Reset all counts to zero
- **Archive Season** — Archive current leaderboard and start new cycle

## Environment Variables

See `.env.example` for all available configuration options including:

- Discord bot token and guild ID
- Channel IDs for claims, announcements, and transcripts
- Role IDs for casters, cam ops, staff, and web admins
- Google Sheet URL for match data
- Web server port and OAuth2 credentials

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Bot token from Discord Developer Portal |
| `GUILD_ID` | Your Discord server ID |
| `CLAIM_CHANNEL_ID` | Channel where claim messages are posted |
| `PRIVATE_CATEGORY_ID` | Category for private match channels |
| `TRANSCRIPT_CHANNEL_ID` | Channel where transcripts are posted |
| `REQUIRE_CLAIM_ROLE` | Whether to restrict who can claim (true/false) |
| `CLAIM_ELIGIBLE_ROLE_ID` | Role ID required to claim (if enabled) |
| `CASTER_ROLE_ID` | Caster role for channel permissions |
| `CAMOP_ROLE_ID` | Cam op role for channel permissions |
| `CASTER_TRAINING_ROLE_ID` | Training caster role |
| `CAMOP_TRAINING_ROLE_ID` | Training cam op role |
| `STAFF_ROLE_ID` | Staff role for admin permissions |
| `LIVE_ANNOUNCEMENT_CHANNEL_ID` | Channel for Go Live announcements |
| `LIVE_PING_ROLE_ID` | Role to ping in Go Live announcements |
| `TWITCH_URL` | Twitch stream URL for announcements |
| `UPCOMING_MATCHES_CSV_URL` | Published Google Sheet CSV URL |
| `ROSTERS_CSV_URL` | Optional roster CSV URL |
| `MATCH_LOOKAHEAD_DAYS` | How many days ahead to fetch matches (default: 14) |
| `MATCH_GRACE_HOURS` | Grace period for past matches (default: 0) |
| `SYNC_INTERVAL_SECONDS` | How often to sync from sheet (default: 300) |
| `TIMEZONE` | Timezone for match times (default: US/Eastern) |

## Notes

- The upcoming matches source is a published Google Sheet CSV (File → Share → Publish to web → CSV)
- Team roles should be named `Team: TeamName` for auto-pinging to work
- Broadcast Controls require confirmation to prevent accidental triggers
- Leaderboard only increments when a channel is properly closed (not when matches are auto-deleted)
