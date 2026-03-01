# casterbot

Discord bot that posts upcoming match claim messages (caster/cam-op buttons) and creates a private match channel once staffing is filled.

## Setup

1. Create a Discord application + bot, invite it with permissions:
   - Manage Channels
   - Manage Roles (optional)
   - Read/Send Messages
   - Create Private Threads/Channels (channels)

2. Copy `.env.example` to `.env` and fill in IDs.

3. Create a virtual environment and install deps:

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

## Commands

- `/sync_matches` — fetch upcoming matches and post any new claim messages.

## Notes

- The upcoming matches source is a published Google Sheet CSV.
- If roster CSV is not publicly readable, the bot will still work but will post roster links instead of full player lists.
