# CasterBot RPC Integration Guide

For connecting an external site to CasterBot — broadcast control, match data, logos, SSO, and monitoring.

**Base URL:** `https://casterbot.onrender.com`

---

## Authentication

All RPC endpoints require an API key. Send it as an HTTP header:

```
X-API-Key: your-shared-key
```

You may also pass it as a query parameter: `?api_key=your-shared-key` (useful for GET requests from browser links).

Ask Todd for the actual key value.

---

## Quick Reference — All Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/rpc/create_channel` | Create private Discord channel for a match |
| POST | `/rpc/crew_ready` | Send "crew ready" ping to teams |
| POST | `/rpc/go_live` | Post live announcement |
| POST | `/rpc/set_stream_channel` | Set stream channel (1 or 2) |
| GET | `/rpc/match` | Get match details + crew + rosters |
| GET | `/rpc/bracket` | Get full bracket state |
| GET | `/rpc/logos/pending` | List pending logo submissions |
| POST | `/rpc/logos/approve` | Approve a logo |
| POST | `/rpc/logos/reject` | Reject a logo |
| GET | `/rpc/logos` | List all approved logos |
| GET | `/rpc/sso` | Single sign-on (browser redirect) |
| GET | `/api/logs` | View live RPC logs (monitoring) |

---

## Match / Broadcast Endpoints

These are the core endpoints for running a broadcast. The typical workflow is:

1. **Get match info** → `GET /rpc/match`
2. **Create channel** → `POST /rpc/create_channel`
3. **Set stream** → `POST /rpc/set_stream_channel`
4. **Crew ready** → `POST /rpc/crew_ready`
5. **Go live** → `POST /rpc/go_live`

All match endpoints accept either a numeric `id` (simple ID from the sheet) or the full match key (e.g. `nocturne_Valiants_03272026_1030_PM`).

### GET `/rpc/match`

Get full details for a single match including teams, crew claims, rosters, and logos.

```
GET /rpc/match?id=42
Header: X-API-Key: your-shared-key
```

**Response:**

```json
{
  "success": true,
  "match": {
    "id": 42,
    "match_id": "nocturne_Valiants_03272026_1030_PM",
    "team_a": "Nocturne",
    "team_b": "Valiants",
    "team_a_rank": 3,
    "team_b_rank": 7,
    "team_a_logo": "https://casterbot.onrender.com/team-logo/Nocturne",
    "team_b_logo": "https://casterbot.onrender.com/team-logo/Valiants",
    "team_a_roster": [
      {"user_id": "123", "username": "player1", "display_name": "Player One"}
    ],
    "team_b_roster": [
      {"user_id": "456", "username": "player2", "display_name": "Player Two"}
    ],
    "match_date": "2026-03-27",
    "match_time": "10:30 PM",
    "match_timestamp": "2026-03-27T22:30:00",
    "stream_channel": 1,
    "has_channel": true,
    "casters": [
      {"user_id": "789", "slot": 1, "display_name": "CasterOne", "avatar_url": "..."}
    ],
    "cam_op": {"user_id": "101", "display_name": "CamOpOne", "avatar_url": "..."}
  }
}
```

### POST `/rpc/create_channel`

Create the private Discord text channel for a match. Requires at least 1 caster and 1 cam op claimed.

```
POST /rpc/create_channel
Header: X-API-Key: your-shared-key
Content-Type: application/json

{ "id": 42 }
```

**Response (success):**

```json
{ "success": true, "channel_id": 1234567890123456789 }
```

**Response (errors):**

```json
{ "success": false, "error": "Need at least 1 caster and 1 cam op" }
{ "success": false, "error": "Channel already exists" }
```

### POST `/rpc/set_stream_channel`

Set which stream channel (1 or 2) this match will broadcast on.

```
POST /rpc/set_stream_channel
Header: X-API-Key: your-shared-key
Content-Type: application/json

{ "id": 42, "channel": 1 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | int/string | Yes | Match ID |
| `channel` | int | Yes | `1` or `2` |

**Response:**

```json
{ "success": true }
```

### POST `/rpc/crew_ready`

Send a "crew is ready" message in the private match channel, pinging both teams.

```
POST /rpc/crew_ready
Header: X-API-Key: your-shared-key
Content-Type: application/json

