"""Optional web server to display claim status with Discord OAuth2."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from dateutil import tz as dateutil_tz

from . import config, db

log = logging.getLogger("casterbot.web")

# Simple in-memory session store
_sessions: dict[str, dict] = {}


def _get_session(request: web.Request) -> dict | None:
    """Get the current user's session."""
    session_id = request.cookies.get("session")
    if session_id and session_id in _sessions:
        return _sessions[session_id]
    return None


def _get_base_url(request: web.Request) -> str:
    """Get the base URL for OAuth redirects."""
    # Check for forwarded headers (behind proxy)
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "http")
    forwarded_host = request.headers.get("X-Forwarded-Host")
    
    if forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"
    
    # Fallback to config or request host
    if config.WEB_PUBLIC_URL:
        return config.WEB_PUBLIC_URL.rstrip("/")
    
    return f"http://{request.host}"


# HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Broadcast Schedule</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e0e0e0;
            padding: 20px;
        }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { text-align: center; color: #5865f2; margin-bottom: 10px; font-size: 2em; }
        .subtitle { text-align: center; color: #8e9297; margin-bottom: 20px; font-size: 0.9em; }
        .user-bar {
            display: flex; justify-content: center; align-items: center; gap: 15px;
            margin-bottom: 25px; padding: 12px; background: #2f3136; border-radius: 8px;
        }
        .user-info { display: flex; align-items: center; gap: 10px; }
        .user-avatar { width: 32px; height: 32px; border-radius: 50%; }
        .user-name { color: #ffffff; font-weight: 500; }
        .login-btn, .logout-btn {
            padding: 8px 16px; border-radius: 6px; text-decoration: none;
            font-weight: 500; font-size: 0.9em; transition: background 0.2s;
        }
        .login-btn { background: #5865f2; color: white; }
        .login-btn:hover { background: #4752c4; }
        .logout-btn { background: #4f545c; color: #dcddde; }
        .logout-btn:hover { background: #5d6269; }
        .match-card {
            background: #2f3136; border-radius: 12px; padding: 20px; margin-bottom: 16px;
            border-left: 4px solid #5865f2; box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .match-card.live { border-left-color: #ed4245; animation: pulse 2s infinite; }
        .match-card.soon { border-left-color: #faa61a; }
        @keyframes pulse {
            0%, 100% { box-shadow: 0 4px 6px rgba(237,66,69,0.3); }
            50% { box-shadow: 0 4px 20px rgba(237,66,69,0.5); }
        }
        .match-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .teams { font-size: 1.4em; font-weight: bold; color: #ffffff; }
        .team-vs { color: #5865f2; margin: 0 8px; }
        .match-id { background: #5865f2; color: white; padding: 4px 10px; border-radius: 12px; font-size: 0.8em; font-weight: bold; }
        .match-time { color: #8e9297; font-size: 0.95em; margin-bottom: 16px; }
        .time-relative { color: #5865f2; font-weight: 500; }
        .claims { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
        .claim-slot {
            background: #36393f; padding: 12px 14px; border-radius: 8px;
            display: flex; align-items: center; justify-content: space-between; gap: 8px;
        }
        .claim-slot.filled { background: #3ba55c20; border: 1px solid #3ba55c40; }
        .claim-slot.open { background: #40444b; border: 1px dashed #72767d; }
        .claim-slot.mine { background: #5865f220; border: 1px solid #5865f240; }
        .slot-info { display: flex; flex-direction: column; gap: 2px; }
        .role-label { font-weight: 600; color: #b9bbbe; font-size: 0.85em; }
        .holder-name { color: #ffffff; }
        .open-text { color: #72767d; font-style: italic; }
        .claim-btn, .unclaim-btn {
            padding: 6px 12px; border: none; border-radius: 4px;
            font-size: 0.8em; font-weight: 600; cursor: pointer; transition: background 0.2s;
        }
        .claim-btn { background: #3ba55c; color: white; }
        .claim-btn:hover { background: #2d8049; }
        .claim-btn:disabled { background: #4f545c; cursor: not-allowed; }
        .unclaim-btn { background: #ed4245; color: white; }
        .unclaim-btn:hover { background: #c73e3e; }
        .no-matches { text-align: center; padding: 60px 20px; color: #72767d; }
        .no-matches h2 { margin-bottom: 10px; color: #8e9297; }
        .refresh-info { text-align: center; color: #72767d; font-size: 0.85em; margin-top: 30px; }
        .status-badge {
            display: inline-block; padding: 2px 8px; border-radius: 4px;
            font-size: 0.75em; font-weight: bold; text-transform: uppercase; margin-left: 8px;
        }
        .status-badge.live { background: #ed4245; color: white; }
        .status-badge.soon { background: #faa61a; color: #1a1a1a; }
        .match-type { color: #72767d; font-size: 0.85em; margin-top: 8px; }
        .toast {
            position: fixed; bottom: 20px; right: 20px; padding: 12px 20px;
            border-radius: 8px; color: white; font-weight: 500; z-index: 1000;
            animation: slideIn 0.3s ease;
        }
        .toast.success { background: #3ba55c; }
        .toast.error { background: #ed4245; }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎙️ Broadcast Schedule</h1>
        <p class="subtitle">Upcoming matches and crew assignments</p>
        {user_bar}
        {content}
        <p class="refresh-info">Page auto-refreshes every 60 seconds</p>
    </div>
    <script>
        async function claimSlot(matchId, role, slot) {
            try {
                const resp = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId, role: role, slot: slot})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Claimed!', 'success');
                    setTimeout(() => location.reload(), 500);
                } else {
                    showToast(data.error || 'Failed to claim', 'error');
                }
            } catch (e) {
                showToast('Network error', 'error');
            }
        }
        
        async function unclaimSlot(matchId, role, slot) {
            try {
                const resp = await fetch('/api/unclaim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId, role: role, slot: slot})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Unclaimed!', 'success');
                    setTimeout(() => location.reload(), 500);
                } else {
                    showToast(data.error || 'Failed to unclaim', 'error');
                }
            } catch (e) {
                showToast('Network error', 'error');
            }
        }
        
        function showToast(msg, type) {
            const toast = document.createElement('div');
            toast.className = 'toast ' + type;
            toast.textContent = msg;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }
        
        setTimeout(() => location.reload(), 60000);
    </script>
</body>
</html>
"""


def _format_time_web(match: dict) -> tuple[str, str, str]:
    """Format match time for web display. Returns (formatted_time, relative_text, status)."""
    ts = match.get("match_timestamp")
    if not ts:
        return match.get("match_date", "?") + " " + match.get("match_time", "?"), "", ""
    
    eastern = dateutil_tz.gettz("America/New_York")
    dt = datetime.fromtimestamp(ts, tz=eastern)
    now = datetime.now(tz=eastern)
    
    formatted = dt.strftime("%A, %B %d, %Y at %I:%M %p %Z")
    diff = dt - now
    total_seconds = diff.total_seconds()
    
    if total_seconds < -3600:
        hours = int(abs(total_seconds) / 3600)
        return formatted, f"Started {hours}h ago", ""
    elif total_seconds < 0:
        minutes = int(abs(total_seconds) / 60)
        return formatted, f"Started {minutes}m ago", "live"
    elif total_seconds < 3600:
        minutes = int(total_seconds / 60)
        return formatted, f"Starting in {minutes}m", "soon"
    elif total_seconds < 86400:
        hours = int(total_seconds / 3600)
        return formatted, f"In {hours}h", ""
    else:
        days = int(total_seconds / 86400)
        return formatted, f"In {days}d", ""


def _build_match_card(match: dict, claims: list[dict], users: dict[int, str], current_user_id: int | None) -> str:
    """Build HTML for a single match card."""
    formatted_time, relative, status = _format_time_web(match)
    match_id = match["match_id"]
    
    card_class = "match-card"
    if status == "live":
        card_class += " live"
    elif status == "soon":
        card_class += " soon"
    
    status_badge = ""
    if status == "live":
        status_badge = '<span class="status-badge live">LIVE</span>'
    elif status == "soon":
        status_badge = '<span class="status-badge soon">SOON</span>'
    
    slots = []
    
    def build_slot(role: str, slot: int, label: str):
        claim = next((c for c in claims if c["role"] == role and c["slot"] == slot), None)
        if claim:
            user_name = users.get(claim["user_id"], f"User #{claim['user_id']}")
            is_mine = claim["user_id"] == current_user_id
            slot_class = "claim-slot mine" if is_mine else "claim-slot filled"
            
            if is_mine and current_user_id:
                button = f'<button class="unclaim-btn" onclick="unclaimSlot(\'{match_id}\', \'{role}\', {slot})">Unclaim</button>'
            else:
                button = ""
            
            return f'''
                <div class="{slot_class}">
                    <div class="slot-info">
                        <span class="role-label">{label}:</span>
                        <span class="holder-name">{user_name}</span>
                    </div>
                    {button}
                </div>
            '''
        else:
            if current_user_id:
                button = f'<button class="claim-btn" onclick="claimSlot(\'{match_id}\', \'{role}\', {slot})">Claim</button>'
            else:
                button = ""
            return f'''
                <div class="claim-slot open">
                    <div class="slot-info">
                        <span class="role-label">{label}:</span>
                        <span class="open-text">Open</span>
                    </div>
                    {button}
                </div>
            '''
    
    slots.append(build_slot("caster", 1, "Caster 1"))
    slots.append(build_slot("caster", 2, "Caster 2"))
    slots.append(build_slot("camop", 1, "Cam Op"))
    slots.append(build_slot("sideline", 1, "Sideline"))
    
    match_type_html = ""
    if match.get("match_type"):
        match_type_html = f'<p class="match-type">{match["match_type"]}</p>'
    
    return f'''
        <div class="{card_class}">
            <div class="match-header">
                <span class="teams">{match["team_a"]}<span class="team-vs">vs</span>{match["team_b"]}</span>
                <span class="match-id">#{match.get("simple_id", "?")}</span>
            </div>
            <p class="match-time">{formatted_time} <span class="time-relative">{status_badge} {relative}</span></p>
            <div class="claims">{"".join(slots)}</div>
            {match_type_html}
        </div>
    '''


async def login_handler(request: web.Request) -> web.Response:
    """Redirect to Discord OAuth2."""
    if not config.DISCORD_CLIENT_ID or not config.DISCORD_CLIENT_SECRET:
        return web.Response(text="OAuth not configured. Set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET.", status=500)
    
    base_url = _get_base_url(request)
    redirect_uri = f"{base_url}/callback"
    
    state = secrets.token_urlsafe(16)
    # Store state temporarily
    _sessions[f"state:{state}"] = {"redirect_uri": redirect_uri}
    
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    url = f"https://discord.com/api/oauth2/authorize?{urlencode(params)}"
    raise web.HTTPFound(url)


async def callback_handler(request: web.Request) -> web.Response:
    """Handle Discord OAuth2 callback."""
    code = request.query.get("code")
    state = request.query.get("state")
    
    if not code or not state:
        return web.Response(text="Missing code or state", status=400)
    
    state_data = _sessions.pop(f"state:{state}", None)
    if not state_data:
        return web.Response(text="Invalid state", status=400)
    
    redirect_uri = state_data["redirect_uri"]
    
    # Exchange code for token
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                return web.Response(text="Token exchange failed", status=400)
            token_data = await resp.json()
        
        # Get user info
        async with session.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        ) as resp:
            if resp.status != 200:
                return web.Response(text="Failed to get user info", status=400)
            user_data = await resp.json()
    
    # Create session
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
        "user_id": int(user_data["id"]),
        "username": user_data["username"],
        "discriminator": user_data.get("discriminator", "0"),
        "avatar": user_data.get("avatar"),
        "global_name": user_data.get("global_name"),
    }
    
    response = web.HTTPFound("/")
    response.set_cookie("session", session_id, max_age=86400 * 7, httponly=True, samesite="Lax")
    return response


async def logout_handler(request: web.Request) -> web.Response:
    """Log out the user."""
    session_id = request.cookies.get("session")
    if session_id and session_id in _sessions:
        del _sessions[session_id]
    
    response = web.HTTPFound("/")
    response.del_cookie("session")
    return response


async def schedule_handler(request: web.Request) -> web.Response:
    """Handle requests to the schedule page."""
    bot = request.app.get("bot")
    session = _get_session(request)
    current_user_id = session["user_id"] if session else None
    
    # Build user bar
    if session:
        avatar_hash = session.get("avatar")
        user_id = session["user_id"]
        display_name = session.get("global_name") or session["username"]
        
        if avatar_hash:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
        else:
            # Default avatar
            default_avatar = (user_id >> 22) % 6
            avatar_url = f"https://cdn.discordapp.com/embed/avatars/{default_avatar}.png"
        
        user_bar = f'''
            <div class="user-bar">
                <div class="user-info">
                    <img src="{avatar_url}" class="user-avatar" alt="">
                    <span class="user-name">{display_name}</span>
                </div>
                <a href="/logout" class="logout-btn">Logout</a>
            </div>
        '''
    else:
        oauth_configured = config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET
        if oauth_configured:
            user_bar = '''
                <div class="user-bar">
                    <span style="color: #8e9297;">Login to claim matches</span>
                    <a href="/login" class="login-btn">Login with Discord</a>
                </div>
            '''
        else:
            user_bar = '''
                <div class="user-bar">
                    <span style="color: #8e9297;">View-only mode (OAuth not configured)</span>
                </div>
            '''
    
    matches = await db.get_all_matches_sorted_by_time()
    
    if not matches:
        content = '''
            <div class="no-matches">
                <h2>No Upcoming Matches</h2>
                <p>Check back later for scheduled broadcasts.</p>
            </div>
        '''
    else:
        all_user_ids: set[int] = set()
        match_claims: dict[str, list[dict]] = {}
        
        for match in matches:
            claims = await db.get_claims(match["match_id"])
            match_claims[match["match_id"]] = claims
            for claim in claims:
                all_user_ids.add(claim["user_id"])
        
        users: dict[int, str] = {}
        if bot:
            for user_id in all_user_ids:
                try:
                    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                    users[user_id] = user.display_name if hasattr(user, "display_name") else user.name
                except Exception:
                    users[user_id] = f"User #{user_id}"
        
        cards = []
        for match in matches:
            claims = match_claims.get(match["match_id"], [])
            cards.append(_build_match_card(match, claims, users, current_user_id))
        
        content = "\n".join(cards)
    
    html = HTML_TEMPLATE.replace("{user_bar}", user_bar).replace("{content}", content)
    return web.Response(text=html, content_type="text/html")


async def api_claim_handler(request: web.Request) -> web.Response:
    """API endpoint to claim a slot."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    role = data.get("role")
    slot = data.get("slot")
    
    if not all([match_id, role, slot]):
        return web.json_response({"success": False, "error": "Missing parameters"}, status=400)
    
    user_id = session["user_id"]
    
    # Claim the slot
    await db.claim_slot(match_id, user_id, role, slot)
    
    # Refresh Discord message if bot available
    bot = request.app.get("bot")
    if bot:
        await _refresh_discord_message(bot, match_id)
    
    return web.json_response({"success": True})


async def api_unclaim_handler(request: web.Request) -> web.Response:
    """API endpoint to unclaim a slot."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    role = data.get("role")
    slot = data.get("slot")
    
    if not all([match_id, role, slot]):
        return web.json_response({"success": False, "error": "Missing parameters"}, status=400)
    
    user_id = session["user_id"]
    
    # Verify user owns this slot
    current_holder = await db.get_slot_holder(match_id, role, slot)
    if current_holder != user_id:
        return web.json_response({"success": False, "error": "You don't hold this slot"}, status=403)
    
    # Unclaim the slot
    await db.unclaim_slot(match_id, user_id, role, slot)
    
    # Refresh Discord message if bot available
    bot = request.app.get("bot")
    if bot:
        await _refresh_discord_message(bot, match_id)
    
    return web.json_response({"success": True})


async def _refresh_discord_message(bot, match_id: str) -> None:
    """Refresh the Discord claim message for a match."""
    try:
        match = await db.get_match(match_id)
        if not match or not match.get("message_id"):
            return
        
        channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
        if not channel:
            return
        
        msg = await channel.fetch_message(match["message_id"])
        claims = await db.get_claims(match_id)
        
        from .views import ClaimView
        new_view = ClaimView(match_id, match, claims)
        await msg.edit(view=new_view)
    except Exception as e:
        log.error(f"Failed to refresh Discord message: {e}")


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.Response(text="OK")


def create_app(bot=None) -> web.Application:
    """Create the aiohttp web application."""
    app = web.Application()
    app["bot"] = bot
    
    app.router.add_get("/", schedule_handler)
    app.router.add_get("/schedule", schedule_handler)
    app.router.add_get("/login", login_handler)
    app.router.add_get("/callback", callback_handler)
    app.router.add_get("/logout", logout_handler)
    app.router.add_post("/api/claim", api_claim_handler)
    app.router.add_post("/api/unclaim", api_unclaim_handler)
    app.router.add_get("/health", health_handler)
    
    return app


async def start_web_server(bot=None, host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start the web server. Returns the runner for cleanup."""
    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(f"Web server started at http://{host}:{port}")
    return runner


async def stop_web_server(runner: web.AppRunner) -> None:
    """Stop the web server."""
    await runner.cleanup()
    log.info("Web server stopped")
