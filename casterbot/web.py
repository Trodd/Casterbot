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


async def _is_admin(bot, user_id: int) -> bool:
    """Check if a user has the lead role (admin access)."""
    if not bot or not config.WEB_LEAD_ROLE_ID or not config.GUILD_ID:
        return False
    
    try:
        guild = bot.get_guild(config.GUILD_ID)
        if not guild:
            return False
        
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                return False
        
        return any(role.id == config.WEB_LEAD_ROLE_ID for role in member.roles)
    except Exception:
        return False


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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>EML Broadcast Hub</title>
    
    <!-- PWA Support -->
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#0a0a12">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="EML Caster">
    <link rel="apple-touch-icon" href="/icon-192.png">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="application-name" content="EML Caster">
    <meta name="description" content="Echo Master League Broadcast Hub - Claim matches and manage casts">
    
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Rajdhani:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --echo-orange: #ff6a00;
            --echo-orange-glow: rgba(255, 106, 0, 0.4);
            --echo-cyan: #00d4ff;
            --echo-cyan-glow: rgba(0, 212, 255, 0.4);
            --echo-blue: #1a4fff;
            --echo-dark: #0a0a12;
            --echo-darker: #050508;
            --echo-panel: rgba(15, 15, 25, 0.9);
            --echo-border: rgba(255, 106, 0, 0.3);
            --echo-text: #e8e8f0;
            --echo-text-dim: #8888a0;
            --echo-success: #00ff88;
            --echo-danger: #ff3366;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Rajdhani', 'Segoe UI', sans-serif;
            background: var(--echo-darker);
            background-image: 
                radial-gradient(ellipse at top, rgba(26, 79, 255, 0.15) 0%, transparent 50%),
                radial-gradient(ellipse at bottom, rgba(255, 106, 0, 0.1) 0%, transparent 50%),
                repeating-linear-gradient(0deg, transparent, transparent 50px, rgba(255,106,0,0.03) 50px, rgba(255,106,0,0.03) 51px),
                repeating-linear-gradient(90deg, transparent, transparent 50px, rgba(255,106,0,0.03) 50px, rgba(255,106,0,0.03) 51px);
            min-height: 100vh;
            color: var(--echo-text);
            padding: 20px;
            position: relative;
        }
        body::before {
            content: '';
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(circle at 50% 50%, transparent 0%, var(--echo-darker) 70%);
            pointer-events: none;
            z-index: -1;
        }
        .container { max-width: 950px; margin: 0 auto; }
        .header {
            text-align: center;
            margin-bottom: 30px;
            position: relative;
        }
        .header::before {
            content: '';
            position: absolute;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            width: 300px; height: 300px;
            background: radial-gradient(circle, var(--echo-orange-glow) 0%, transparent 70%);
            opacity: 0.3;
            pointer-events: none;
        }
        h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 2.5em;
            font-weight: 900;
            letter-spacing: 4px;
            text-transform: uppercase;
            background: linear-gradient(180deg, var(--echo-orange) 0%, #ff8533 50%, var(--echo-orange) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            text-shadow: 0 0 40px var(--echo-orange-glow);
            position: relative;
        }
        .subtitle {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            font-size: 0.85em;
            letter-spacing: 3px;
            text-transform: uppercase;
            margin-top: 8px;
        }
        .season-badge {
            display: inline-block;
            margin-top: 16px;
            padding: 8px 20px;
            background: linear-gradient(135deg, rgba(255,106,0,0.2), rgba(0,212,255,0.2));
            border: 1px solid var(--echo-orange);
            border-radius: 20px;
            font-family: 'Orbitron', sans-serif;
            font-size: 0.9em;
            font-weight: 600;
            letter-spacing: 2px;
            color: var(--echo-text);
            box-shadow: 0 0 15px var(--echo-orange-glow);
        }
        .season-badge .season-num {
            color: var(--echo-orange);
        }
        .season-badge .week-num {
            color: var(--echo-cyan);
        }
        .season-badge .separator {
            color: var(--echo-text-dim);
            margin: 0 8px;
        }
        .user-bar {
            display: flex; justify-content: center; align-items: center; gap: 15px;
            margin-bottom: 30px; padding: 14px 20px;
            background: var(--echo-panel);
            border: 1px solid var(--echo-border);
            border-radius: 4px;
            box-shadow: 0 0 20px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.05);
        }
        .user-info { display: flex; align-items: center; gap: 12px; }
        .user-avatar { 
            width: 36px; height: 36px; border-radius: 50%;
            border: 2px solid var(--echo-orange);
            box-shadow: 0 0 10px var(--echo-orange-glow);
        }
        .user-name { 
            color: var(--echo-text); 
            font-weight: 600; 
            font-size: 1.1em;
            letter-spacing: 1px;
        }
        .login-btn, .logout-btn {
            padding: 10px 20px; border-radius: 4px; text-decoration: none;
            font-weight: 600; font-size: 0.9em; transition: all 0.3s;
            text-transform: uppercase; letter-spacing: 1px;
            font-family: 'Orbitron', sans-serif;
        }
        .login-btn { 
            background: linear-gradient(180deg, var(--echo-orange) 0%, #cc5500 100%);
            color: white;
            border: 1px solid var(--echo-orange);
            box-shadow: 0 0 15px var(--echo-orange-glow);
        }
        .login-btn:hover { 
            box-shadow: 0 0 25px var(--echo-orange-glow), 0 0 40px var(--echo-orange-glow);
            transform: translateY(-2px);
        }
        .logout-btn { 
            background: rgba(255,255,255,0.1); 
            color: var(--echo-text-dim);
            border: 1px solid rgba(255,255,255,0.2);
        }
        .logout-btn:hover { 
            background: rgba(255,255,255,0.2);
            color: var(--echo-text);
        }
        .match-card {
            background: var(--echo-panel);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 20px;
            border: 1px solid var(--echo-border);
            box-shadow: 0 4px 30px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.05);
            position: relative;
            overflow: hidden;
        }
        .match-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 3px;
            background: linear-gradient(90deg, transparent, var(--echo-orange), transparent);
        }
        .match-card.live { 
            border-color: var(--echo-danger);
            animation: livePulse 2s infinite;
        }
        .match-card.live::before {
            background: linear-gradient(90deg, transparent, var(--echo-danger), transparent);
        }
        .match-card.soon { 
            border-color: var(--echo-cyan);
        }
        .match-card.soon::before {
            background: linear-gradient(90deg, transparent, var(--echo-cyan), transparent);
        }
        @keyframes livePulse {
            0%, 100% { box-shadow: 0 4px 30px rgba(0,0,0,0.4), 0 0 20px rgba(255,51,102,0.2); }
            50% { box-shadow: 0 4px 30px rgba(0,0,0,0.4), 0 0 40px rgba(255,51,102,0.4); }
        }
        .match-header { 
            display: flex; justify-content: space-between; align-items: center; 
            margin-bottom: 16px;
        }
        .teams { 
            font-family: 'Orbitron', sans-serif;
            font-size: 1.5em; 
            font-weight: 700; 
            color: #ffffff;
            letter-spacing: 2px;
        }
        .team-vs { 
            color: var(--echo-orange); 
            margin: 0 12px;
            font-size: 0.8em;
        }
        .match-id { 
            background: linear-gradient(135deg, var(--echo-orange) 0%, #cc5500 100%);
            color: white; 
            padding: 6px 14px; 
            border-radius: 4px; 
            font-size: 0.85em; 
            font-weight: 700;
            font-family: 'Orbitron', sans-serif;
            letter-spacing: 1px;
            box-shadow: 0 0 10px var(--echo-orange-glow);
        }
        .match-time { 
            color: var(--echo-text-dim); 
            font-size: 1em; 
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(255,106,0,0.2);
        }
        .time-relative { 
            color: var(--echo-cyan); 
            font-weight: 600;
        }
        .claims { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
            gap: 12px; 
        }
        .claim-slot {
            background: rgba(0,0,0,0.3);
            padding: 14px 16px;
            border-radius: 6px;
            display: flex; align-items: center; justify-content: space-between; gap: 10px;
            border: 1px solid rgba(255,255,255,0.1);
            transition: all 0.3s;
        }
        .claim-slot:hover {
            border-color: rgba(255,106,0,0.3);
        }
        .claim-slot.filled { 
            background: rgba(0,255,136,0.1); 
            border: 1px solid rgba(0,255,136,0.3);
        }
        .claim-slot.open { 
            background: rgba(0,0,0,0.2); 
            border: 1px dashed rgba(255,255,255,0.2);
        }
        .claim-slot.mine { 
            background: rgba(0,212,255,0.15); 
            border: 1px solid rgba(0,212,255,0.4);
            box-shadow: 0 0 15px rgba(0,212,255,0.2);
        }
        .slot-info { display: flex; flex-direction: column; gap: 4px; }
        .role-label { 
            font-weight: 700; 
            color: var(--echo-orange); 
            font-size: 0.8em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .holder-name { color: #ffffff; font-weight: 500; }
        .open-text { color: var(--echo-text-dim); font-style: italic; }
        .claim-btn, .unclaim-btn {
            padding: 8px 14px; border: none; border-radius: 4px;
            font-size: 0.8em; font-weight: 700; cursor: pointer; 
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-family: 'Rajdhani', sans-serif;
        }
        .claim-btn { 
            background: linear-gradient(180deg, var(--echo-success) 0%, #00cc6a 100%);
            color: #001a0d;
            box-shadow: 0 0 10px rgba(0,255,136,0.3);
        }
        .claim-btn:hover { 
            box-shadow: 0 0 20px rgba(0,255,136,0.5);
            transform: translateY(-1px);
        }
        .claim-btn:disabled { 
            background: rgba(255,255,255,0.1); 
            color: var(--echo-text-dim);
            cursor: not-allowed;
            box-shadow: none;
        }
        .unclaim-btn { 
            background: linear-gradient(180deg, var(--echo-danger) 0%, #cc2952 100%);
            color: white;
            box-shadow: 0 0 10px rgba(255,51,102,0.3);
        }
        .unclaim-btn:hover { 
            box-shadow: 0 0 20px rgba(255,51,102,0.5);
        }
        .no-matches { 
            text-align: center; padding: 80px 20px; 
            color: var(--echo-text-dim);
        }
        .no-matches h2 { 
            margin-bottom: 12px; 
            color: var(--echo-orange);
            font-family: 'Orbitron', sans-serif;
        }
        .refresh-info { 
            text-align: center; 
            color: var(--echo-text-dim); 
            font-size: 0.85em; 
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid rgba(255,106,0,0.2);
        }
        .status-badge {
            display: inline-block; padding: 4px 10px; border-radius: 4px;
            font-size: 0.7em; font-weight: 700; text-transform: uppercase; 
            margin-left: 10px; letter-spacing: 1px;
            font-family: 'Orbitron', sans-serif;
        }
        .status-badge.live { 
            background: var(--echo-danger); 
            color: white;
            animation: liveFlash 1s infinite;
        }
        @keyframes liveFlash {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        .status-badge.soon { 
            background: var(--echo-cyan); 
            color: #001a1a;
        }
        .match-type { 
            color: var(--echo-text-dim); 
            font-size: 0.9em; 
            margin-top: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .broadcast-controls {
            margin-top: 20px; 
            padding-top: 20px; 
            border-top: 1px solid rgba(255,106,0,0.2);
        }
        .broadcast-controls h4 { 
            color: var(--echo-orange); 
            font-size: 0.85em; 
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 2px;
            font-family: 'Orbitron', sans-serif;
        }
        .broadcast-btns { display: flex; flex-wrap: wrap; gap: 10px; }
        .broadcast-btn {
            padding: 10px 18px; border: none; border-radius: 4px;
            font-size: 0.85em; font-weight: 700; cursor: pointer; 
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-family: 'Rajdhani', sans-serif;
        }
        .broadcast-btn.create { 
            background: linear-gradient(180deg, var(--echo-success) 0%, #00cc6a 100%);
            color: #001a0d;
            box-shadow: 0 0 10px rgba(0,255,136,0.3);
        }
        .broadcast-btn.create:hover { 
            box-shadow: 0 0 25px rgba(0,255,136,0.5);
        }
        .broadcast-btn.ready { 
            background: linear-gradient(180deg, var(--echo-cyan) 0%, #00a8cc 100%);
            color: #001a1a;
            box-shadow: 0 0 10px var(--echo-cyan-glow);
        }
        .broadcast-btn.ready:hover { 
            box-shadow: 0 0 25px var(--echo-cyan-glow);
        }
        .broadcast-btn.golive { 
            background: linear-gradient(180deg, var(--echo-danger) 0%, #cc2952 100%);
            color: white;
            box-shadow: 0 0 10px rgba(255,51,102,0.3);
        }
        .broadcast-btn.golive:hover { 
            box-shadow: 0 0 25px rgba(255,51,102,0.5);
        }
        .broadcast-btn:disabled { 
            background: rgba(255,255,255,0.1); 
            color: var(--echo-text-dim);
            cursor: not-allowed; 
            opacity: 0.5;
            box-shadow: none;
        }
        .toast {
            position: fixed; bottom: 20px; right: 20px; padding: 14px 24px;
            border-radius: 6px; color: white; font-weight: 600; z-index: 1000;
            animation: slideIn 0.3s ease;
            font-family: 'Orbitron', sans-serif;
            letter-spacing: 1px;
            text-transform: uppercase;
            font-size: 0.85em;
        }
        .toast.success { 
            background: var(--echo-success); 
            color: #001a0d;
            box-shadow: 0 0 20px rgba(0,255,136,0.4);
        }
        .toast.error { 
            background: var(--echo-danger);
            box-shadow: 0 0 20px rgba(255,51,102,0.4);
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .confirm-modal {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(5,5,8,0.95); 
            display: flex; align-items: center;
            justify-content: center; z-index: 1000;
        }
        .confirm-box {
            background: var(--echo-panel);
            padding: 30px;
            border-radius: 8px;
            text-align: center; 
            max-width: 420px;
            border: 1px solid var(--echo-orange);
            box-shadow: 0 0 40px var(--echo-orange-glow);
        }
        .confirm-box h3 { 
            color: var(--echo-orange); 
            margin-bottom: 16px;
            font-family: 'Orbitron', sans-serif;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        .confirm-box p { 
            color: var(--echo-text-dim); 
            margin-bottom: 24px;
        }
        .confirm-btns { display: flex; gap: 14px; justify-content: center; }
        .confirm-btns button {
            padding: 12px 28px; border: none; border-radius: 4px;
            font-weight: 700; cursor: pointer;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-family: 'Rajdhani', sans-serif;
            transition: all 0.3s;
        }
        .confirm-yes { 
            background: linear-gradient(180deg, var(--echo-success) 0%, #00cc6a 100%);
            color: #001a0d;
        }
        .confirm-yes:hover {
            box-shadow: 0 0 20px rgba(0,255,136,0.5);
        }
        .confirm-no { 
            background: rgba(255,255,255,0.1); 
            color: var(--echo-text);
            border: 1px solid rgba(255,255,255,0.2);
        }
        .confirm-no:hover {
            background: rgba(255,255,255,0.2);
        }
        /* Hexagon decoration */
        .hex-bg {
            position: fixed;
            top: 20px;
            right: 20px;
            width: 100px;
            height: 100px;
            opacity: 0.1;
            background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cpolygon points='50,5 95,27.5 95,72.5 50,95 5,72.5 5,27.5' fill='none' stroke='%23ff6a00' stroke-width='2'/%3E%3C/svg%3E") no-repeat center;
            pointer-events: none;
        }
        /* Tab Navigation */
        .tabs {
            display: flex;
            gap: 0;
            margin-bottom: 30px;
            border-bottom: 2px solid var(--echo-border);
        }
        .tab-btn {
            padding: 14px 30px;
            background: transparent;
            border: none;
            color: var(--echo-text-dim);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.95em;
            font-weight: 600;
            letter-spacing: 2px;
            text-transform: uppercase;
            cursor: pointer;
            position: relative;
            transition: all 0.3s;
        }
        .tab-btn:hover {
            color: var(--echo-text);
        }
        .tab-btn.active {
            color: var(--echo-orange);
        }
        .tab-btn.active::after {
            content: '';
            position: absolute;
            bottom: -2px;
            left: 0;
            right: 0;
            height: 2px;
            background: var(--echo-orange);
            box-shadow: 0 0 10px var(--echo-orange-glow);
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }
        /* Filter bar */
        .filter-bar {
            margin-bottom: 20px;
            display: flex;
            gap: 10px;
        }
        .filter-btn {
            padding: 10px 20px;
            background: rgba(0,212,255,0.1);
            border: 1px solid var(--echo-cyan);
            border-radius: 4px;
            color: var(--echo-cyan);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.8em;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.3s;
        }
        .filter-btn:hover {
            background: rgba(0,212,255,0.2);
            box-shadow: 0 0 10px rgba(0,212,255,0.3);
        }
        .filter-btn.active {
            background: var(--echo-cyan);
            color: var(--echo-bg);
            box-shadow: 0 0 15px rgba(0,212,255,0.5);
        }
        .no-claims-msg {
            text-align: center;
            color: var(--echo-text-dim);
            padding: 40px;
            font-style: italic;
        }
        .match-card.filtered-out {
            display: none !important;
        }
        /* Leaderboard styles */
        .leaderboard {
            background: var(--echo-panel);
            border-radius: 8px;
            border: 1px solid var(--echo-border);
            overflow: hidden;
            box-shadow: 0 4px 30px rgba(0,0,0,0.4);
        }
        .leaderboard-header {
            background: linear-gradient(90deg, rgba(255,106,0,0.2), transparent);
            padding: 20px 24px;
            border-bottom: 1px solid var(--echo-border);
        }
        .leaderboard-header h2 {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-orange);
            font-size: 1.2em;
            letter-spacing: 3px;
            text-transform: uppercase;
            margin: 0;
        }
        .leaderboard-row {
            display: grid;
            grid-template-columns: 60px 1fr 120px;
            align-items: center;
            padding: 16px 24px;
            border-bottom: 1px solid rgba(255,106,0,0.1);
            transition: all 0.3s;
        }
        .leaderboard-row:hover {
            background: rgba(255,106,0,0.05);
        }
        .leaderboard-row:last-child {
            border-bottom: none;
        }
        .leaderboard-row.top-1 {
            background: linear-gradient(90deg, rgba(255,215,0,0.15), transparent);
        }
        .leaderboard-row.top-2 {
            background: linear-gradient(90deg, rgba(192,192,192,0.1), transparent);
        }
        .leaderboard-row.top-3 {
            background: linear-gradient(90deg, rgba(205,127,50,0.1), transparent);
        }
        .rank {
            font-family: 'Orbitron', sans-serif;
            font-size: 1.4em;
            font-weight: 900;
            text-align: center;
        }
        .rank-1 { color: #ffd700; text-shadow: 0 0 10px rgba(255,215,0,0.5); }
        .rank-2 { color: #c0c0c0; text-shadow: 0 0 10px rgba(192,192,192,0.5); }
        .rank-3 { color: #cd7f32; text-shadow: 0 0 10px rgba(205,127,50,0.5); }
        .rank-other { color: var(--echo-text-dim); }
        .caster-info {
            display: flex;
            align-items: center;
            gap: 14px;
        }
        .caster-avatar {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            border: 2px solid var(--echo-border);
        }
        .caster-name {
            font-weight: 600;
            font-size: 1.1em;
            color: var(--echo-text);
        }
        .cast-count {
            font-family: 'Orbitron', sans-serif;
            font-size: 1.3em;
            font-weight: 700;
            color: var(--echo-cyan);
            text-align: right;
        }
        .cast-label {
            font-size: 0.7em;
            color: var(--echo-text-dim);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .no-data {
            text-align: center;
            padding: 60px 20px;
            color: var(--echo-text-dim);
        }
        .no-data h2 {
            color: var(--echo-orange);
            font-family: 'Orbitron', sans-serif;
            margin-bottom: 10px;
        }
        /* Cycle selector */
        .cycle-selector {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .cycle-selector label {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-text-dim);
            font-size: 0.85em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .cycle-select {
            background: var(--echo-panel);
            border: 1px solid var(--echo-border);
            color: var(--echo-text);
            padding: 10px 16px;
            border-radius: 4px;
            font-family: 'Rajdhani', sans-serif;
            font-size: 1em;
            cursor: pointer;
            min-width: 200px;
        }
        .cycle-select:focus {
            outline: none;
            border-color: var(--echo-orange);
            box-shadow: 0 0 10px var(--echo-orange-glow);
        }
        .cycle-select option {
            background: var(--echo-dark);
            color: var(--echo-text);
        }
        .cycle-info {
            background: linear-gradient(90deg, rgba(0,212,255,0.1), transparent);
            border: 1px solid rgba(0,212,255,0.3);
            border-radius: 6px;
            padding: 16px 20px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 20px;
            flex-wrap: wrap;
        }
        .cycle-info-item {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .cycle-info-label {
            font-size: 0.75em;
            color: var(--echo-text-dim);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .cycle-info-value {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            font-weight: 600;
        }
        /* Cycle history */
        .cycle-history {
            margin-top: 40px;
        }
        .cycle-history h3 {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-orange);
            font-size: 1em;
            letter-spacing: 2px;
            text-transform: uppercase;
            margin-bottom: 16px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--echo-border);
        }
        .cycle-history-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 16px;
        }
        .cycle-card {
            background: var(--echo-panel);
            border: 1px solid var(--echo-border);
            border-radius: 8px;
            padding: 20px;
            cursor: pointer;
            transition: all 0.3s;
            position: relative;
            overflow: hidden;
        }
        .cycle-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 3px;
            background: linear-gradient(90deg, transparent, var(--echo-cyan), transparent);
            opacity: 0;
            transition: opacity 0.3s;
        }
        .cycle-card:hover {
            border-color: var(--echo-cyan);
            box-shadow: 0 0 20px rgba(0,212,255,0.2);
        }
        .cycle-card:hover::before {
            opacity: 1;
        }
        .cycle-card-name {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-text);
            font-size: 1.1em;
            font-weight: 600;
            margin-bottom: 10px;
        }
        .cycle-card-dates {
            color: var(--echo-text-dim);
            font-size: 0.9em;
            margin-bottom: 8px;
        }
        .cycle-card-stats {
            display: flex;
            gap: 16px;
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid rgba(255,106,0,0.2);
        }
        .cycle-stat {
            display: flex;
            flex-direction: column;
        }
        .cycle-stat-value {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            font-weight: 700;
        }
        .cycle-stat-label {
            font-size: 0.7em;
            color: var(--echo-text-dim);
            text-transform: uppercase;
        }
        /* Admin tab styles */
        .admin-section {
            background: var(--echo-panel);
            border-radius: 8px;
            border: 1px solid var(--echo-border);
            margin-bottom: 20px;
            overflow: hidden;
        }
        .admin-section-header {
            background: linear-gradient(90deg, rgba(255,51,102,0.2), transparent);
            padding: 16px 20px;
            border-bottom: 1px solid var(--echo-border);
        }
        .admin-section-header h3 {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-danger);
            font-size: 1em;
            letter-spacing: 2px;
            text-transform: uppercase;
            margin: 0;
        }
        .admin-row {
            padding: 20px;
            border-bottom: 1px solid rgba(255,106,0,0.1);
        }
        .admin-row:last-child {
            border-bottom: none;
        }
        .admin-row-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }
        .admin-row-title {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            font-size: 0.95em;
            font-weight: 600;
        }
        .admin-row-desc {
            color: var(--echo-text-dim);
            font-size: 0.85em;
            margin-bottom: 12px;
        }
        .admin-form {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: flex-end;
        }
        .admin-input-group {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .admin-input-group label {
            font-size: 0.75em;
            color: var(--echo-text-dim);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .admin-input, .admin-select {
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--echo-border);
            color: var(--echo-text);
            padding: 8px 12px;
            border-radius: 4px;
            font-family: 'Rajdhani', sans-serif;
            font-size: 0.95em;
            min-width: 120px;
        }
        .admin-input:focus, .admin-select:focus {
            outline: none;
            border-color: var(--echo-cyan);
            box-shadow: 0 0 10px var(--echo-cyan-glow);
        }
        .admin-input::placeholder {
            color: var(--echo-text-dim);
        }
        .admin-btn {
            padding: 8px 18px;
            border: none;
            border-radius: 4px;
            font-family: 'Rajdhani', sans-serif;
            font-size: 0.9em;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.3s;
        }
        .admin-btn.primary {
            background: linear-gradient(180deg, var(--echo-cyan) 0%, #00a8cc 100%);
            color: #001a1a;
            box-shadow: 0 0 10px var(--echo-cyan-glow);
        }
        .admin-btn.primary:hover {
            box-shadow: 0 0 20px var(--echo-cyan-glow);
        }
        .admin-btn.danger {
            background: linear-gradient(180deg, var(--echo-danger) 0%, #cc2952 100%);
            color: white;
            box-shadow: 0 0 10px rgba(255,51,102,0.3);
        }
        .admin-btn.danger:hover {
            box-shadow: 0 0 20px rgba(255,51,102,0.5);
        }
        .admin-btn.success {
            background: linear-gradient(180deg, var(--echo-success) 0%, #00cc6a 100%);
            color: #001a0d;
            box-shadow: 0 0 10px rgba(0,255,136,0.3);
        }
        .admin-btn.success:hover {
            box-shadow: 0 0 20px rgba(0,255,136,0.5);
        }
        .admin-btn:disabled {
            background: rgba(255,255,255,0.1);
            color: var(--echo-text-dim);
            cursor: not-allowed;
            box-shadow: none;
        }
        .admin-warning {
            background: linear-gradient(90deg, rgba(255,51,102,0.1), transparent);
            border: 1px solid rgba(255,51,102,0.3);
            border-radius: 6px;
            padding: 16px 20px;
            margin-bottom: 20px;
            color: var(--echo-text-dim);
            font-size: 0.9em;
        }
        .admin-warning strong {
            color: var(--echo-danger);
        }
        .tab-btn.admin {
            color: var(--echo-danger);
        }
        .tab-btn.admin.active::after {
            background: var(--echo-danger);
            box-shadow: 0 0 10px rgba(255,51,102,0.5);
        }
        .admin-result {
            margin-top: 10px;
            padding: 10px 14px;
            border-radius: 4px;
            font-size: 0.9em;
            background: rgba(0,0,0,0.2);
            border: 1px solid var(--echo-border);
        }
        .admin-result:empty {
            display: none;
        }
        .admin-result .success {
            color: var(--echo-success);
        }
        .admin-result .error {
            color: var(--echo-danger);
        }
        .match-select-list {
            max-height: 200px;
            overflow-y: auto;
            background: rgba(0,0,0,0.2);
            border: 1px solid var(--echo-border);
            border-radius: 4px;
            margin-bottom: 10px;
        }
        .match-select-item {
            padding: 10px 14px;
            cursor: pointer;
            border-bottom: 1px solid rgba(255,106,0,0.1);
            transition: background 0.2s;
        }
        .match-select-item:hover {
            background: rgba(255,106,0,0.1);
        }
        .match-select-item.selected {
            background: rgba(0,212,255,0.15);
            border-left: 3px solid var(--echo-cyan);
        }
        .match-select-item:last-child {
            border-bottom: none;
        }
        .match-select-teams {
            font-weight: 600;
            color: var(--echo-text);
        }
        .match-select-id {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-orange);
            font-size: 0.85em;
        }
    </style>
</head>
<body>
    <div class="hex-bg"></div>
    <div class="container">
        <div class="header">
            <h1>Echo Master League</h1>
            <p class="subtitle">Broadcast Hub</p>
            {season_badge}
        </div>
        {user_bar}
        <div class="tabs">
            <button class="tab-btn {schedule_active}" onclick="switchTab('schedule')">Schedule</button>
            <button class="tab-btn {leaderboard_active}" onclick="switchTab('leaderboard')">Leaderboard</button>
            {admin_tab_btn}
        </div>
        <div id="tab-schedule" class="tab-content {schedule_content_active}">
            {filter_bar}
            <div id="schedule-content">
                {content}
            </div>
            <p class="no-claims-msg" id="no-claims-msg" style="display:none;">You haven't claimed any matches yet.</p>
        </div>
        <div id="tab-leaderboard" class="tab-content {leaderboard_content_active}">
            {cycle_selector}
            {cycle_info}
            {leaderboard_content}
            {cycle_history}
        </div>
        {admin_tab_content}
        <p class="refresh-info">// AUTO-REFRESH: 60 SECONDS //</p>
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
        
        function showConfirm(title, message) {
            return new Promise((resolve) => {
                const modal = document.createElement('div');
                modal.className = 'confirm-modal';
                modal.innerHTML = `
                    <div class="confirm-box">
                        <h3>${title}</h3>
                        <p>${message}</p>
                        <div class="confirm-btns">
                            <button class="confirm-yes">Yes</button>
                            <button class="confirm-no">No</button>
                        </div>
                    </div>
                `;
                document.body.appendChild(modal);
                modal.querySelector('.confirm-yes').onclick = () => { modal.remove(); resolve(true); };
                modal.querySelector('.confirm-no').onclick = () => { modal.remove(); resolve(false); };
            });
        }
        
        async function createChannel(matchId) {
            if (!await showConfirm('Create Channel', 'Create the private match channel?')) return;
            try {
                const resp = await fetch('/api/create_channel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Channel created!', 'success');
                    setTimeout(() => location.reload(), 500);
                } else {
                    showToast(data.error || 'Failed to create channel', 'error');
                }
            } catch (e) {
                showToast('Network error', 'error');
            }
        }
        
        async function crewReady(matchId) {
            if (!await showConfirm('Crew Ready', 'Send the crew ready message to the channel?')) return;
            try {
                const resp = await fetch('/api/crew_ready', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Ready message sent!', 'success');
                } else {
                    showToast(data.error || 'Failed to send message', 'error');
                }
            } catch (e) {
                showToast('Network error', 'error');
            }
        }
        
        async function goLive(matchId) {
            if (!await showConfirm('Go Live', 'Post the live announcement?')) return;
            try {
                const resp = await fetch('/api/go_live', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Live announcement posted!', 'success');
                } else {
                    showToast(data.error || 'Failed to go live', 'error');
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
        
        function switchTab(tab) {
            // Update URL without reload
            const url = new URL(window.location);
            url.searchParams.set('tab', tab);
            window.history.pushState({}, '', url);
            
            // Update button states
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelector(`.tab-btn[onclick="switchTab('${tab}')"]`).classList.add('active');
            
            // Update content visibility
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');
        }
        
        function selectCycle(cycleId) {
            const url = new URL(window.location);
            url.searchParams.set('tab', 'leaderboard');
            if (cycleId === 'current') {
                url.searchParams.delete('cycle');
            } else {
                url.searchParams.set('cycle', cycleId);
            }
            window.location.href = url.toString();
        }
        
        function viewCycle(cycleId) {
            selectCycle(cycleId);
        }
        
        // Filter for "My Claims" only
        let myClaimsFilterActive = false;
        function toggleMyClaimsFilter() {
            myClaimsFilterActive = !myClaimsFilterActive;
            const btn = document.getElementById('filter-my-claims');
            const cards = document.querySelectorAll('.match-card');
            const noClaimsMsg = document.getElementById('no-claims-msg');
            
            if (myClaimsFilterActive) {
                btn.classList.add('active');
                btn.textContent = 'Show All Matches';
                let visibleCount = 0;
                cards.forEach(card => {
                    if (card.dataset.myClaim === 'true') {
                        card.classList.remove('filtered-out');
                        visibleCount++;
                    } else {
                        card.classList.add('filtered-out');
                    }
                });
                noClaimsMsg.style.display = visibleCount === 0 ? 'block' : 'none';
            } else {
                btn.classList.remove('active');
                btn.textContent = 'Show My Claims Only';
                cards.forEach(card => card.classList.remove('filtered-out'));
                noClaimsMsg.style.display = 'none';
            }
        }
        
        // Admin functions
        async function adminSyncMatches(btn) {
            btn.disabled = true;
            btn.textContent = 'Syncing...';
            const result = document.getElementById('result-sync');
            try {
                const resp = await fetch('/api/admin/sync', {method: 'POST', credentials: 'include'});
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Matches synced', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
            btn.disabled = false;
            btn.textContent = 'Sync Now';
        }
        
        async function adminRefreshMessages(btn) {
            btn.disabled = true;
            btn.textContent = 'Refreshing...';
            const result = document.getElementById('result-refresh');
            try {
                const resp = await fetch('/api/admin/refresh', {method: 'POST', credentials: 'include'});
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Messages refreshed', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
            btn.disabled = false;
            btn.textContent = 'Refresh';
        }
        
        async function adminForceChannel() {
            const matchId = document.getElementById('force-channel-match').value;
            const result = document.getElementById('result-force-channel');
            if (!matchId) {
                result.innerHTML = '<span class="error">Select a match first</span>';
                return;
            }
            try {
                const resp = await fetch('/api/admin/force-channel', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId})
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Channel created', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminSetWeek() {
            const season = document.getElementById('set-season').value;
            const week = document.getElementById('set-week').value;
            const result = document.getElementById('result-set-week');
            if (!season || !week) {
                result.innerHTML = '<span class="error">Enter both season and week</span>';
                return;
            }
            try {
                const resp = await fetch('/api/admin/set-week', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({season: season, week: week})
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Season/Week updated', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminEditLeaderboard() {
            const userId = document.getElementById('edit-lb-user').value;
            const count = document.getElementById('edit-lb-count').value;
            const result = document.getElementById('result-edit-lb');
            if (!userId || count === '') {
                result.innerHTML = '<span class="error">Enter user ID and count</span>';
                return;
            }
            try {
                const resp = await fetch('/api/admin/edit-leaderboard', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: userId, count: parseInt(count)})
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Leaderboard updated', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminResetLeaderboard() {
            const result = document.getElementById('result-reset-lb');
            if (!confirm('Are you sure you want to reset ALL cast counts to zero? This cannot be undone!')) {
                return;
            }
            try {
                const resp = await fetch('/api/admin/reset-leaderboard', {
                    method: 'POST',
                    credentials: 'include'
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Leaderboard reset', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminStartCycle() {
            const name = document.getElementById('cycle-name').value;
            const weeks = document.getElementById('cycle-weeks').value;
            const startDate = document.getElementById('cycle-start').value;
            const endDate = document.getElementById('cycle-end').value;
            const result = document.getElementById('result-cycle');
            
            if (!name) {
                result.innerHTML = '<span class="error">Enter a cycle name</span>';
                return;
            }
            if (!confirm('Archive current leaderboard as "' + name + '" and reset? This preserves history.')) {
                return;
            }
            try {
                const resp = await fetch('/api/admin/start-cycle', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: name,
                        weeks: parseInt(weeks) || null,
                        start_date: startDate || null,
                        end_date: endDate || null
                    })
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Cycle archived', 'success');
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        setTimeout(() => location.reload(), 60000);
        
        // Register Service Worker for PWA
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(() => {});
        }
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
    
    # Check if current user has any claim in this match
    has_my_claim = any(c["user_id"] == current_user_id for c in claims) if current_user_id else False
    
    match_type_html = ""
    if match.get("match_type"):
        match_type_html = f'<p class="match-type">{match["match_type"]}</p>'
    
    # Check if requirements met for controls
    has_caster = any(c for c in claims if c["role"] == "caster")
    has_camop = any(c for c in claims if c["role"] == "camop")
    has_channel = bool(match.get("private_channel_id"))
    can_create = has_caster and has_camop and not has_channel
    can_ready = has_channel
    can_go_live = has_channel and has_caster and has_camop
    
    # Build broadcast controls (only show if logged in)
    broadcast_html = ""
    if current_user_id:
        create_disabled = "" if can_create else "disabled"
        ready_disabled = "" if can_ready else "disabled"
        live_disabled = "" if can_go_live else "disabled"
        
        channel_info = ""
        if has_channel:
            channel_info = '<span style="color: #3ba55c; font-size: 0.85em; margin-left: 8px;">✓ Channel exists</span>'
        
        broadcast_html = f'''
            <div class="broadcast-controls">
                <h4>Broadcast Controls {channel_info}</h4>
                <div class="broadcast-btns">
                    <button class="broadcast-btn create" onclick="createChannel('{match_id}')" {create_disabled}>Create Channel</button>
                    <button class="broadcast-btn ready" onclick="crewReady('{match_id}')" {ready_disabled}>Crew Ready</button>
                    <button class="broadcast-btn golive" onclick="goLive('{match_id}')" {live_disabled}>Go Live</button>
                </div>
            </div>
        '''
    
    my_claim_attr = ' data-my-claim="true"' if has_my_claim else ''
    
    return f'''
        <div class="{card_class}"{my_claim_attr}>
            <div class="match-header">
                <span class="teams">{match["team_a"]}<span class="team-vs">vs</span>{match["team_b"]}</span>
                <span class="match-id">#{match.get("simple_id", "?")}</span>
            </div>
            <p class="match-time">{formatted_time} <span class="time-relative">{status_badge} {relative}</span></p>
            <div class="claims">{"".join(slots)}</div>
            {match_type_html}
            {broadcast_html}
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
    
    # Check if user is admin
    is_admin = await _is_admin(bot, current_user_id) if current_user_id else False
    
    # Determine active tab
    active_tab = request.query.get("tab", "schedule")
    valid_tabs = ["schedule", "leaderboard"]
    if is_admin:
        valid_tabs.append("admin")
    if active_tab not in valid_tabs:
        active_tab = "schedule"
    
    schedule_active = "active" if active_tab == "schedule" else ""
    leaderboard_active = "active" if active_tab == "leaderboard" else ""
    admin_active = "active" if active_tab == "admin" else ""
    schedule_content_active = "active" if active_tab == "schedule" else ""
    leaderboard_content_active = "active" if active_tab == "leaderboard" else ""
    admin_content_active = "active" if active_tab == "admin" else ""
    
    # Build season badge
    season = await db.get_setting("season")
    week = await db.get_setting("week")
    
    if season or week:
        season_part = f'<span class="season-num">SEASON {season}</span>' if season else ''
        week_part = f'<span class="week-num">WEEK {week}</span>' if week else ''
        separator = '<span class="separator">//</span>' if season and week else ''
        season_badge = f'<div class="season-badge">{season_part}{separator}{week_part}</div>'
    else:
        season_badge = ''
    
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
    
    # Build filter bar (only for logged in users)
    if session:
        filter_bar = '''
            <div class="filter-bar">
                <button class="filter-btn" id="filter-my-claims" onclick="toggleMyClaimsFilter()">Show My Claims Only</button>
            </div>
        '''
    else:
        filter_bar = ''
    
    # Build schedule content
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
    
    # Build leaderboard content with cycle support
    cycles = await db.get_cycles()
    selected_cycle_id = request.query.get("cycle")
    selected_cycle = None
    
    # Determine which leaderboard to show
    if selected_cycle_id and selected_cycle_id.isdigit():
        selected_cycle_id = int(selected_cycle_id)
        selected_cycle = await db.get_cycle_by_id(selected_cycle_id)
        if selected_cycle:
            leaderboard_data = await db.get_cycle_leaderboard(selected_cycle_id)
        else:
            # Invalid cycle, fall back to current
            selected_cycle_id = None
            leaderboard_data = await db.get_caster_leaderboard(limit=100)
    else:
        selected_cycle_id = None
        leaderboard_data = await db.get_caster_leaderboard(limit=100)
    
    # For current season, include all members with caster/camop roles (even with 0 casts)
    if not selected_cycle_id and bot:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            # Get all role IDs to check
            role_ids = set()
            for rid in (config.CASTER_ROLE_ID, config.CAMOP_ROLE_ID, 
                       config.CASTER_TRAINING_ROLE_ID, config.CAMOP_TRAINING_ROLE_ID):
                if rid:
                    role_ids.add(rid)
            
            # Get existing user IDs from leaderboard
            existing_user_ids = {entry["user_id"] for entry in leaderboard_data}
            
            # Find all members with these roles who aren't already in the leaderboard
            for member in guild.members:
                if member.bot:
                    continue
                member_role_ids = {r.id for r in member.roles}
                if member_role_ids & role_ids:  # Has at least one of the roles
                    if member.id not in existing_user_ids:
                        leaderboard_data.append({"user_id": member.id, "cast_count": 0})
            
            # Sort by cast_count descending, then by user_id for consistency
            leaderboard_data.sort(key=lambda x: (-x["cast_count"], x["user_id"]))
    
    # Build cycle selector dropdown
    if cycles:
        options = ['<option value="current"' + (' selected' if not selected_cycle_id else '') + '>Current Season</option>']
        for cycle in cycles:
            selected_attr = ' selected' if selected_cycle_id == cycle["cycle_id"] else ''
            options.append(f'<option value="{cycle["cycle_id"]}"{selected_attr}>{cycle["cycle_name"]}</option>')
        
        cycle_selector = f'''
            <div class="cycle-selector">
                <label>Season:</label>
                <select class="cycle-select" onchange="selectCycle(this.value)">
                    {"".join(options)}
                </select>
            </div>
        '''
    else:
        cycle_selector = ''
    
    # Build cycle info if viewing a past cycle
    if selected_cycle:
        cycle_info = f'''
            <div class="cycle-info">
                <div class="cycle-info-item">
                    <span class="cycle-info-label">Season</span>
                    <span class="cycle-info-value">{selected_cycle["cycle_name"]}</span>
                </div>
                <div class="cycle-info-item">
                    <span class="cycle-info-label">Duration</span>
                    <span class="cycle-info-value">{selected_cycle["weeks"]} weeks</span>
                </div>
                <div class="cycle-info-item">
                    <span class="cycle-info-label">Period</span>
                    <span class="cycle-info-value">{selected_cycle["start_date"]} - {selected_cycle["end_date"]}</span>
                </div>
            </div>
        '''
    else:
        cycle_info = ''
    
    # Build leaderboard rows
    if not leaderboard_data:
        leaderboard_content = '''
            <div class="no-data">
                <h2>No Stats Yet</h2>
                <p>Complete some broadcasts to appear on the leaderboard!</p>
            </div>
        '''
    else:
        rows = []
        for idx, entry in enumerate(leaderboard_data, start=1):
            user_id = entry["user_id"]
            cast_count = entry["cast_count"]
            
            # Get user info
            user_name = f"User #{user_id}"
            avatar_url = f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"
            
            if bot:
                try:
                    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                    user_name = user.display_name if hasattr(user, "display_name") else user.name
                    if user.avatar:
                        avatar_url = user.avatar.url
                except Exception:
                    pass
            
            # Rank styling
            if idx == 1:
                rank_class = "rank-1"
                row_class = "leaderboard-row top-1"
            elif idx == 2:
                rank_class = "rank-2"
                row_class = "leaderboard-row top-2"
            elif idx == 3:
                rank_class = "rank-3"
                row_class = "leaderboard-row top-3"
            else:
                rank_class = "rank-other"
                row_class = "leaderboard-row"
            
            rows.append(f'''
                <div class="{row_class}">
                    <div class="rank {rank_class}">{idx}</div>
                    <div class="caster-info">
                        <img src="{avatar_url}" class="caster-avatar" alt="">
                        <span class="caster-name">{user_name}</span>
                    </div>
                    <div class="cast-count">
                        {cast_count}
                        <div class="cast-label">casts</div>
                    </div>
                </div>
            ''')
        
        title = "Top Casters" if not selected_cycle else f"{selected_cycle['cycle_name']} - Final Standings"
        leaderboard_content = f'''
            <div class="leaderboard">
                <div class="leaderboard-header">
                    <h2>{title}</h2>
                </div>
                {"".join(rows)}
            </div>
        '''
    
    # Build cycle history section (only show when viewing current season)
    if cycles and not selected_cycle_id:
        cycle_cards = []
        for cycle in cycles:
            # Get top caster for this cycle
            cycle_lb = await db.get_cycle_leaderboard(cycle["cycle_id"])
            total_casts = sum(entry["cast_count"] for entry in cycle_lb)
            top_caster_count = cycle_lb[0]["cast_count"] if cycle_lb else 0
            caster_count = len(cycle_lb)
            
            cycle_cards.append(f'''
                <div class="cycle-card" onclick="viewCycle({cycle["cycle_id"]})">
                    <div class="cycle-card-name">{cycle["cycle_name"]}</div>
                    <div class="cycle-card-dates">{cycle["start_date"]} - {cycle["end_date"]}</div>
                    <div class="cycle-card-stats">
                        <div class="cycle-stat">
                            <span class="cycle-stat-value">{caster_count}</span>
                            <span class="cycle-stat-label">Casters</span>
                        </div>
                        <div class="cycle-stat">
                            <span class="cycle-stat-value">{total_casts}</span>
                            <span class="cycle-stat-label">Total Casts</span>
                        </div>
                        <div class="cycle-stat">
                            <span class="cycle-stat-value">{top_caster_count}</span>
                            <span class="cycle-stat-label">Top Score</span>
                        </div>
                    </div>
                </div>
            ''')
        
        cycle_history = f'''
            <div class="cycle-history">
                <h3>Past Seasons</h3>
                <div class="cycle-history-grid">
                    {"".join(cycle_cards)}
                </div>
            </div>
        '''
    else:
        cycle_history = ''
    
    # Build admin tab (only for admins)
    if is_admin:
        admin_tab_btn = f'<button class="tab-btn admin {admin_active}" onclick="switchTab(\'admin\')">Admin</button>'
        
        # Build match options for selects
        match_options = '<option value="">Select a match...</option>'
        for match in matches:
            match_options += f'<option value="{match["match_id"]}">#{match.get("simple_id", "?")} - {match["team_a"]} vs {match["team_b"]}</option>'
        
        admin_tab_content = f'''
            <div id="tab-admin" class="tab-content {admin_content_active}">
                <div class="admin-warning">
                    <strong>Admin Panel</strong> — Execute administrative commands directly from this interface.
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Match Sync</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-header">
                            <span class="admin-row-title">Sync Matches</span>
                        </div>
                        <div class="admin-row-desc">Manually sync upcoming matches from the Google Sheet.</div>
                        <div class="admin-form">
                            <button class="admin-btn primary" onclick="adminSyncMatches(this)">Sync Now</button>
                        </div>
                        <div class="admin-result" id="result-sync"></div>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-header">
                            <span class="admin-row-title">Refresh Messages</span>
                        </div>
                        <div class="admin-row-desc">Refresh all claim messages in the claim channel.</div>
                        <div class="admin-form">
                            <button class="admin-btn primary" onclick="adminRefreshMessages(this)">Refresh</button>
                        </div>
                        <div class="admin-result" id="result-refresh"></div>
                    </div>
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Force Create Channel</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-desc">Force create the private broadcast channel for a match (bypasses requirements).</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>Match</label>
                                <select class="admin-select" id="force-channel-match">
                                    {match_options}
                                </select>
                            </div>
                            <button class="admin-btn success" onclick="adminForceChannel()">Create Channel</button>
                        </div>
                        <div class="admin-result" id="result-force-channel"></div>
                    </div>
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Season & Week</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-desc">Set the current season and week number displayed on the website.</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>Season</label>
                                <input type="text" class="admin-input" id="set-season" placeholder="e.g. 5" value="{season or ''}">
                            </div>
                            <div class="admin-input-group">
                                <label>Week</label>
                                <input type="text" class="admin-input" id="set-week" placeholder="e.g. 3" value="{week or ''}">
                            </div>
                            <button class="admin-btn primary" onclick="adminSetWeek()">Update</button>
                        </div>
                        <div class="admin-result" id="result-set-week"></div>
                    </div>
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Leaderboard Management</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-header">
                            <span class="admin-row-title">Edit Cast Count</span>
                        </div>
                        <div class="admin-row-desc">Manually set a user's cast count.</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>User ID</label>
                                <input type="text" class="admin-input" id="edit-lb-user" placeholder="Discord User ID">
                            </div>
                            <div class="admin-input-group">
                                <label>Count</label>
                                <input type="number" class="admin-input" id="edit-lb-count" placeholder="0" min="0">
                            </div>
                            <button class="admin-btn primary" onclick="adminEditLeaderboard()">Set Count</button>
                        </div>
                        <div class="admin-result" id="result-edit-lb"></div>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-header">
                            <span class="admin-row-title">Reset Leaderboard</span>
                        </div>
                        <div class="admin-row-desc">Reset all cast counts to zero. This cannot be undone!</div>
                        <div class="admin-form">
                            <button class="admin-btn danger" onclick="adminResetLeaderboard()">Reset All</button>
                        </div>
                        <div class="admin-result" id="result-reset-lb"></div>
                    </div>
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Archive Season</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-desc">Archive the current leaderboard and start a new cycle. This preserves all stats in history.</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>Cycle Name</label>
                                <input type="text" class="admin-input" id="cycle-name" placeholder="e.g. Season 5">
                            </div>
                            <div class="admin-input-group">
                                <label>Weeks</label>
                                <input type="number" class="admin-input" id="cycle-weeks" placeholder="10" min="1" style="width: 80px;">
                            </div>
                            <div class="admin-input-group">
                                <label>Start Date</label>
                                <input type="date" class="admin-input" id="cycle-start">
                            </div>
                            <div class="admin-input-group">
                                <label>End Date</label>
                                <input type="date" class="admin-input" id="cycle-end">
                            </div>
                            <button class="admin-btn success" onclick="adminStartCycle()">Archive & Start</button>
                        </div>
                        <div class="admin-result" id="result-cycle"></div>
                    </div>
                </div>
            </div>
        '''
    else:
        admin_tab_btn = ''
        admin_tab_content = ''
    
    html = (HTML_TEMPLATE
        .replace("{user_bar}", user_bar)
        .replace("{season_badge}", season_badge)
        .replace("{filter_bar}", filter_bar)
        .replace("{admin_tab_btn}", admin_tab_btn)
        .replace("{admin_tab_content}", admin_tab_content)
        .replace("{content}", content)
        .replace("{cycle_selector}", cycle_selector)
        .replace("{cycle_info}", cycle_info)
        .replace("{leaderboard_content}", leaderboard_content)
        .replace("{cycle_history}", cycle_history)
        .replace("{schedule_active}", schedule_active)
        .replace("{leaderboard_active}", leaderboard_active)
        .replace("{schedule_content_active}", schedule_content_active)
        .replace("{leaderboard_content_active}", leaderboard_content_active)
    )
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


async def api_create_channel_handler(request: web.Request) -> web.Response:
    """API endpoint to create private match channel."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    # Check if channel already exists
    if match.get("private_channel_id"):
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            existing = guild.get_channel(match["private_channel_id"])
            if existing:
                return web.json_response({"success": False, "error": "Channel already exists"}, status=400)
        await db.clear_private_channel(match_id)
    
    # Check requirements
    claims = await db.get_claims(match_id)
    casters = [c for c in claims if c["role"] == "caster"]
    camops = [c for c in claims if c["role"] == "camop"]
    if not casters or not camops:
        return web.json_response({"success": False, "error": "Need at least 1 caster and 1 cam op"}, status=400)
    
    # Create channel
    from .views import create_private_match_channel_web
    channel = await create_private_match_channel_web(bot, match, claims)
    if channel:
        await _refresh_discord_message(bot, match_id)
        return web.json_response({"success": True, "channel_id": channel.id})
    else:
        return web.json_response({"success": False, "error": "Failed to create channel"}, status=500)


async def api_crew_ready_handler(request: web.Request) -> web.Response:
    """API endpoint to send crew ready message."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    if not match.get("private_channel_id"):
        return web.json_response({"success": False, "error": "Create the channel first"}, status=400)
    
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    channel = guild.get_channel(match["private_channel_id"])
    if not channel:
        return web.json_response({"success": False, "error": "Channel not found"}, status=404)
    
    # Find team roles
    team_a_lower = match['team_a'].lower()
    team_b_lower = match['team_b'].lower()
    team_pings = []
    for role in guild.roles:
        if role.name.lower().startswith("team:"):
            team_name = role.name[5:].strip().lower()
            if team_name == team_a_lower or team_name == team_b_lower:
                team_pings.append(role.mention)
    
    if team_pings:
        pings = " ".join(team_pings)
        ready_msg = f"{pings}\n\n**The casting crew is ready!** You may start whenever you're ready."
    else:
        ready_msg = f"**{match['team_a']}** and **{match['team_b']}**\n\n**The casting crew is ready!** You may start whenever you're ready."
    
    await channel.send(ready_msg)
    return web.json_response({"success": True})


async def api_go_live_handler(request: web.Request) -> web.Response:
    """API endpoint to post live announcement."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    if not match.get("private_channel_id"):
        return web.json_response({"success": False, "error": "Create the channel first"}, status=400)
    
    claims = await db.get_claims(match_id)
    casters = [c for c in claims if c["role"] == "caster"]
    camops = [c for c in claims if c["role"] == "camop"]
    if not casters or not camops:
        return web.json_response({"success": False, "error": "Need at least 1 caster and 1 cam op"}, status=400)
    
    if not config.LIVE_ANNOUNCEMENT_CHANNEL_ID:
        return web.json_response({"success": False, "error": "Live announcement channel not configured"}, status=500)
    
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    live_channel = guild.get_channel(config.LIVE_ANNOUNCEMENT_CHANNEL_ID)
    if not live_channel:
        return web.json_response({"success": False, "error": "Live announcement channel not found"}, status=404)
    
    # Find team roles
    team_a_lower = match['team_a'].lower()
    team_b_lower = match['team_b'].lower()
    team_mentions = []
    for role in guild.roles:
        if role.name.lower().startswith("team:"):
            team_name = role.name[5:].strip().lower()
            if team_name == team_a_lower:
                team_mentions.append(role.mention)
            elif team_name == team_b_lower:
                team_mentions.append(role.mention)
    
    if len(team_mentions) >= 2:
        teams_text = f"{team_mentions[0]} vs {team_mentions[1]}"
    elif len(team_mentions) == 1:
        if team_a_lower in team_mentions[0].lower():
            teams_text = f"{team_mentions[0]} vs {match['team_b']}"
        else:
            teams_text = f"{match['team_a']} vs {team_mentions[0]}"
    else:
        teams_text = f"{match['team_a']} vs {match['team_b']}"
    
    live_ping = ""
    if config.LIVE_PING_ROLE_ID:
        live_role = guild.get_role(config.LIVE_PING_ROLE_ID)
        if live_role:
            live_ping = live_role.mention
    
    twitch_url = config.TWITCH_URL or "https://www.twitch.tv/echomasterleague"
    announcement = f"# [EchoMasterLeague]({twitch_url}) We are live now casting {teams_text}"
    if live_ping:
        announcement += f"\n{live_ping}"
    
    await live_channel.send(announcement)
    return web.json_response({"success": True})


# Admin API helpers
async def _check_admin(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Check if user is admin. Returns (session, error_response)."""
    session = _get_session(request)
    if not session:
        return None, web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return None, web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    # Check admin role
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return None, web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    member = guild.get_member(session["user_id"])
    if not member:
        return None, web.json_response({"success": False, "error": "Not a guild member"}, status=403)
    
    lead_role_id = config.WEB_LEAD_ROLE_ID
    if not lead_role_id:
        return None, web.json_response({"success": False, "error": "Admin role not configured"}, status=500)
    
    if lead_role_id not in [r.id for r in member.roles]:
        return None, web.json_response({"success": False, "error": "Admin access required"}, status=403)
    
    return session, None


async def api_admin_sync_handler(request: web.Request) -> web.Response:
    """Admin API: Sync matches from Google Sheet."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    try:
        # Import and call sync_matches directly
        from . import bot as bot_module
        count = await bot_module.sync_matches(bot)
        return web.json_response({"success": True, "message": f"Synced {count} new matches"})
    except Exception as e:
        log.error(f"Admin sync failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_refresh_handler(request: web.Request) -> web.Response:
    """Admin API: Refresh all claim messages."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    try:
        from .views import ClaimView
        matches = await db.get_matches_with_message()
        channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
        if not channel:
            return web.json_response({"success": False, "error": "Claim channel not found"}, status=500)
        
        updated = 0
        for match in matches:
            if not match.get("message_id"):
                continue
            try:
                msg = await channel.fetch_message(match["message_id"])
                claims = await db.get_claims(match["match_id"])
                new_view = ClaimView(match["match_id"], match, claims)
                await msg.edit(view=new_view)
                updated += 1
            except Exception as e:
                log.error(f"Failed to refresh message for {match['match_id']}: {e}")
        
        return web.json_response({"success": True, "message": f"Refreshed {updated} messages"})
    except Exception as e:
        log.error(f"Admin refresh failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_force_channel_handler(request: web.Request) -> web.Response:
    """Admin API: Force create private channel for a match."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    if match.get("private_channel_id"):
        return web.json_response({"success": False, "error": "Channel already exists"}, status=400)
    
    try:
        from .views import create_private_match_channel_web
        claims = await db.get_claims(match_id)
        channel = await create_private_match_channel_web(bot, match, claims)
        if channel:
            return web.json_response({"success": True, "message": f"Created channel #{channel.name}"})
        else:
            return web.json_response({"success": False, "error": "Failed to create channel"}, status=500)
    except Exception as e:
        log.error(f"Admin force channel failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_set_week_handler(request: web.Request) -> web.Response:
    """Admin API: Set current season/week."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    season = data.get("season")
    week = data.get("week")
    
    if not season or not week:
        return web.json_response({"success": False, "error": "Missing season or week"}, status=400)
    
    try:
        await db.set_setting("current_season", str(season))
        await db.set_setting("current_week", str(week))
        return web.json_response({"success": True, "message": f"Updated to Season {season} Week {week}"})
    except Exception as e:
        log.error(f"Admin set week failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_edit_leaderboard_handler(request: web.Request) -> web.Response:
    """Admin API: Edit a user's cast count."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    user_id = data.get("user_id")
    count = data.get("count")
    
    if not user_id:
        return web.json_response({"success": False, "error": "Missing user_id"}, status=400)
    if count is None:
        return web.json_response({"success": False, "error": "Missing count"}, status=400)
    
    try:
        user_id_int = int(user_id)
        count_int = int(count)
        await db.set_cast_count(user_id_int, count_int)
        return web.json_response({"success": True, "message": f"Set cast count to {count_int}"})
    except ValueError:
        return web.json_response({"success": False, "error": "Invalid user_id or count"}, status=400)
    except Exception as e:
        log.error(f"Admin edit leaderboard failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_reset_leaderboard_handler(request: web.Request) -> web.Response:
    """Admin API: Reset all cast counts to zero."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        count = await db.reset_leaderboard()
        return web.json_response({"success": True, "message": f"Reset {count} entries"})
    except Exception as e:
        log.error(f"Admin reset leaderboard failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_start_cycle_handler(request: web.Request) -> web.Response:
    """Admin API: Archive current leaderboard and start new cycle."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    name = data.get("name")
    if not name:
        return web.json_response({"success": False, "error": "Missing cycle name"}, status=400)
    
    weeks = data.get("weeks") or 0
    start_date = data.get("start_date") or ""
    end_date = data.get("end_date") or ""
    
    try:
        cycle_id = await db.archive_cycle(name, weeks, start_date, end_date)
        return web.json_response({"success": True, "message": f"Archived as '{name}' (cycle #{cycle_id})"})
    except Exception as e:
        log.error(f"Admin start cycle failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.Response(text="OK")


async def manifest_handler(request: web.Request) -> web.Response:
    """Serve PWA manifest."""
    manifest = {
        "name": "EML Broadcast Hub",
        "short_name": "EML Caster",
        "description": "Echo Master League Broadcast Hub - Claim matches and manage casts",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a12",
        "theme_color": "#ff6a00",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    return web.json_response(manifest)


async def service_worker_handler(request: web.Request) -> web.Response:
    """Serve service worker for PWA."""
    sw_code = """
const CACHE_NAME = 'eml-caster-v1';
const STATIC_ASSETS = [
    'https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Rajdhani:wght@400;500;600;700&display=swap'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => 
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    // Network-first for API calls and main page
    if (event.request.url.includes('/api/') || event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(() => caches.match(event.request))
        );
        return;
    }
    // Cache-first for static assets
    event.respondWith(
        caches.match(event.request).then(cached => cached || fetch(event.request))
    );
});
"""
    return web.Response(text=sw_code.strip(), content_type="application/javascript")


async def icon_handler(request: web.Request) -> web.Response:
    """Generate PNG icon dynamically."""
    import base64
    import struct
    import zlib
    
    # Determine size from path
    size = 512 if "512" in request.path else 192
    
    # Generate a simple PNG with EML logo (orange circle with E)
    # This creates a minimal valid PNG
    def create_png(width, height):
        # Create RGBA pixel data
        pixels = []
        center_x, center_y = width // 2, height // 2
        radius = min(width, height) // 2 - 4
        
        for y in range(height):
            row = []
            for x in range(width):
                # Distance from center
                dx, dy = x - center_x, y - center_y
                dist = (dx * dx + dy * dy) ** 0.5
                
                if dist <= radius:
                    # Inside circle - orange (#ff6a00)
                    # Check if we should draw the "E"
                    rel_x = (x - center_x) / radius
                    rel_y = (y - center_y) / radius
                    
                    # Draw E shape
                    e_left = -0.5
                    e_right = 0.4
                    e_top = -0.55
                    e_bottom = 0.55
                    bar_height = 0.12
                    
                    is_e = False
                    # Left vertical bar
                    if e_left <= rel_x <= e_left + 0.2 and e_top <= rel_y <= e_bottom:
                        is_e = True
                    # Top horizontal bar
                    if e_left <= rel_x <= e_right and e_top <= rel_y <= e_top + bar_height:
                        is_e = True
                    # Middle horizontal bar
                    if e_left <= rel_x <= e_right - 0.1 and -bar_height/2 <= rel_y <= bar_height/2:
                        is_e = True
                    # Bottom horizontal bar
                    if e_left <= rel_x <= e_right and e_bottom - bar_height <= rel_y <= e_bottom:
                        is_e = True
                    
                    if is_e:
                        row.extend([10, 10, 18, 255])  # Dark background for E
                    else:
                        row.extend([255, 106, 0, 255])  # Orange
                else:
                    # Outside circle - transparent
                    row.extend([0, 0, 0, 0])
            pixels.append(bytes(row))
        
        # Create PNG
        def png_chunk(chunk_type, data):
            chunk = chunk_type + data
            return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)
        
        # PNG signature
        png = b'\x89PNG\r\n\x1a\n'
        
        # IHDR chunk
        ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
        png += png_chunk(b'IHDR', ihdr_data)
        
        # IDAT chunk (compressed pixel data)
        raw_data = b''
        for row in pixels:
            raw_data += b'\x00' + row  # Filter byte + row data
        compressed = zlib.compress(raw_data, 9)
        png += png_chunk(b'IDAT', compressed)
        
        # IEND chunk
        png += png_chunk(b'IEND', b'')
        
        return png
    
    png_data = create_png(size, size)
    return web.Response(body=png_data, content_type="image/png")


async def leaderboard_handler(request: web.Request) -> web.Response:
    """Redirect to main page with leaderboard tab active."""
    raise web.HTTPFound("/?tab=leaderboard")


def create_app(bot=None) -> web.Application:
    """Create the aiohttp web application."""
    app = web.Application()
    app["bot"] = bot
    
    app.router.add_get("/", schedule_handler)
    app.router.add_get("/schedule", schedule_handler)
    app.router.add_get("/leaderboard", leaderboard_handler)
    app.router.add_get("/login", login_handler)
    app.router.add_get("/callback", callback_handler)
    app.router.add_get("/logout", logout_handler)
    app.router.add_post("/api/claim", api_claim_handler)
    app.router.add_post("/api/unclaim", api_unclaim_handler)
    app.router.add_post("/api/create_channel", api_create_channel_handler)
    app.router.add_post("/api/crew_ready", api_crew_ready_handler)
    app.router.add_post("/api/go_live", api_go_live_handler)
    # Admin API routes
    app.router.add_post("/api/admin/sync", api_admin_sync_handler)
    app.router.add_post("/api/admin/refresh", api_admin_refresh_handler)
    app.router.add_post("/api/admin/force-channel", api_admin_force_channel_handler)
    app.router.add_post("/api/admin/set-week", api_admin_set_week_handler)
    app.router.add_post("/api/admin/edit-leaderboard", api_admin_edit_leaderboard_handler)
    app.router.add_post("/api/admin/reset-leaderboard", api_admin_reset_leaderboard_handler)
    app.router.add_post("/api/admin/start-cycle", api_admin_start_cycle_handler)
    app.router.add_get("/health", health_handler)
    # PWA routes
    app.router.add_get("/manifest.json", manifest_handler)
    app.router.add_get("/sw.js", service_worker_handler)
    app.router.add_get("/icon-192.png", icon_handler)
    app.router.add_get("/icon-512.png", icon_handler)
    
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