{ "id": 42 }
```

**Response:**

```json
{ "success": true }
```

### POST `/rpc/go_live`

Post the live announcement in the configured announcements channel with stream link and team mentions.

**Prerequisites:** channel created, crew claimed, stream channel set.

```
POST /rpc/go_live
Header: X-API-Key: your-shared-key
Content-Type: application/json

{ "id": 42 }
```

**Response:**

```json
{ "success": true }
```

---

## Bracket Endpoint

### GET `/rpc/bracket`

Returns the full double-elimination bracket state (all 14 slots). See `BRACKET_RPC_INTEGRATION.md` for the complete slot reference and response shape.

```
GET /rpc/bracket
Header: X-API-Key: your-shared-key
```

**Response (abbreviated):**

```json
{
  "success": true,
  "bracket": {
    "WQF1": {
      "slot": "WQF1",
      "label": "WQF 1",
      "team_a": "Nocturne",
      "team_b": "Valiants",
      "winner": null,
      "match_id": "nocturne_Valiants_03272026_1030_PM",
      "stream_channel": 1,
      "team_a_logo": "https://casterbot.onrender.com/team-logo/Nocturne",
      "team_b_logo": "https://casterbot.onrender.com/team-logo/Valiants",
      "match": {
        "id": 42,
        "match_id": "nocturne_Valiants_03272026_1030_PM",
        "has_channel": true,
        "stream_channel": 1
      },
      "crew": [
        {"role": "caster", "slot_num": 1, "user_id": "789", "display_name": "CasterOne"}
      ]
    }
  }
}
```

---

## Logo Endpoints

### GET `/rpc/logos/pending`

List pending logo submissions (not yet approved/rejected).

```
GET /rpc/logos/pending
Header: X-API-Key: your-shared-key
```

**Response:**

```json
{
  "success": true,
  "pending": [
    {
      "message_id": "1234567890",
      "user_id": "987654321",
      "username": "player1",
      "display_name": "Player One",
      "team_name": "Algorithm",
      "image_url": "https://cdn.discordapp.com/attachments/...",
      "image_filename": "logo.png",
      "posted_at": "2026-07-10T12:00:00",
      "has_existing_logo": false
    }
  ]
}
```

### GET `/rpc/logos`

List all approved team logos.

```
GET /rpc/logos
Header: X-API-Key: your-shared-key
```

**Response:**

```json
{
  "success": true,
  "logos": [
    {
      "team_name": "Algorithm",
      "logo_url": "https://casterbot.onrender.com/team-logo/Algorithm",
      "approved_at": 1750612345
    }
  ]
}
```

### POST `/rpc/logos/approve`

Approve a logo submission. Downloads the image and saves it.

```
POST /rpc/logos/approve
Header: X-API-Key: your-shared-key
Content-Type: application/json

