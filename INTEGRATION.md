# CasterBot RPC Integration Guide

For connecting an external site to CasterBot's logo approval system and SSO.

**Base URL:** `https://casterbot.onrender.com`

---

## Authentication

All RPC endpoints require an API key. Send it as an HTTP header:

```
X-API-Key: your-shared-key
```

Ask Todd for the actual key value.

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
| 401 | Missing or invalid API key (logo endpoints) / expired token (SSO) |
| 400 | Missing required fields or invalid JSON |
| 500 | Server error (check Render logs) |

---

## Syncing Approvals Between Sites

Both your site and CasterBot's admin panel operate on the **same Discord channel and database**. When either site approves or rejects a logo:

- The image is saved to the CasterBot server
- The Discord message gets a ✅ or ❌ reaction
- `GET /rpc/logos/pending` no longer returns that submission
- `GET /rpc/logos` includes it (if approved)

No webhooks needed — just poll the endpoints or call them on user action.
