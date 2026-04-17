# Bracket RPC Integration Guide

## Overview

The EML CasterBot exposes a read-only RPC endpoint for the finals double-elimination bracket. This returns the full bracket state including teams, scores, winners, crew assignments, stream channels, and linked match info.

There is also a public bracket page at `/bracket` (no auth required) that auto-refreshes every 10 seconds.

---

## Authentication

All RPC requests require an API key sent via header:

```
X-API-Key: <your-api-key>
```

Alternatively, pass it as a query parameter: `?api_key=<your-api-key>`

---

## Endpoint

### `GET /rpc/bracket`

Returns the full bracket state for all 14 slots.

**Response:**

```json
{
  "success": true,
  "bracket": {
    "WQF1": { ... },
    "WQF2": { ... },
    ...
    "GF": { ... },
    "GFR": { ... }
  }
}
```

---

## Slot Reference

| Key    | Label             | Round           |
|--------|-------------------|-----------------|
| WQF1   | WQF 1             | Winners Quarter |
| WQF2   | WQF 2             | Winners Quarter |
| WQF3   | WQF 3             | Winners Quarter |
| WQF4   | WQF 4             | Winners Quarter |
| WSF1   | WSF 1             | Winners Semi    |
| WSF2   | WSF 2             | Winners Semi    |
| WF     | Winners Final     | Winners Final   |
| LR1_1  | LR1-1             | Losers Round 1  |
| LR1_2  | LR1-2             | Losers Round 1  |
| LR2_1  | LR2-1             | Losers Round 2  |
| LR2_2  | LR2-2             | Losers Round 2  |
| LSF    | Losers Semi       | Losers Semi     |
| LF     | Losers Final      | Losers Final    |
| GF     | Grand Final       | Grand Final     |
| GFR    | Grand Final Reset | Grand Final     |

---

## Slot Object Fields

Each slot in the `bracket` object has this shape:

| Field            | Type          | Description                                         |
|------------------|---------------|-----------------------------------------------------|
| `slot`           | string        | Slot key (e.g., `"WQF1"`, `"GF"`)                  |
| `label`          | string        | Display label (e.g., `"WQF 1"`, `"Grand Final"`)   |
| `team_a`         | string\|null  | Team A name, or `null` if TBD                       |
| `team_b`         | string\|null  | Team B name, or `null` if TBD                       |
| `winner`         | string\|null  | Winning team name, or `null` if not decided          |
| `match_id`       | string\|null  | Internal match key if a match has been created       |
| `stream_channel` | int\|null     | Stream channel number (`1` or `2`), or `null`       |
| `team_a_logo`    | string\|null  | Full URL to team A's logo, or `null`                |
| `team_b_logo`    | string\|null  | Full URL to team B's logo, or `null`                |
| `match`          | object\|null  | Linked match details (see below), or `null`         |
| `crew`           | array         | List of crew claim objects (see below)               |

### `match` Object

Present when a match has been created for this bracket slot.

| Field            | Type    | Description                              |
|------------------|---------|------------------------------------------|
| `id`             | int     | Simple numeric match ID                  |
| `match_id`       | string  | Full match key                           |
| `has_channel`    | bool    | Whether a private Discord channel exists |
| `stream_channel` | int\|null | Stream channel on the match            |

### `crew` Array Items

Each item in the `crew` array:

| Field          | Type   | Description                                 |
|----------------|--------|---------------------------------------------|
| `role`         | string | `"caster"`, `"camop"`, or `"sideline"`      |
| `slot_num`     | int    | Slot number within the role (1-based)        |
| `user_id`      | string | Discord user ID                              |
| `display_name` | string | Display name (server nickname if available)  |
| `avatar_url`   | string | URL to user's avatar (optional, may be absent) |

**Crew layout per bracket slot:**

| Role     | Slots | Description        |
|----------|-------|--------------------|
| caster   | 1, 2  | Primary casters    |
| camop    | 1     | Camera operator    |
| sideline | 1     | Sideline reporter  |

---

## Example Response (single slot)

```json
{
  "WQF1": {
    "slot": "WQF1",
    "label": "WQF 1",
    "team_a": "Nocturne",
    "team_b": "Valiants",
    "winner": null,
    "match_id": "finals_abc123def456",
    "stream_channel": 1,
    "team_a_logo": "https://your-domain.com/team-logo/Nocturne",
    "team_b_logo": "https://your-domain.com/team-logo/Valiants",
    "match": {
      "id": 42,
      "match_id": "finals_abc123def456",
      "has_channel": true,
      "stream_channel": 1
    },
    "crew": [
      {
        "role": "caster",
        "slot_num": 1,
        "user_id": "123456789",
        "display_name": "TroddCaster",
        "avatar_url": "https://cdn.discordapp.com/avatars/..."
      },
      {
        "role": "camop",
        "slot_num": 1,
        "user_id": "987654321",
        "display_name": "CamGuy"
      }
    ]
  }
}
```

---

## Python Example

```python
import eml_client

eml_client.configure("https://your-domain.com", "your-api-key")

bracket = eml_client.get_bracket()

for slot_key, data in bracket.items():
    team_a = data["team_a"] or "TBD"
    team_b = data["team_b"] or "TBD"
    ch = f"CH{data['stream_channel']}" if data["stream_channel"] else "—"
    crew_count = len(data["crew"])
    print(f"{data['label']:20s} {team_a:15s} vs {team_b:15s}  {ch}  crew: {crew_count}/4")
```

## JavaScript / fetch Example

```javascript
const resp = await fetch("https://your-domain.com/rpc/bracket", {
  headers: { "X-API-Key": "your-api-key" }
});
const { bracket } = await resp.json();

for (const [slot, data] of Object.entries(bracket)) {
  const a = data.team_a ?? "TBD";
  const b = data.team_b ?? "TBD";
  const ch = data.stream_channel ? `CH${data.stream_channel}` : "—";
  console.log(`${data.label}: ${a} vs ${b} [${ch}] crew: ${data.crew.length}/4`);
}
```

---

## Public Bracket Page

A standalone, read-only bracket page is available at:

```
https://your-domain.com/bracket
```

- No authentication required
- Auto-refreshes every 10 seconds
- Renders the full double-elimination bracket as SVG
- Shows team names, winners, stream channel indicators, and crew fill status
- Mobile responsive