{
  "message_id": "1234567890",
  "team_name": "Algorithm",
  "image_url": "https://cdn.discordapp.com/attachments/.../logo.png",
  "approved_by": "aaliyah"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `message_id` | string | Yes | Discord message ID of the submission |
| `team_name` | string | Yes | Team name |
| `image_url` | string | Yes | Direct URL to the logo image |
| `approved_by` | string | No | Who approved it (for audit) |

**Response:**

```json
{ "success": true, "message": "Approved logo for Algorithm" }
```

### POST `/rpc/logos/reject`

Reject a logo submission. Optionally deletes the Discord message.

```
POST /rpc/logos/reject
Header: X-API-Key: your-shared-key
Content-Type: application/json

{
  "message_id": "1234567890",
  "delete_message": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `message_id` | string | Yes | Discord message ID |
| `delete_message` | bool | No | Delete the Discord message (default: false). If false, adds ❌ reaction instead. |

**Response:**

```json
{ "success": true, "message": "Logo rejected" }
```

---

## SSO Login

Seamlessly log users into CasterBot from your site without a second Discord prompt.

### Flow

1. User is already logged into your site via Discord OAuth2
2. You have the user's Discord **access token**
3. Redirect the user's browser to:

```
GET /rpc/sso?token=<DISCORD_ACCESS_TOKEN>&redirect=<DESTINATION_URL>
```

1. CasterBot verifies the token, creates a session, sets a cookie
2. User is redirected to the destination — fully logged in

### Parameters

| Param | Description |
|---|---|
| `token` | The user's Discord OAuth2 access token |
| `redirect` | Where to send the user after login (e.g., `/` or `/broadcast`) |

### Example: HTML link/redirect

```html
<a href="https://casterbot.onrender.com/rpc/sso?token=USER_ACCESS_TOKEN&redirect=/">
  Go to Broadcast Hub
</a>
```

Or in JavaScript:

```js
const token = getDiscordAccessToken(); // your existing token
window.location = `https://casterbot.onrender.com/rpc/sso?token=${token}&redirect=/`;
```

### Example: PHP redirect

```php
$token = $_SESSION['discord_access_token'];
$redirect = urlencode('/broadcast');
header("Location: https://casterbot.onrender.com/rpc/sso?token=$token&redirect=$redirect");
exit;
```

### Important

- The access token is sent in the URL — use **HTTPS only**
- Tokens expire quickly; call this immediately after login
- The session cookie lasts 7 days

---

## Monitoring — Logs Endpoint

### GET `/api/logs`

View recent server logs to confirm RPC calls succeeded or debug failures. Accepts API key or lead-role session.

```
GET /api/logs?search=[RPC]&format=text
Header: X-API-Key: your-shared-key
```

| Param | Description |
|---|---|
| `search` | Filter lines containing this text (case-insensitive). Use `[RPC]` for all RPC calls, `FAIL` for failures only, `PASS` for successes. |
| `level` | Filter by log level: `INFO`, `WARNING`, `ERROR` |
| `format` | `text` (default, plain text) or `json` |

**Example: check if create_channel worked:**

```bash
curl -H "X-API-Key: $KEY" "https://casterbot.onrender.com/api/logs?search=create_channel"
```

**Example: see only failures:**

```bash
curl -H "X-API-Key: $KEY" "https://casterbot.onrender.com/api/logs?search=FAIL"
```

**Example: JSON for programmatic use:**

```bash
curl -H "X-API-Key: $KEY" "https://casterbot.onrender.com/api/logs?search=[RPC]&format=json"
```

---

## Error Responses

All endpoints return errors in this format:

```json
{
  "success": false,
  "error": "Description of what went wrong"
}
```

| Status | Meaning |
|---|---|
| 401 | Missing or invalid API key / expired token (SSO) |
| 400 | Missing required fields, invalid JSON, or precondition not met |
| 404 | Match / bracket slot / channel not found |
| 500 | Server error (check `/api/logs` or Render logs) |

---

## Syncing Between Sites

Both your site and CasterBot operate on the **same Discord server and database**. When either side takes an action:

- **Logo approvals**: Image saved to CasterBot, ✅ reaction on Discord, appears in `GET /rpc/logos`
- **Channel creation**: Discord channel appears, `GET /rpc/match` reflects `has_channel: true`
- **Go live**: Announcement posts to Discord, no further action needed

No webhooks needed — just call the endpoints directly.

---

## CORS & Browser Access

CORS is enabled on all endpoints. You can call these directly from a browser at any origin. If the browser sends an `OPTIONS` preflight, the server responds with the appropriate `Access-Control-Allow-*` headers.

### Calling from a static site (Cloudflare Pages, Netlify, Vercel, etc.)

**Do not expose the API key in client-side JavaScript.** Instead, use a serverless function to proxy the request:

1. Store the key as an environment secret (`CASTERBOT_API_KEY`)
2. Create a function that reads the secret and forwards the request
3. Call your own function endpoint from the frontend

**Cloudflare Pages example** — save as `functions/api/logos.js`:

```js
export async function onRequest(context) {
  const url = new URL(context.request.url);
  const target = url.pathname.endsWith("/pending")
    ? "https://casterbot.onrender.com/rpc/logos/pending"
    : "https://casterbot.onrender.com/rpc/logos";

  const resp = await fetch(target, {
    headers: { "X-API-Key": context.env.CASTERBOT_API_KEY }
  });
  return new Response(await resp.text(), {
    headers: { "Content-Type": "application/json" }
  });
}
```

Then call from your frontend:

```js
fetch("/api/logos/pending")    // → proxies to /rpc/logos/pending
fetch("/api/logos/approved")   // → proxies to /rpc/logos
```

Same pattern works for any endpoint — just add more Functions.
