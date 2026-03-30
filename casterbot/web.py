"""Optional web server to display claim status with Discord OAuth2."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from dateutil import tz as dateutil_tz

from . import config, db

log = logging.getLogger("casterbot.web")

# Simple in-memory session store
_sessions: dict[str, dict] = {}

# Track messages sent via web UI (message_id -> sender info)
# This allows us to show the web sender's name in the chat UI
_web_sent_messages: dict[str, dict] = {}

import re
import json
import asyncio

def _convert_mentions_to_names(content: str, guild) -> str:
    """Convert Discord mention syntax to readable names for web display."""
    if not guild:
        return content
    
    # Convert user mentions <@123456789> or <@!123456789>
    def replace_user_mention(match):
        user_id = int(match.group(1))
        member = guild.get_member(user_id)
        if member:
            return f"@{member.display_name}"
        return match.group(0)
    
    content = re.sub(r'<@!?(\d+)>', replace_user_mention, content)
    
    # Convert role mentions <@&123456789>
    def replace_role_mention(match):
        role_id = int(match.group(1))
        role = guild.get_role(role_id)
        if role:
            return f"@{role.name}"
        return match.group(0)
    
    content = re.sub(r'<@&(\d+)>', replace_role_mention, content)
    
    # Convert channel mentions <#123456789>
    def replace_channel_mention(match):
        channel_id = int(match.group(1))
        channel = guild.get_channel(channel_id)
        if channel:
            return f"#{channel.name}"
        return match.group(0)
    
    content = re.sub(r'<#(\d+)>', replace_channel_mention, content)
    
    return content


def _get_session(request: web.Request) -> dict | None:
    """Get the current user's session."""
    session_id = request.cookies.get("session")
    if session_id and session_id in _sessions:
        return _sessions[session_id]
    return None


async def _is_admin(bot, user_id: int) -> bool:
    """Check if a user has the lead role (admin access)."""
    if not bot or not (config.WEB_LEAD_ROLE_ID or config.STAFF_ROLE_ID) or not config.GUILD_ID:
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
        allowed_admin_roles = [r for r in [config.WEB_LEAD_ROLE_ID, config.STAFF_ROLE_ID] if r]
        return any(role.id in allowed_admin_roles for role in member.roles)
    except Exception:
        return False


async def _is_crew_member(bot, user_id: int) -> bool:
    """Check if a user has any caster/camop role (including training)."""
    if not bot or not config.GUILD_ID:
        return False
    
    # Get all allowed role IDs
    allowed_roles = set()
    if config.CASTER_ROLE_ID:
        allowed_roles.add(config.CASTER_ROLE_ID)
    if config.CAMOP_ROLE_ID:
        allowed_roles.add(config.CAMOP_ROLE_ID)
    if config.CASTER_TRAINING_ROLE_ID:
        allowed_roles.add(config.CASTER_TRAINING_ROLE_ID)
    if config.CAMOP_TRAINING_ROLE_ID:
        allowed_roles.add(config.CAMOP_TRAINING_ROLE_ID)
    if config.WEB_LEAD_ROLE_ID:
        allowed_roles.add(config.WEB_LEAD_ROLE_ID)
    if config.STAFF_ROLE_ID:
        allowed_roles.add(config.STAFF_ROLE_ID)
    
    # If no roles configured, deny access
    if not allowed_roles:
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
        
        return any(role.id in allowed_roles for role in member.roles)
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
    <meta name="description" content="{league_name} Broadcast Hub - Claim matches and manage casts">
    
    <!-- Discord/Social Embed -->
    <meta property="og:type" content="website">
    <meta property="og:url" content="{site_url}">
    <meta property="og:title" content="Broadcast Hub">
    <meta property="og:description" content="Claim matches and manage casts for {league_name}">
    <meta property="og:site_name" content="{league_name}">
    <meta name="theme-color" content="#ff6a00">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="Broadcast Hub">
    <meta name="twitter:description" content="Claim matches and manage casts for {league_name}">
    
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Rajdhani:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --echo-orange: #ff6a00;
            --echo-orange-glow: rgba(255, 106, 0, 0.6);
            --echo-cyan: #00d4ff;
            --echo-cyan-glow: rgba(0, 212, 255, 0.6);
            --echo-blue: #1a4fff;
            --echo-dark: #0a0a12;
            --echo-darker: #050508;
            --echo-panel: rgba(15, 15, 25, 0.97);
            --echo-border: rgba(255, 106, 0, 0.5);
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
        .container { max-width: 950px; margin: 0 auto; padding-left: 300px; padding-top: 100px; }
        .top-bar {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 150;
            background: linear-gradient(180deg, var(--echo-darker) 0%, var(--echo-dark) 100%);
            border-bottom: 1px solid var(--echo-border);
            padding: 12px 30px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }
        .top-bar-inner {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
        }
        .header {
            text-align: left;
            position: relative;
            flex-shrink: 0;
        }
        .header::before {
            display: none;
        }
        h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 1.6em;
            font-weight: 900;
            letter-spacing: 3px;
            text-transform: uppercase;
            background: linear-gradient(180deg, var(--echo-orange) 0%, #ff8533 50%, var(--echo-orange) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            text-shadow: 0 0 40px var(--echo-orange-glow);
            position: relative;
            margin: 0;
            line-height: 1.2;
        }
        .subtitle {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            font-size: 0.7em;
            letter-spacing: 2px;
            text-transform: uppercase;
            margin-top: 4px;
        }
        .season-badge {
            display: inline-block;
            margin-top: 8px;
            padding: 5px 14px;
            background: linear-gradient(135deg, rgba(255,106,0,0.2), rgba(0,212,255,0.2));
            border: 1px solid var(--echo-orange);
            border-radius: 15px;
            font-family: 'Orbitron', sans-serif;
            font-size: 0.75em;
            font-weight: 600;
            letter-spacing: 1px;
            color: var(--echo-text);
            box-shadow: 0 0 10px var(--echo-orange-glow);
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
            display: flex; justify-content: flex-end; align-items: center; gap: 15px;
            padding: 0;
            background: transparent;
            border: none;
            border-radius: 0;
            box-shadow: none;
            flex-shrink: 0;
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
        .user-bar .refresh-btn {
            background: transparent;
            border: 1px solid var(--echo-orange);
            color: var(--echo-orange);
            padding: 8px 16px;
            font-family: 'Orbitron', sans-serif;
            font-size: 0.8em;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 1px;
            border-radius: 4px;
            margin-left: auto;
        }
        .user-bar .refresh-btn:hover {
            background: rgba(255,106,0,0.2);
            box-shadow: 0 0 15px rgba(255,106,0,0.3);
        }
        .profile-menu-container {
            position: relative;
        }
        .profile-menu-trigger {
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .profile-menu-trigger:hover .user-avatar {
            box-shadow: 0 0 20px var(--echo-orange-glow);
        }
        .profile-dropdown {
            position: absolute;
            top: 100%;
            right: 0;
            margin-top: 8px;
            background: var(--echo-panel);
            border: 1px solid var(--echo-border);
            border-radius: 8px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            min-width: 180px;
            z-index: 1000;
            display: none;
            overflow: hidden;
        }
        .profile-dropdown.show {
            display: block;
        }
        .profile-dropdown-item {
            display: block;
            padding: 12px 16px;
            color: var(--echo-text);
            text-decoration: none;
            font-size: 0.9em;
            transition: background 0.2s;
            cursor: pointer;
            border: none;
            background: transparent;
            width: 100%;
            text-align: left;
        }
        .profile-dropdown-item:hover {
            background: rgba(255,106,0,0.2);
        }
        .profile-dropdown-divider {
            height: 1px;
            background: var(--echo-border);
            margin: 4px 0;
        }
        .profile-pic-input {
            display: none;
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
        .assign-btn {
            background: linear-gradient(180deg, var(--echo-cyan) 0%, #00a8cc 100%);
            color: #001a1a;
            box-shadow: 0 0 10px rgba(0,212,255,0.3);
        }
        .assign-btn:hover {
            box-shadow: 0 0 20px rgba(0,212,255,0.5);
            transform: translateY(-1px);
        }
        .slot-buttons {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        .assign-modal {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.9);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }
        .assign-modal-box {
            background: #12121a;
            border: 2px solid var(--echo-orange);
            border-radius: 12px;
            padding: 24px;
            min-width: 320px;
            max-width: 90vw;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 0 30px rgba(0,0,0,0.8), 0 0 20px var(--echo-orange-glow);
        }
        .assign-modal-box h3 {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            margin-bottom: 16px;
        }
        .assign-modal-box p {
            color: var(--echo-text-dim);
            margin-bottom: 16px;
            font-size: 0.9em;
        }
        .assign-user-list {
            max-height: 300px;
            overflow-y: auto;
            margin-bottom: 16px;
        }
        .assign-user-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px;
            background: #1a1a24;
            border: 1px solid var(--echo-border);
            border-radius: 8px;
            margin-bottom: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .assign-user-item:hover {
            background: #252535;
            border-color: var(--echo-cyan);
        }
        .assign-user-item img {
            width: 32px;
            height: 32px;
            border-radius: 50%;
        }
        .assign-user-item span {
            flex: 1;
        }
        .assign-modal-close {
            background: #2a2a3a;
            color: var(--echo-text);
            border: 1px solid var(--echo-border);
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            width: 100%;
        }
        .assign-modal-close:hover {
            background: #3a3a4a;
            border-color: var(--echo-orange);
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
        .stream-select {
            padding: 10px 16px;
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--echo-orange);
            border-radius: 4px;
            color: var(--echo-orange);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.85em;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.3s;
            min-width: 180px;
            margin-right: 10px;
        }
        .stream-select:hover {
            background: rgba(255,106,0,0.1);
            box-shadow: 0 0 10px var(--echo-orange-glow);
        }
        .stream-select:focus {
            outline: none;
            border-color: var(--echo-orange);
            box-shadow: 0 0 15px var(--echo-orange-glow);
        }
        .stream-select option {
            background: #1a1a2e;
            color: var(--echo-text);
            padding: 10px;
        }
        .stream-select.warning {
            border-color: var(--echo-danger);
            animation: streamWarning 1s infinite;
        }
        @keyframes streamWarning {
            0%, 100% { border-color: var(--echo-danger); box-shadow: 0 0 10px rgba(255,51,102,0.3); }
            50% { border-color: var(--echo-orange); box-shadow: 0 0 5px var(--echo-orange-glow); }
        }
        .stream-row {
            display: flex;
            align-items: center;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: 10px;
        }
        .stream-label {
            color: var(--echo-text-dim);
            font-size: 0.85em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        /* Chat Styles */
        .chat-toggle-btn {
            padding: 10px 18px;
            background: linear-gradient(180deg, #5865F2 0%, #4752C4 100%);
            border: none;
            border-radius: 4px;
            color: white;
            font-size: 0.85em;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-family: 'Rajdhani', sans-serif;
            box-shadow: 0 0 10px rgba(88,101,242,0.3);
        }
        .chat-toggle-btn:hover {
            box-shadow: 0 0 20px rgba(88,101,242,0.5);
        }
        .chat-messages {
            background: rgba(0,0,0,0.4);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            height: 300px;
            overflow-y: auto;
            padding: 16px;
            margin-bottom: 12px;
        }
        .chat-messages::-webkit-scrollbar {
            width: 6px;
        }
        .chat-messages::-webkit-scrollbar-track {
            background: transparent;
        }
        .chat-messages::-webkit-scrollbar-thumb {
            background: var(--echo-border);
            border-radius: 3px;
        }
        .chat-msg {
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            padding: 8px 0;
            border-bottom: 1px solid rgba(255,255,255,0.03);
        }
        .chat-msg:last-child {
            border-bottom: none;
            margin-bottom: 0;
        }
        .chat-msg-continued {
            padding-left: 52px;
            margin-bottom: 6px;
            padding-top: 0;
            border-bottom: none;
        }
        .chat-msg-bot {
            background: rgba(88,101,242,0.1);
            border-radius: 8px;
            padding: 10px 12px;
            margin: 4px -8px;
            border-left: 3px solid rgba(88,101,242,0.5);
        }
        .chat-avatar {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            flex-shrink: 0;
            border: 2px solid rgba(255,255,255,0.1);
        }
        .chat-content {
            flex: 1;
            min-width: 0;
        }
        .chat-header {
            display: flex;
            align-items: baseline;
            gap: 10px;
            margin-bottom: 6px;
        }
        .chat-author {
            font-weight: 700;
            color: var(--echo-cyan);
            font-size: 1em;
            letter-spacing: 0.3px;
        }
        .chat-time {
            font-size: 0.8em;
            color: var(--echo-text-dim);
            font-weight: 500;
        }
        .chat-text {
            color: var(--echo-text);
            word-wrap: break-word;
            line-height: 1.5;
            font-size: 1em;
        }
        .chat-input-row {
            display: flex;
            gap: 10px;
        }
        .chat-input {
            flex: 1;
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--echo-border);
            border-radius: 6px;
            padding: 12px 14px;
            color: var(--echo-text);
            font-family: 'Rajdhani', sans-serif;
            font-size: 1em;
        }
        .chat-input:focus {
            outline: none;
            border-color: var(--echo-cyan);
            box-shadow: 0 0 10px var(--echo-cyan-glow);
        }
        .chat-input::placeholder {
            color: var(--echo-text-dim);
        }
        .chat-send-btn {
            padding: 12px 20px;
            background: linear-gradient(180deg, var(--echo-cyan) 0%, #00a8cc 100%);
            border: none;
            border-radius: 6px;
            color: #001a1a;
            font-weight: 700;
            font-family: 'Rajdhani', sans-serif;
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.3s;
        }
        .chat-send-btn:hover {
            box-shadow: 0 0 15px var(--echo-cyan-glow);
        }
        .chat-send-btn:disabled {
            background: rgba(255,255,255,0.1);
            color: var(--echo-text-dim);
            cursor: not-allowed;
        }
        .chat-info, .chat-error {
            text-align: center;
            padding: 40px 20px;
            color: var(--echo-text-dim);
            font-style: italic;
        }
        .chat-error {
            color: var(--echo-danger);
        }
        .chat-input-container {
            position: relative;
        }
        .mention-suggestions {
            display: none;
            position: absolute;
            bottom: 100%;
            left: 0;
            right: 0;
            background: rgba(20,20,28,0.98);
            border: 1px solid var(--echo-border);
            border-bottom: none;
            border-radius: 6px 6px 0 0;
            max-height: 200px;
            overflow-y: auto;
            z-index: 10;
        }
        .mention-suggestion {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 12px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .mention-suggestion:hover {
            background: rgba(0,255,255,0.1);
        }
        .mention-avatar {
            width: 24px;
            height: 24px;
            border-radius: 50%;
        }
        .mention-role-badge {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 14px;
            color: white;
        }
        .mention-role {
            background: rgba(255,255,255,0.03);
        }
        .chat-mention {
            background: rgba(0,255,255,0.15);
            color: var(--echo-cyan);
            padding: 1px 4px;
            border-radius: 3px;
        }
        .chat-date-separator {
            display: flex;
            align-items: center;
            margin: 20px 0 16px 0;
            padding: 0;
        }
        .chat-date-separator::before,
        .chat-date-separator::after {
            content: '';
            flex: 1;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.15), transparent);
        }
        .chat-date-separator span {
            padding: 4px 16px;
            font-size: 0.75em;
            font-weight: 600;
            color: var(--echo-text-dim);
            text-transform: uppercase;
            letter-spacing: 1px;
            background: rgba(255,106,0,0.1);
            border-radius: 12px;
            border: 1px solid rgba(255,106,0,0.2);
        }
        /* Chat Reply Styles */
        .chat-msg-actions {
            display: none;
            position: absolute;
            top: -8px;
            right: 10px;
            background: var(--echo-panel);
            border: 1px solid var(--echo-border);
            border-radius: 4px;
            padding: 2px;
            z-index: 5;
        }
        .chat-msg:hover .chat-msg-actions {
            display: flex;
        }
        .chat-action-btn {
            background: transparent;
            border: none;
            color: var(--echo-text-dim);
            padding: 4px 8px;
            cursor: pointer;
            font-size: 0.85em;
            transition: all 0.2s;
            border-radius: 3px;
        }
        .chat-action-btn:hover {
            background: rgba(0,212,255,0.2);
            color: var(--echo-cyan);
        }
        .chat-reply-preview {
            display: none;
            background: rgba(0,212,255,0.1);
            border-left: 3px solid var(--echo-cyan);
            padding: 8px 12px;
            margin-bottom: 8px;
            border-radius: 0 6px 6px 0;
            position: relative;
        }
        .chat-reply-preview.active {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .chat-reply-preview-content {
            flex: 1;
            min-width: 0;
        }
        .chat-reply-preview-author {
            font-weight: 600;
            color: var(--echo-cyan);
            font-size: 0.85em;
            margin-bottom: 2px;
        }
        .chat-reply-preview-text {
            color: var(--echo-text-dim);
            font-size: 0.85em;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .chat-reply-cancel {
            background: transparent;
            border: none;
            color: var(--echo-text-dim);
            cursor: pointer;
            padding: 4px 8px;
            font-size: 1.1em;
            transition: color 0.2s;
        }
        .chat-reply-cancel:hover {
            color: var(--echo-danger);
        }
        .chat-msg-reply-ref {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 4px 8px;
            margin-bottom: 6px;
            background: rgba(0,212,255,0.05);
            border-left: 2px solid var(--echo-cyan);
            border-radius: 0 4px 4px 0;
            font-size: 0.85em;
            cursor: pointer;
            transition: background 0.2s;
        }
        .chat-msg-reply-ref:hover {
            background: rgba(0,212,255,0.1);
        }
        .chat-msg-reply-ref-icon {
            color: var(--echo-cyan);
        }
        .chat-msg-reply-ref-author {
            font-weight: 600;
            color: var(--echo-cyan);
        }
        .chat-msg-reply-ref-text {
            color: var(--echo-text-dim);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            flex: 1;
            min-width: 0;
        }
        .chat-msg-wrapper {
            position: relative;
        }
        .chat-image {
            max-width: 100%;
            max-height: 300px;
            border-radius: 8px;
            margin-top: 8px;
            cursor: pointer;
            transition: transform 0.2s;
            display: block;
        }
        .chat-image:hover {
            transform: scale(1.02);
        }
        .chat-image-link {
            display: block;
            margin-top: 8px;
        }
        .chat-images-container {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 8px;
        }
        .chat-images-container .chat-image {
            margin-top: 0;
        }
        /* Image lightbox */
        .image-lightbox {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.9);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 2000;
            cursor: zoom-out;
        }
        .image-lightbox img {
            max-width: 95%;
            max-height: 95%;
            object-fit: contain;
            border-radius: 8px;
        }
        /* Chats Tab Styles */
        .chats-header {
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid rgba(255,106,0,0.2);
        }
        .chats-header h3 {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-orange);
            font-size: 1.3em;
            margin: 0 0 5px 0;
        }
        .chats-subtitle {
            color: var(--echo-text-dim);
            font-size: 0.9em;
            margin: 0;
        }
        .chat-group {
            background: var(--echo-card);
            border: 1px solid var(--echo-border);
            border-radius: 8px;
            margin-bottom: 16px;
            overflow: hidden;
        }
        .chat-group-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            background: rgba(255,106,0,0.05);
            border-bottom: 1px solid var(--echo-border);
            cursor: pointer;
            transition: background 0.2s;
        }
        .chat-group-header:hover {
            background: rgba(255,106,0,0.1);
        }
        .chat-group-title {
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-cyan);
            font-size: 0.95em;
            letter-spacing: 1px;
        }
        .chat-group-meta {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .chat-group-time {
            font-size: 0.8em;
            color: var(--echo-text-dim);
        }
        .chat-group-expand {
            color: var(--echo-orange);
            font-size: 1.2em;
            transition: transform 0.3s;
        }
        .chat-group.expanded .chat-group-expand {
            transform: rotate(180deg);
        }
        .chat-group-body {
            display: none;
            padding: 0;
        }
        .chat-group.expanded .chat-group-body {
            display: block;
        }
        .chat-group .chat-messages {
            border-radius: 0;
            border: none;
            border-top: 1px solid rgba(255,255,255,0.05);
            margin: 0;
            height: 350px;
        }
        .chat-group .chat-input-container {
            padding: 12px 16px;
            background: rgba(0,0,0,0.2);
        }
        .chats-empty {
            text-align: center;
            padding: 40px 20px;
            color: var(--echo-text-dim);
        }
        .chats-empty-icon {
            font-size: 3em;
            margin-bottom: 15px;
            opacity: 0.5;
        }
        .channel-status {
            font-size: 0.75em;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .channel-status.exists {
            background: rgba(0,255,136,0.15);
            color: var(--echo-success);
        }
        .channel-status.pending {
            background: rgba(255,106,0,0.15);
            color: var(--echo-orange);
        }
        .broadcast-controls-inline {
            padding: 16px;
            background: rgba(0,0,0,0.2);
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .broadcast-controls-inline .stream-row {
            display: flex;
            align-items: center;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: 10px;
        }
        .broadcast-controls-inline .stream-label {
            color: var(--echo-text-dim);
            font-size: 0.85em;
            min-width: 60px;
        }
        .broadcast-controls-inline .stream-select {
            flex: 1;
            min-width: 150px;
            max-width: 250px;
        }
        .broadcast-controls-inline .broadcast-btns {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }
        .chat-section {
            padding: 0;
        }
        .chat-section .chat-messages {
            border-radius: 0;
            border: none;
            margin: 0;
            height: 350px;
        }
        .chat-section .chat-input-container {
            padding: 12px 16px;
            background: rgba(0,0,0,0.2);
        }
        .chat-section-placeholder {
            padding: 30px 20px;
            text-align: center;
            color: var(--echo-text-dim);
            background: rgba(0,0,0,0.1);
        }
        .chat-section-placeholder p {
            margin: 0;
            font-size: 0.9em;
        }
        /* Match tab panel styles */
        .match-tab-content {
            padding: 0;
        }
        .match-panel-header {
            padding: 20px;
            background: rgba(255,106,0,0.05);
            border-bottom: 1px solid var(--echo-border);
        }
        .match-panel-header h2 {
            margin: 0 0 6px 0;
            font-family: 'Orbitron', sans-serif;
            font-size: 1.3em;
            color: var(--echo-cyan);
            letter-spacing: 1px;
        }
        .match-panel-time {
            margin: 0;
            color: var(--echo-text-dim);
            font-size: 0.9em;
        }
        .match-panel-controls {
            padding: 16px 20px;
            background: rgba(0,0,0,0.2);
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .match-panel-controls .stream-row {
            display: flex;
            align-items: center;
            margin-bottom: 12px;
            gap: 10px;
            flex-wrap: wrap;
        }
        .match-panel-controls .stream-label {
            color: var(--echo-text-dim);
            font-size: 0.85em;
            min-width: 60px;
        }
        .match-panel-controls .stream-select {
            flex: 1;
            min-width: 150px;
            max-width: 280px;
        }
        .match-panel-controls .broadcast-btns {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }
        .match-chat-section {
            display: flex;
            flex-direction: column;
            height: calc(100vh - 320px);
            min-height: 350px;
        }
        .match-chat-section .chat-messages {
            flex: 1;
            border: none;
            border-radius: 0;
            margin: 0;
            min-height: 250px;
            padding: 20px;
            font-size: 1.05em;
        }
        .match-chat-section .chat-msg {
            margin-bottom: 18px;
            padding: 10px 0;
        }
        .match-chat-section .chat-avatar {
            width: 44px;
            height: 44px;
        }
        .match-chat-section .chat-author {
            font-size: 1.05em;
        }
        .match-chat-section .chat-text {
            font-size: 1.05em;
            line-height: 1.6;
        }
        .match-chat-section .chat-input-container {
            padding: 16px 20px;
            background: rgba(0,0,0,0.3);
            border-top: 1px solid rgba(255,255,255,0.08);
        }
        .match-chat-section .chat-input {
            font-size: 1.05em;
            padding: 14px 16px;
        }
        .match-no-chat {
            padding: 40px 20px;
            text-align: center;
            color: var(--echo-text-dim);
            background: rgba(0,0,0,0.1);
        }
        .match-no-chat p {
            margin: 0;
            font-size: 0.95em;
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
        /* Sidebar Navigation */
        .sidebar {
            position: fixed;
            top: 100px;
            left: 0;
            width: 280px;
            height: calc(100vh - 100px);
            background: var(--echo-panel);
            border-right: 1px solid var(--echo-border);
            z-index: 100;
            display: flex;
            flex-direction: column;
            padding-top: 10px;
        }
        .sidebar-header {
            padding: 15px 20px;
            border-bottom: 1px solid var(--echo-border);
            margin-bottom: 10px;
        }
        .sidebar-header h2 {
            font-family: 'Orbitron', sans-serif;
            font-size: 0.75em;
            color: var(--echo-orange);
            letter-spacing: 2px;
            margin: 0;
        }
        .sidebar-overlay {
            display: none;
        }
        .sidebar-toggle {
            display: none;
        }
        .tabs {
            display: flex;
            flex-direction: column;
            gap: 0;
        }
        .tab-btn {
            padding: 20px 24px;
            background: transparent;
            border: none;
            border-left: 4px solid transparent;
            color: var(--echo-text-dim);
            font-family: 'Rajdhani', sans-serif;
            font-size: 1.2em;
            font-weight: 600;
            letter-spacing: 1px;
            text-transform: uppercase;
            text-align: left;
            cursor: pointer;
            transition: all 0.3s;
        }
        .tab-btn:hover {
            color: var(--echo-text);
            background: rgba(255,255,255,0.05);
        }
        .tab-btn.active {
            color: var(--echo-orange);
            border-left-color: var(--echo-orange);
            background: rgba(255,106,0,0.1);
        }
        .tab-btn.active::after {
            display: none;
        }
        /* Tab category (collapsible group) */
        .tab-category {
            border-top: 1px solid rgba(255,255,255,0.05);
        }
        .tab-category-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 24px;
            color: var(--echo-text-dim);
            font-family: 'Rajdhani', sans-serif;
            font-size: 0.9em;
            font-weight: 600;
            letter-spacing: 2px;
            text-transform: uppercase;
            cursor: pointer;
            transition: all 0.3s;
            background: rgba(0,0,0,0.2);
        }
        .tab-category-header:hover {
            color: var(--echo-text);
            background: rgba(255,255,255,0.03);
        }
        .tab-category-arrow {
            font-size: 0.8em;
            transition: transform 0.3s;
        }
        .tab-category.expanded .tab-category-arrow {
            transform: rotate(180deg);
        }
        .tab-category-items {
            display: none;
            padding-left: 12px;
            max-height: 300px;
            overflow-y: auto;
        }
        .tab-category.expanded .tab-category-items {
            display: block;
        }
        .tab-category-items::-webkit-scrollbar {
            width: 4px;
        }
        .tab-category-items::-webkit-scrollbar-track {
            background: transparent;
        }
        .tab-category-items::-webkit-scrollbar-thumb {
            background: var(--echo-border);
            border-radius: 2px;
        }
        .tab-match-btn {
            display: block;
            width: 100%;
            padding: 12px 20px;
            background: transparent;
            border: none;
            border-left: 3px solid transparent;
            color: var(--echo-text-dim);
            font-family: 'Rajdhani', sans-serif;
            font-size: 0.95em;
            font-weight: 500;
            text-align: left;
            cursor: pointer;
            transition: all 0.3s;
        }
        .tab-match-btn:hover {
            color: var(--echo-text);
            background: rgba(255,255,255,0.03);
        }
        .tab-match-btn.active {
            color: var(--echo-cyan);
            border-left-color: var(--echo-cyan);
            background: rgba(0,212,255,0.08);
        }
        .tab-match-btn .match-label {
            display: block;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .tab-match-btn .match-time-label {
            display: block;
            font-size: 0.75em;
            color: var(--echo-text-dim);
            margin-top: 2px;
        }
        .tab-match-btn .match-status-dot {
            display: inline-block;
            width: 6px;
            height: 6px;
            border-radius: 50%;
            margin-right: 6px;
        }
        .tab-match-btn .match-status-dot.has-channel {
            background: var(--echo-success);
        }
        .tab-match-btn .match-status-dot.no-channel {
            background: var(--echo-orange);
        }
        .tab-category-empty {
            padding: 12px 20px;
            color: var(--echo-text-dim);
            font-size: 0.85em;
            font-style: italic;
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }
        /* Compact Filter Bar - Inline with icon toggle */
        .filter-bar {
            margin-bottom: 16px;
            display: flex;
            gap: 8px;
            align-items: center;
        }
        .filter-toggle {
            display: none;
            padding: 8px 12px;
            background: rgba(0,212,255,0.1);
            border: 1px solid var(--echo-cyan);
            border-radius: 4px;
            color: var(--echo-cyan);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.75em;
            cursor: pointer;
            transition: all 0.3s;
        }
        .filter-toggle:hover {
            background: rgba(0,212,255,0.2);
        }
        .filter-toggle.active {
            background: var(--echo-cyan);
            color: var(--echo-dark);
        }
        .filter-controls {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
            flex: 1;
        }
        .filter-btn {
            padding: 8px 14px;
            background: rgba(0,212,255,0.1);
            border: 1px solid var(--echo-cyan);
            border-radius: 4px;
            color: var(--echo-cyan);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.75em;
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
            color: var(--echo-dark);
            box-shadow: 0 0 15px rgba(0,212,255,0.5);
        }
        .filter-select {
            padding: 8px 12px;
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--echo-cyan);
            border-radius: 4px;
            color: var(--echo-cyan);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.75em;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.3s;
            min-width: 140px;
        }
        .filter-select:hover {
            background: rgba(0,212,255,0.1);
            box-shadow: 0 0 10px rgba(0,212,255,0.3);
        }
        .filter-select:focus {
            outline: none;
            border-color: var(--echo-cyan);
            box-shadow: 0 0 15px rgba(0,212,255,0.4);
        }
        .filter-select option {
            background: #1a1a2e;
            color: var(--echo-text);
            padding: 10px;
        }
        .filter-select optgroup {
            background: #12121a;
            color: var(--echo-orange);
            font-weight: 600;
            font-style: normal;
        }
        .filter-datetime {
            padding: 8px 10px;
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--echo-cyan);
            border-radius: 4px;
            color: var(--echo-cyan);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.75em;
            cursor: pointer;
            transition: all 0.3s;
        }
        .filter-datetime:hover {
            background: rgba(0,212,255,0.1);
            box-shadow: 0 0 10px rgba(0,212,255,0.3);
        }
        .filter-datetime:focus {
            outline: none;
            border-color: var(--echo-cyan);
            box-shadow: 0 0 15px rgba(0,212,255,0.4);
        }
        .filter-datetime::-webkit-calendar-picker-indicator {
            filter: invert(1);
            cursor: pointer;
        }
        /* Desktop: hide quick chips and toggle, show controls inline */
        .filter-quick {
            display: none;
        }
        .filter-toggle {
            display: none;
        }
        .filter-controls {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
            flex: 1;
        }
        .filter-clear-btn {
            padding: 8px 12px;
            background: rgba(255,106,0,0.1);
            border: 1px solid var(--echo-orange);
            border-radius: 4px;
            color: var(--echo-orange);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.7em;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.3s;
        }
        .filter-clear-btn:hover {
            background: rgba(255,106,0,0.2);
            box-shadow: 0 0 10px rgba(255,106,0,0.3);
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
        .caster-avatar-btn {
            position: relative;
            background: none;
            border: none;
            padding: 0;
            cursor: pointer;
            border-radius: 50%;
            transition: all 0.3s;
        }
        .caster-avatar-btn:hover {
            transform: scale(1.1);
        }
        .caster-avatar-btn:hover .caster-avatar {
            border-color: var(--echo-cyan);
            box-shadow: 0 0 10px var(--echo-cyan-glow);
        }
        .caster-avatar-btn::after {
            content: 'Copy URL';
            position: absolute;
            bottom: -20px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 0.7em;
            color: var(--echo-cyan);
            white-space: nowrap;
            opacity: 0;
            transition: opacity 0.2s;
        }
        .caster-avatar-btn:hover::after {
            opacity: 1;
        }
        .caster-avatar-btn.copied .caster-avatar {
            border-color: #3ba55c;
            box-shadow: 0 0 10px rgba(59,165,92,0.5);
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
        .admin-select option {
            background: #1a1a2e;
            color: var(--echo-text);
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
        
        /* Logo Review Styles */
        .logo-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            padding: 10px 0;
        }
        .logo-card {
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--echo-border);
            border-radius: 8px;
            padding: 15px;
            transition: border-color 0.2s;
        }
        .logo-card:hover {
            border-color: var(--echo-cyan);
        }
        .logo-card.approved {
            border-color: var(--echo-success);
        }
        .logo-preview {
            width: 100%;
            height: 150px;
            object-fit: contain;
            background: rgba(0,0,0,0.2);
            border-radius: 6px;
            cursor: pointer;
            margin-bottom: 10px;
        }
        .logo-info {
            margin-bottom: 10px;
        }
        .logo-team {
            font-family: 'Orbitron', sans-serif;
            font-size: 1.1em;
            color: var(--echo-text);
            margin-bottom: 5px;
        }
        .logo-submitter {
            font-size: 0.85em;
            color: var(--echo-text-dim);
        }
        .logo-warning {
            font-size: 0.8em;
            color: var(--echo-danger);
            background: rgba(255,51,102,0.1);
            padding: 5px 8px;
            border-radius: 4px;
            margin-top: 5px;
        }
        .logo-actions {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        .logo-actions .admin-btn {
            flex: 1;
            min-width: 70px;
            padding: 8px 12px;
            font-size: 0.85em;
        }
        
        /* Mobile Responsive Styles */
        @media (max-width: 768px) {
            body {
                padding: 10px;
                background-image: none;
                background-color: var(--echo-darker);
            }
            .container {
                max-width: 100%;
                padding-top: 0;
            }
            /* Mobile top bar */
            .top-bar {
                position: relative;
                left: 0;
                padding: 10px 15px;
                margin-bottom: 15px;
            }
            .top-bar-inner {
                flex-direction: column;
                gap: 10px;
                align-items: stretch;
            }
            /* Simplified header */
            .header {
                text-align: center;
                border-bottom: none;
            }
            .header::before {
                display: none;
            }
            .header h1 {
                font-size: 1.4em;
                letter-spacing: 1px;
            }
            .header .subtitle {
                font-size: 0.65em;
                letter-spacing: 2px;
                margin-top: 4px;
            }
            .season-badge {
                padding: 6px 12px;
                margin-top: 10px;
                font-size: 0.75em;
                border-radius: 15px;
            }
            /* Compact user bar */
            .user-bar {
                flex-direction: row;
                justify-content: center;
                gap: 10px;
                padding: 8px 12px;
                background: var(--echo-panel);
                border: 1px solid var(--echo-border);
                border-radius: 4px;
            }
            .user-info {
                gap: 8px;
            }
            .user-avatar {
                width: 28px;
                height: 28px;
            }
            .user-name {
                font-size: 0.9em;
            }
            .login-btn, .logout-btn {
                padding: 8px 14px;
                font-size: 0.75em;
            }
            /* Mobile Swipeable Sidebar */
            .sidebar {
                position: fixed;
                top: 0;
                left: -280px;
                width: 260px;
                height: 100vh;
                background: var(--echo-panel);
                border-right: 1px solid var(--echo-border);
                z-index: 200;
                transition: transform 0.3s ease;
                padding-top: 0;
            }
            .sidebar.open {
                transform: translateX(280px);
            }
            .sidebar-header {
                padding: 20px;
                border-bottom: 1px solid var(--echo-border);
            }
            .sidebar-header h2 {
                font-size: 0.9em;
            }
            .sidebar-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: rgba(0,0,0,0.6);
                z-index: 150;
                opacity: 0;
                transition: opacity 0.3s ease;
            }
            .sidebar-overlay.show {
                display: block;
                opacity: 1;
            }
            .sidebar-toggle {
                display: flex;
                align-items: center;
                justify-content: center;
                position: fixed;
                top: 10px;
                left: 10px;
                width: 56px;
                height: 56px;
                background: var(--echo-panel);
                border: 2px solid var(--echo-orange);
                border-radius: 12px;
                z-index: 200;
                cursor: pointer;
                padding: 0;
                box-shadow: 0 2px 12px rgba(0,0,0,0.4);
            }
            .sidebar-toggle:active {
                background: rgba(255,106,0,0.2);
                transform: scale(0.95);
            }
            .sidebar-toggle svg {
                width: 30px;
                height: 30px;
                fill: var(--echo-orange);
            }
            .tabs {
                flex-direction: column;
                gap: 0;
            }
            .tab-btn {
                padding: 24px 30px;
                font-size: 1.4em;
                text-align: left;
                border-left: 5px solid transparent;
                border-radius: 0;
                min-height: 70px;
                display: flex;
                align-items: center;
            }
            .tab-btn.active {
                border-left-color: var(--echo-orange);
                background: rgba(255,106,0,0.15);
            }
            /* Remove bottom nav padding */
            .container {
                padding-left: 0;
                padding-bottom: 20px;
                padding-top: 0;
            }
            .top-bar {
                position: relative;
                left: 0;
            }
            /* Collapsible Filter - Single row with toggle */
            .filter-bar {
                flex-direction: row;
                gap: 6px;
                padding: 0;
                background: none;
                margin-bottom: 12px;
                align-items: center;
            }
            .filter-toggle {
                display: flex;
                align-items: center;
                gap: 4px;
                padding: 8px 10px;
                font-size: 0.7em;
                flex-shrink: 0;
            }
            .filter-toggle svg {
                width: 14px;
                height: 14px;
                fill: currentColor;
            }
            .filter-controls {
                display: none;
                position: absolute;
                top: 100%;
                left: 0;
                right: 0;
                padding: 10px;
                background: var(--echo-panel);
                border: 1px solid var(--echo-border);
                border-radius: 6px;
                z-index: 50;
                flex-direction: column;
                gap: 8px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            }
            .filter-controls.show {
                display: flex;
            }
            .filter-bar {
                position: relative;
            }
            /* Inline quick filters */
            .filter-quick {
                display: flex;
                gap: 4px;
                flex: 1;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                padding: 2px 0;
            }
            .filter-quick::-webkit-scrollbar {
                display: none;
            }
            .filter-chip {
                padding: 6px 10px;
                background: rgba(0,0,0,0.3);
                border: 1px solid rgba(255,255,255,0.2);
                border-radius: 20px;
                color: var(--echo-text-dim);
                font-family: 'Rajdhani', sans-serif;
                font-size: 0.75em;
                font-weight: 600;
                white-space: nowrap;
                cursor: pointer;
                transition: all 0.2s;
                flex-shrink: 0;
            }
            .filter-chip:hover, .filter-chip.active {
                background: rgba(0,212,255,0.2);
                border-color: var(--echo-cyan);
                color: var(--echo-cyan);
            }
            .filter-select, .filter-datetime, .filter-clear-btn {
                width: 100%;
                padding: 10px 12px;
                font-size: 0.8em;
            }
            
            /* REDESIGNED MATCH CARDS - Clean & Compact */
            .match-card {
                padding: 14px;
                margin-bottom: 12px;
                border-radius: 10px;
            }
            .match-card::before {
                height: 2px;
            }
            .match-header {
                flex-direction: row;
                justify-content: space-between;
                align-items: flex-start;
                gap: 8px;
                margin-bottom: 8px;
            }
            .match-header .teams {
                font-size: 1em;
                line-height: 1.3;
                letter-spacing: 0.5px;
            }
            .match-header .team-vs {
                display: block;
                margin: 2px 0;
                font-size: 0.6em;
                color: var(--echo-text-dim);
            }
            .match-id {
                padding: 4px 8px;
                font-size: 0.65em;
                flex-shrink: 0;
            }
            .match-time {
                font-size: 0.8em;
                margin-bottom: 10px;
                padding-bottom: 10px;
                border-bottom: 1px solid rgba(255,255,255,0.1);
            }
            .time-relative {
                display: inline;
                margin-left: 8px;
            }
            .status-badge {
                padding: 2px 6px;
                font-size: 0.6em;
                margin-left: 6px;
            }
            
            /* COMPACT CLAIMS - 2x2 Grid */
            .claims {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 6px;
            }
            .claim-slot {
                padding: 8px 10px;
                flex-direction: column;
                align-items: flex-start;
                gap: 6px;
                border-radius: 6px;
            }
            .slot-info {
                flex-direction: column;
                gap: 2px;
                width: 100%;
            }
            .role-label {
                font-size: 0.65em;
                opacity: 0.8;
            }
            .holder-name {
                font-size: 0.8em;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                max-width: 100%;
            }
            .open-text {
                font-size: 0.75em;
            }
            .slot-buttons {
                width: 100%;
                display: flex;
                flex-direction: row;
                gap: 4px;
            }
            .slot-buttons .claim-btn,
            .slot-buttons .unclaim-btn,
            .slot-buttons .assign-btn {
                flex: 1;
                padding: 6px 4px;
                font-size: 0.6em;
                min-height: 28px;
            }
            
            .match-type {
                font-size: 0.7em;
                margin-top: 8px;
                opacity: 0.7;
            }
            
            /* STREAMLINED BROADCAST CONTROLS */
            .broadcast-controls {
                padding: 12px;
                margin-top: 10px;
                background: rgba(0,0,0,0.2);
                border-radius: 8px;
            }
            .broadcast-controls h4 {
                font-size: 0.7em;
                margin-bottom: 10px;
                display: flex;
                align-items: center;
                gap: 6px;
            }
            .broadcast-controls h4 span {
                font-size: 0.9em;
            }
            .stream-row {
                flex-direction: row;
                align-items: center;
                margin-bottom: 10px;
                gap: 8px;
            }
            .stream-label {
                font-size: 0.7em;
                flex-shrink: 0;
            }
            .stream-select {
                flex: 1;
                padding: 8px 10px;
                font-size: 0.75em;
                min-width: 0;
            }
            .broadcast-btns {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 6px;
            }
            .broadcast-btn, .chat-toggle-btn {
                padding: 10px 6px;
                font-size: 0.65em;
                letter-spacing: 0.5px;
            }
            
            /* Leaderboard mobile */
            .leaderboard-header {
                padding: 14px;
            }
            .leaderboard-header h2 {
                font-size: 0.95em;
            }
            .leaderboard-row {
                grid-template-columns: 40px 1fr 50px;
                padding: 12px;
                gap: 10px;
            }
            .rank {
                font-size: 0.95em;
            }
            .caster-avatar {
                width: 30px;
                height: 30px;
            }
            .caster-name {
                font-size: 0.85em;
            }
            .cast-count {
                font-size: 1em;
            }
            .cast-label {
                font-size: 0.55em;
            }
            .caster-avatar-btn::after {
                display: none;
            }
            .cycle-selector {
                flex-direction: column;
                gap: 8px;
                margin-bottom: 16px;
            }
            .cycle-select {
                width: 100%;
                padding: 10px;
            }
            .cycle-info {
                flex-direction: column;
                gap: 8px;
                padding: 12px;
                margin-bottom: 16px;
            }
            .cycle-info-item {
                flex-direction: row;
                justify-content: space-between;
                padding: 6px 0;
                border-bottom: 1px solid var(--echo-border);
            }
            .cycle-info-item:last-child {
                border-bottom: none;
            }
            .cycle-history {
                grid-template-columns: 1fr;
                gap: 10px;
            }
            .cycle-card {
                padding: 14px;
            }
            /* Admin panel mobile */
            .admin-section {
                padding: 14px;
                margin-bottom: 14px;
            }
            .admin-section-header h3 {
                font-size: 0.9em;
            }
            .admin-row {
                padding: 12px 0;
            }
            .admin-row-desc {
                font-size: 0.8em;
                margin-bottom: 10px;
            }
            .admin-form {
                flex-direction: column;
                gap: 10px;
            }
            .admin-input-group {
                width: 100%;
            }
            .admin-input-group label {
                font-size: 0.7em;
                margin-bottom: 4px;
            }
            .admin-input, .admin-select {
                width: 100%;
                padding: 10px;
                font-size: 0.9em;
            }
            .admin-btn {
                width: 100%;
                padding: 12px;
                font-size: 0.8em;
            }
            /* Modal mobile */
            .confirm-box {
                margin: 12px;
                padding: 20px 16px;
            }
            .confirm-box h3 {
                font-size: 1em;
            }
            .confirm-box p {
                font-size: 0.85em;
            }
            .confirm-btns {
                flex-direction: row;
                gap: 10px;
            }
            .confirm-btns button {
                flex: 1;
                padding: 12px;
                font-size: 0.8em;
            }
            .assign-modal-box {
                margin: 10px;
                padding: 16px;
                max-height: 85vh;
            }
            .assign-user-item {
                padding: 12px 10px;
            }
            /* Toast mobile */
            .toast {
                left: 10px;
                right: 10px;
                bottom: 10px;
                text-align: center;
                padding: 12px 16px;
                font-size: 0.8em;
            }
            /* Hide decorations on mobile */
            .hex-bg {
                display: none;
            }
        }
        
        @media (max-width: 480px) {
            body {
                padding: 8px;
            }
            .header h1 {
                font-size: 1.2em;
            }
            .header .subtitle {
                font-size: 0.55em;
            }
            .season-badge {
                font-size: 0.65em;
                padding: 5px 10px;
            }
            .user-bar {
                padding: 8px 10px;
            }
            .user-avatar {
                width: 24px;
                height: 24px;
            }
            .user-name {
                font-size: 0.8em;
            }
            .tab-btn {
                padding: 8px 12px;
                font-size: 0.6em;
            }
            /* Even more compact match cards */
            .match-card {
                padding: 12px;
                margin-bottom: 10px;
            }
            .match-header .teams {
                font-size: 0.9em;
            }
            .match-id {
                padding: 3px 6px;
                font-size: 0.6em;
            }
            .match-time {
                font-size: 0.75em;
                padding-bottom: 8px;
                margin-bottom: 8px;
            }
            /* Stack claims 2x2 tighter */
            .claims {
                gap: 4px;
            }
            .claim-slot {
                padding: 6px 8px;
                gap: 4px;
            }
            .role-label {
                font-size: 0.6em;
            }
            .holder-name {
                font-size: 0.75em;
            }
            .open-text {
                font-size: 0.7em;
            }
            .slot-buttons .claim-btn,
            .slot-buttons .unclaim-btn,
            .slot-buttons .assign-btn {
                padding: 5px 4px;
                font-size: 0.55em;
                min-height: 26px;
            }
            /* Compact broadcast */
            .broadcast-controls {
                padding: 10px;
                margin-top: 8px;
            }
            .broadcast-controls h4 {
                font-size: 0.65em;
                margin-bottom: 8px;
            }
            .stream-select {
                padding: 6px 8px;
                font-size: 0.7em;
            }
            .broadcast-btns {
                gap: 4px;
            }
            .broadcast-btn {
                padding: 8px 4px;
                font-size: 0.55em;
                letter-spacing: 0;
            }
            /* Leaderboard tighter */
            .leaderboard-row {
                grid-template-columns: 35px 1fr 45px;
                padding: 10px 8px;
                gap: 8px;
            }
            .rank {
                font-size: 0.85em;
            }
            .caster-avatar {
                width: 26px;
                height: 26px;
            }
            .caster-name {
                font-size: 0.8em;
            }
            .cast-count {
                font-size: 0.9em;
            }
            /* Chat mobile */
            .chat-messages {
                height: 200px;
            }
            .chat-avatar {
                width: 30px;
                height: 30px;
            }
            .chat-msg-continued {
                padding-left: 40px;
            }
            .chat-toggle-btn {
                font-size: 0.65em;
                padding: 8px 10px;
            }
        }
        
        /* Touch-friendly improvements */
        @media (pointer: coarse) {
            .admin-btn, .filter-btn, .filter-select, .filter-datetime, .filter-clear-btn {
                min-height: 44px;
            }
            .assign-user-item {
                min-height: 48px;
            }
        }
        
        /* Touch targets on larger touch screens (tablets) */
        @media (pointer: coarse) and (min-width: 769px) {
            .claim-btn, .unclaim-btn, .assign-btn, .broadcast-btn, .tab-btn {
                min-height: 44px;
            }
            .claim-slot {
                min-height: 56px;
            }
        }
        
        /* Safe area for notched phones */
        @supports (padding: max(0px)) {
            body {
                padding-left: max(8px, env(safe-area-inset-left));
                padding-right: max(8px, env(safe-area-inset-right));
                padding-bottom: max(8px, env(safe-area-inset-bottom));
            }
        }
        
        /* Pull-to-refresh styles */
        .pull-indicator {
            position: fixed;
            top: 0;
            left: 50%;
            transform: translateX(-50%) translateY(-60px);
            width: 50px;
            height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--echo-panel);
            border: 2px solid var(--echo-orange);
            border-radius: 50%;
            z-index: 9999;
            transition: transform 0.2s ease;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5), 0 0 15px var(--echo-orange-glow);
        }
        .pull-indicator.visible {
            transform: translateX(-50%) translateY(20px);
        }
        .pull-indicator.refreshing {
            transform: translateX(-50%) translateY(20px);
        }
        .pull-indicator svg {
            width: 24px;
            height: 24px;
            fill: var(--echo-orange);
            transition: transform 0.2s ease;
        }
        .pull-indicator.refreshing svg {
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        .pull-indicator .pull-text {
            display: none;
        }
    </style>
</head>
<body>
    <div class="pull-indicator" id="pull-indicator">
        <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
    </div>
    <div class="hex-bg"></div>
    <div class="sidebar-overlay" id="sidebar-overlay" onclick="closeSidebar()"></div>
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <h2>Navigation</h2>
        </div>
        <div class="tabs">
            <button class="tab-btn {schedule_active}" onclick="switchTab('schedule')">Schedule</button>
            <button class="tab-btn {leaderboard_active}" onclick="switchTab('leaderboard')">Leaderboard</button>
            {admin_tab_btn}
            {logos_tab_btn}
            {broadcast_tabs}
        </div>
    </div>
    <button class="sidebar-toggle" id="sidebar-toggle" onclick="toggleSidebar()">
        <svg viewBox="0 0 24 24"><path d="M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z"/></svg>
    </button>
    <div class="top-bar">
        <div class="top-bar-inner">
            <div class="header">
                <h1>{league_name}</h1>
                <p class="subtitle">Broadcast Hub</p>
                {season_badge}
            </div>
            {user_bar}
        </div>
    </div>
    <div class="container">
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
        {broadcast_tab_contents}
        {admin_tab_content}
        {logos_tab_content}
    </div>
    <script>
        // Refresh function that preserves scroll position and tab state
        function refreshPage() {
            const scrollPos = window.scrollY;
            const activeTab = new URL(window.location).searchParams.get('tab') || 'schedule';
            sessionStorage.setItem('refreshScrollPos', scrollPos);
            sessionStorage.setItem('refreshTab', activeTab);
            location.reload();
        }
        
        // Profile menu functions
        function toggleProfileMenu(event) {
            event.stopPropagation();
            const dropdown = document.getElementById('profile-dropdown');
            dropdown.classList.toggle('show');
        }
        
        // Close profile menu when clicking outside
        document.addEventListener('click', function(e) {
            const dropdown = document.getElementById('profile-dropdown');
            if (dropdown && !e.target.closest('.profile-menu-container')) {
                dropdown.classList.remove('show');
            }
        });
        
        async function uploadProfilePic(input) {
            if (!input.files || !input.files[0]) return;
            
            const file = input.files[0];
            if (!file.type.startsWith('image/')) {
                alert('Please select an image file');
                return;
            }
            if (file.size > 5 * 1024 * 1024) {
                alert('Image must be less than 5MB');
                return;
            }
            
            const formData = new FormData();
            formData.append('image', file);
            
            try {
                const resp = await fetch('/api/profile-pic/upload', {
                    method: 'POST',
                    body: formData
                });
                const data = await resp.json();
                
                if (data.success) {
                    // Update avatar image
                    const avatarImg = document.getElementById('user-avatar-img');
                    if (avatarImg) {
                        avatarImg.src = data.avatar_url + '?t=' + Date.now();
                    }
                    // Show reset button
                    const resetBtn = document.getElementById('reset-pic-btn');
                    if (resetBtn) resetBtn.style.display = 'block';
                    // Close dropdown
                    document.getElementById('profile-dropdown').classList.remove('show');
                } else {
                    alert('Upload failed: ' + (data.error || 'Unknown error'));
                }
            } catch (err) {
                alert('Upload failed: ' + err.message);
            }
            
            // Clear input so same file can be selected again
            input.value = '';
        }
        
        async function resetProfilePic() {
            if (!confirm('Reset to your Discord avatar?')) return;
            
            try {
                const resp = await fetch('/api/profile-pic/delete', {
                    method: 'POST'
                });
                const data = await resp.json();
                
                if (data.success) {
                    // Reload to get Discord avatar
                    location.reload();
                } else {
                    alert('Reset failed: ' + (data.error || 'Unknown error'));
                }
            } catch (err) {
                alert('Reset failed: ' + err.message);
            }
        }
        
        // Restore scroll position and tab after refresh
        (function() {
            const savedScroll = sessionStorage.getItem('refreshScrollPos');
            const savedTab = sessionStorage.getItem('refreshTab');
            if (savedScroll !== null || savedTab !== null) {
                sessionStorage.removeItem('refreshScrollPos');
                sessionStorage.removeItem('refreshTab');
                
                // Wait for DOM to be ready, then restore tab and scroll
                setTimeout(() => {
                    if (savedTab && savedTab !== 'schedule') {
                        // Switch to the saved tab
                        if (typeof switchTab === 'function') {
                            switchTab(savedTab);
                        }
                    }
                    if (savedScroll !== null) {
                        window.scrollTo(0, parseInt(savedScroll, 10));
                    }
                }, 150);
            }
        })();
        
        // Pull-to-refresh for mobile
        (function() {
            const indicator = document.getElementById('pull-indicator');
            let startY = 0;
            let currentY = 0;
            let pulling = false;
            let refreshing = false;
            const threshold = 80;
            
            function isTouchDevice() {
                return 'ontouchstart' in window || navigator.maxTouchPoints > 0;
            }
            
            if (isTouchDevice()) {
                document.addEventListener('touchstart', function(e) {
                    if (window.scrollY === 0 && !refreshing) {
                        startY = e.touches[0].pageY;
                        pulling = true;
                    }
                }, { passive: true });
                
                document.addEventListener('touchmove', function(e) {
                    if (!pulling || refreshing) return;
                    currentY = e.touches[0].pageY;
                    const pullDistance = currentY - startY;
                    
                    if (pullDistance > 0 && window.scrollY === 0) {
                        const progress = Math.min(pullDistance / threshold, 1);
                        indicator.style.transform = `translateX(-50%) translateY(${-60 + (80 * progress)}px)`;
                        indicator.style.opacity = progress;
                        
                        // Rotate arrow based on progress
                        const rotation = progress * 180;
                        indicator.querySelector('svg').style.transform = `rotate(${rotation}deg)`;
                        
                        if (pullDistance > threshold) {
                            indicator.classList.add('visible');
                        }
                    }
                }, { passive: true });
                
                document.addEventListener('touchend', function(e) {
                    if (!pulling || refreshing) return;
                    const pullDistance = currentY - startY;
                    
                    if (pullDistance > threshold && window.scrollY === 0) {
                        refreshing = true;
                        indicator.classList.add('refreshing');
                        indicator.style.transform = 'translateX(-50%) translateY(20px)';
                        indicator.style.opacity = 1;
                        indicator.querySelector('svg').style.transform = '';
                        
                        setTimeout(() => {
                            refreshPage();
                        }, 300);
                    } else {
                        indicator.style.transform = 'translateX(-50%) translateY(-60px)';
                        indicator.style.opacity = 0;
                        indicator.classList.remove('visible');
                    }
                    
                    pulling = false;
                    startY = 0;
                    currentY = 0;
                }, { passive: true });
            }
        })();
        
        // Sidebar swipe gesture
        (function() {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebar-overlay');
            let touchStartX = 0;
            let touchCurrentX = 0;
            let swiping = false;
            const swipeThreshold = 80; // Distance to trigger open/close
            
            function openSidebar() {
                sidebar.classList.add('open');
                overlay.classList.add('show');
                document.body.style.overflow = 'hidden';
            }
            
            function closeSidebar() {
                sidebar.classList.remove('open');
                overlay.classList.remove('show');
                document.body.style.overflow = '';
            }
            
            // Expose globally
            window.toggleSidebar = function() {
                if (sidebar.classList.contains('open')) {
                    closeSidebar();
                } else {
                    openSidebar();
                }
            };
            window.closeSidebar = closeSidebar;
            
            // Also close sidebar when clicking a tab (on mobile)
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    if (window.innerWidth <= 768) {
                        closeSidebar();
                    }
                });
            });
            
            // Touch swipe handling
            document.addEventListener('touchstart', function(e) {
                const touchX = e.touches[0].clientX;
                
                // Start swipe from anywhere on screen
                touchStartX = touchX;
                swiping = true;
            }, { passive: true });
            
            document.addEventListener('touchmove', function(e) {
                if (!swiping) return;
                touchCurrentX = e.touches[0].clientX;
                
                const isOpen = sidebar.classList.contains('open');
                const diffX = touchCurrentX - touchStartX;
                
                // Visual feedback during swipe
                if (!isOpen && diffX > 0) {
                    // Swiping right to open
                    const progress = Math.min(diffX / 260, 1);
                    sidebar.style.transform = `translateX(${diffX}px)`;
                    sidebar.style.transition = 'none';
                    overlay.style.display = 'block';
                    overlay.style.opacity = progress * 0.6;
                } else if (isOpen && diffX < 0) {
                    // Swiping left to close
                    sidebar.style.transform = `translateX(${280 + diffX}px)`;
                    sidebar.style.transition = 'none';
                    overlay.style.opacity = 0.6 + (diffX / 260) * 0.6;
                }
            }, { passive: true });
            
            document.addEventListener('touchend', function(e) {
                if (!swiping) return;
                
                const diffX = touchCurrentX - touchStartX;
                const isOpen = sidebar.classList.contains('open');
                
                sidebar.style.transition = '';
                sidebar.style.transform = '';
                overlay.style.transition = 'opacity 0.3s ease';
                
                if (!isOpen && diffX > swipeThreshold) {
                    openSidebar();
                } else if (isOpen && diffX < -swipeThreshold) {
                    closeSidebar();
                } else {
                    // Snap back
                    if (isOpen) {
                        sidebar.classList.add('open');
                        overlay.style.opacity = '0.6';
                    } else {
                        overlay.style.opacity = '0';
                        setTimeout(() => { overlay.style.display = ''; }, 300);
                    }
                }
                
                swiping = false;
                touchStartX = 0;
                touchCurrentX = 0;
            }, { passive: true });
        })();
        
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
        
        let crewMembersCache = null;
        async function showAssignModal(matchId, role, slot, label) {
            // Fetch crew members if not cached
            if (!crewMembersCache) {
                try {
                    const resp = await fetch('/api/crew-members');
                    const data = await resp.json();
                    if (data.success) {
                        crewMembersCache = data.members;
                    } else {
                        showToast(data.error || 'Failed to load crew', 'error');
                        return;
                    }
                } catch (e) {
                    showToast('Network error', 'error');
                    return;
                }
            }
            
            // Create modal
            const modal = document.createElement('div');
            modal.className = 'assign-modal';
            
            let userList = crewMembersCache.map(m => `
                <div class="assign-user-item" onclick="assignSlot('${matchId}', '${role}', ${slot}, '${m.user_id}', this)">
                    <img src="${m.avatar_url}" alt="">
                    <span>${m.display_name}</span>
                </div>
            `).join('');
            
            modal.innerHTML = `
                <div class="assign-modal-box">
                    <h3>Assign ${label}</h3>
                    <p>Select a crew member to assign to this slot:</p>
                    <div class="assign-user-list">
                        ${userList || '<p>No crew members found</p>'}
                    </div>
                    <button class="assign-modal-close" onclick="this.closest('.assign-modal').remove()">Cancel</button>
                </div>
            `;
            
            document.body.appendChild(modal);
            
            // Close on backdrop click
            modal.onclick = (e) => {
                if (e.target === modal) modal.remove();
            };
        }
        
        async function assignSlot(matchId, role, slot, userId, btn) {
            btn.style.opacity = '0.5';
            btn.style.pointerEvents = 'none';
            try {
                const resp = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId, role: role, slot: slot, assign_user_id: userId})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Assigned!', 'success');
                    setTimeout(() => location.reload(), 500);
                } else {
                    showToast(data.error || 'Failed to assign', 'error');
                    btn.style.opacity = '1';
                    btn.style.pointerEvents = 'auto';
                }
            } catch (e) {
                showToast('Network error', 'error');
                btn.style.opacity = '1';
                btn.style.pointerEvents = 'auto';
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
        
        async function setStreamChannel(matchId, selectEl) {
            const channel = selectEl.value;
            if (!channel) return;
            try {
                const resp = await fetch('/api/set_stream_channel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId, stream_channel: parseInt(channel)})
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('Stream channel updated!', 'success');
                    selectEl.classList.remove('warning');
                } else {
                    showToast(data.error || 'Failed to update channel', 'error');
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
            
            // Update button states - handle both main tabs and match tabs
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.tab-match-btn').forEach(btn => btn.classList.remove('active'));
            
            const mainTabBtn = document.querySelector(`.tab-btn[onclick="switchTab('${tab}')"]`);
            if (mainTabBtn) {
                mainTabBtn.classList.add('active');
            } else {
                // It's a match tab
                const matchTabBtn = document.querySelector(`.tab-match-btn[data-tab="${tab}"]`);
                if (matchTabBtn) {
                    matchTabBtn.classList.add('active');
                }
            }
            
            // Update content visibility
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            const tabContent = document.getElementById('tab-' + tab);
            if (tabContent) {
                tabContent.classList.add('active');
                // If it's a match tab, start loading chat
                if (tab.startsWith('match-')) {
                    const matchId = tabContent.dataset.matchId;
                    if (matchId) {
                        loadChatMessages(matchId);
                        // Start polling
                        if (!chatIntervals[matchId]) {
                            chatIntervals[matchId] = setInterval(() => loadChatMessages(matchId), 5000);
                        }
                    }
                }
                // If it's the logos tab, load the logo data
                if (tab === 'logos') {
                    loadPendingLogos();
                    loadApprovedLogos();
                }
            }
            
            // Close sidebar on mobile
            closeSidebar();
        }
        
        function toggleCategory(categoryId) {
            const category = document.getElementById(categoryId);
            if (category) {
                category.classList.toggle('expanded');
            }
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
        
        // Schedule filter
        function applyScheduleFilter() {
            const filterValue = document.getElementById('schedule-filter').value;
            // Clear datetime input when using dropdown
            document.getElementById('filter-datetime').value = '';
            applyFilters();
        }
        
        function applyDateTimeFilter() {
            // Reset dropdown to "all" when using datetime
            document.getElementById('schedule-filter').value = 'all';
            applyFilters();
        }
        
        function clearFilters() {
            document.getElementById('schedule-filter').value = 'all';
            document.getElementById('filter-datetime').value = '';
            // Reset quick filter chips
            document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
            document.querySelector('.filter-chip').classList.add('active'); // Select "All"
            applyFilters();
        }
        
        function quickFilter(value, chip) {
            // Update chip visual state
            document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            // Set dropdown value and apply
            document.getElementById('schedule-filter').value = value;
            document.getElementById('filter-datetime').value = '';
            applyFilters();
        }
        
        function toggleFilters(btn) {
            const controls = document.getElementById('filter-controls');
            controls.classList.toggle('show');
            btn.classList.toggle('active');
        }
        
        function applyFilters() {
            const filterValue = document.getElementById('schedule-filter').value;
            const filterDateTime = document.getElementById('filter-datetime').value;
            const cards = document.querySelectorAll('.match-card');
            const noClaimsMsg = document.getElementById('no-claims-msg');
            let visibleCount = 0;
            
            // Get today's and tomorrow's date strings (local time, not UTC)
            const now = new Date();
            const today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
            const tmrw = new Date(now.getTime() + 86400000);
            const tomorrow = tmrw.getFullYear() + '-' + String(tmrw.getMonth() + 1).padStart(2, '0') + '-' + String(tmrw.getDate()).padStart(2, '0');
            
            cards.forEach(card => {
                let show = true;
                const cardDate = card.dataset.date;
                const cardTimestamp = parseInt(card.dataset.timestamp) || 0;
                
                // Apply dropdown filter
                if (filterValue === 'all') {
                    show = true;
                } else if (filterValue === 'my-claims') {
                    show = card.dataset.myClaim === 'true';
                } else if (filterValue === 'open-slots') {
                    show = card.dataset.hasOpen === 'true';
                } else if (filterValue === 'live-soon') {
                    show = card.dataset.status === 'live' || card.dataset.status === 'soon';
                } else if (filterValue === 'today') {
                    show = cardDate === today;
                } else if (filterValue === 'tomorrow') {
                    show = cardDate === tomorrow;
                } else if (filterValue.startsWith('type:')) {
                    const matchType = filterValue.substring(5);
                    show = card.dataset.matchType === matchType;
                }
                
                // Apply datetime filter if set (show matches starting at or after this datetime)
                if (show && filterDateTime) {
                    const filterTimestamp = new Date(filterDateTime).getTime() / 1000;
                    show = cardTimestamp >= filterTimestamp;
                }
                
                if (show) {
                    card.classList.remove('filtered-out');
                    visibleCount++;
                } else {
                    card.classList.add('filtered-out');
                }
            });
            
            noClaimsMsg.style.display = visibleCount === 0 ? 'block' : 'none';
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
            const weeks = parseInt(document.getElementById('cycle-weeks').value) || 5;
            const result = document.getElementById('result-cycle');
            
            if (!name) {
                result.innerHTML = '<span class="error">Enter a cycle name</span>';
                return;
            }
            if (!confirm('Start new ' + weeks + '-week cycle "' + name + '"? Current leaderboard will be archived.')) {
                return;
            }
            try {
                const resp = await fetch('/api/admin/start-cycle', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ name: name, weeks: weeks })
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Cycle started', 'success');
                    setTimeout(() => location.reload(), 1000);
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminEndCycle() {
            const result = document.getElementById('result-cycle');
            if (!confirm('End the current cycle now and archive the leaderboard?')) {
                return;
            }
            try {
                const resp = await fetch('/api/admin/end-cycle', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({})
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Cycle ended', 'success');
                    setTimeout(() => location.reload(), 1000);
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminForceDelete() {
            const matchId = document.getElementById('force-delete-match').value;
            const result = document.getElementById('result-force-delete');
            
            if (!matchId) {
                result.innerHTML = '<span class="error">Select a match</span>';
                return;
            }
            if (!confirm('Force delete this match? This will delete the private channel (if any) and claim message. Leaderboard will NOT be updated.')) {
                return;
            }
            try {
                const resp = await fetch('/api/admin/force-delete', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ match_id: matchId })
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Match force deleted', 'success');
                    setTimeout(() => location.reload(), 1000);
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        async function adminForceCreateMatch() {
            const teamA = document.getElementById('force-create-team-a').value.trim();
            const teamB = document.getElementById('force-create-team-b').value.trim();
            const datetime = document.getElementById('force-create-datetime').value;
            const matchType = document.getElementById('force-create-type').value.trim();
            const result = document.getElementById('result-force-create');
            
            if (!teamA || !teamB) {
                result.innerHTML = '<span class="error">Enter both team names</span>';
                return;
            }
            if (!datetime) {
                result.innerHTML = '<span class="error">Enter a date/time</span>';
                return;
            }
            if (!confirm('Create match: ' + teamA + ' vs ' + teamB + '?')) {
                return;
            }
            try {
                const resp = await fetch('/api/admin/force-create-match', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        team_a: teamA,
                        team_b: teamB,
                        datetime: datetime,
                        match_type: matchType || null
                    })
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<span class="success">✓ ' + data.message + '</span>';
                    showToast('Match created', 'success');
                    // Clear form
                    document.getElementById('force-create-team-a').value = '';
                    document.getElementById('force-create-team-b').value = '';
                    document.getElementById('force-create-datetime').value = '';
                    document.getElementById('force-create-type').value = '';
                    setTimeout(() => location.reload(), 1500);
                } else {
                    result.innerHTML = '<span class="error">✗ ' + data.error + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span class="error">✗ Request failed</span>';
            }
        }
        
        // ========== Team Logo Review Functions ==========
        
        async function loadPendingLogos() {
            const container = document.getElementById('pending-logos-container');
            if (!container) return;
            
            container.innerHTML = '<div class="loading">Loading pending submissions...</div>';
            
            try {
                const resp = await fetch('/api/logos/pending', {credentials: 'include'});
                const data = await resp.json();
                
                if (!data.success) {
                    container.innerHTML = '<div class="no-data">Error: ' + (data.error || 'Unknown error') + '</div>';
                    return;
                }
                
                if (data.pending.length === 0) {
                    container.innerHTML = '<div class="no-data">No pending logo submissions</div>';
                    return;
                }
                
                let html = '<div class="logo-grid">';
                for (const item of data.pending) {
                    const existingWarning = item.has_existing_logo ? 
                        '<div class="logo-warning">This team already has a logo - approving will replace it</div>' : '';
                    html += `
                        <div class="logo-card" id="logo-${item.message_id}">
                            <img src="${item.image_url}" class="logo-preview" alt="Team Logo" onclick="window.open('${item.image_url}', '_blank')">
                            <div class="logo-info">
                                <div class="logo-team">${item.team_name}</div>
                                <div class="logo-submitter">Submitted by ${item.display_name}</div>
                                ${existingWarning}
                            </div>
                            <div class="logo-actions">
                                <button class="admin-btn success" onclick="approveLogo('${item.message_id}', '${item.team_name.replace(/'/g, "\\'")}', '${item.image_url}')">Approve</button>
                                <button class="admin-btn danger" onclick="rejectLogo('${item.message_id}', false)">Reject</button>
                                <button class="admin-btn" onclick="rejectLogo('${item.message_id}', true)" title="Reject and delete message">Reject & Delete</button>
                            </div>
                        </div>
                    `;
                }
                html += '</div>';
                container.innerHTML = html;
            } catch(e) {
                container.innerHTML = '<div class="no-data">Failed to load pending logos</div>';
            }
        }
        
        async function approveLogo(messageId, teamName, imageUrl) {
            if (!confirm('Approve this logo for ' + teamName + '?')) return;
            
            try {
                const resp = await fetch('/api/logos/approve', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message_id: messageId, team_name: teamName, image_url: imageUrl})
                });
                const data = await resp.json();
                
                if (data.success) {
                    showToast('Logo approved for ' + teamName, 'success');
                    document.getElementById('logo-' + messageId)?.remove();
                    loadApprovedLogos();
                } else {
                    showToast('Error: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch(e) {
                showToast('Failed to approve logo', 'error');
            }
        }
        
        async function rejectLogo(messageId, deleteMessage) {
            const action = deleteMessage ? 'reject and delete' : 'reject';
            if (!confirm('Are you sure you want to ' + action + ' this submission?')) return;
            
            try {
                const resp = await fetch('/api/logos/reject', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message_id: messageId, delete_message: deleteMessage})
                });
                const data = await resp.json();
                
                if (data.success) {
                    showToast('Logo rejected', 'success');
                    document.getElementById('logo-' + messageId)?.remove();
                } else {
                    showToast('Error: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch(e) {
                showToast('Failed to reject logo', 'error');
            }
        }
        
        async function loadApprovedLogos() {
            const container = document.getElementById('approved-logos-container');
            if (!container) return;
            
            container.innerHTML = '<div class="loading">Loading approved logos...</div>';
            
            try {
                const resp = await fetch('/api/logos', {credentials: 'include'});
                const data = await resp.json();
                
                if (!data.success) {
                    container.innerHTML = '<div class="no-data">Error: ' + (data.error || 'Unknown error') + '</div>';
                    return;
                }
                
                if (data.logos.length === 0) {
                    container.innerHTML = '<div class="no-data">No approved logos yet</div>';
                    return;
                }
                
                let html = '<div class="logo-grid">';
                for (const logo of data.logos) {
                    html += `
                        <div class="logo-card approved">
                            <img src="${logo.logo_url}" class="logo-preview" alt="${logo.team_name} Logo" onclick="window.open('${logo.logo_url}', '_blank')">
                            <div class="logo-info">
                                <div class="logo-team">${logo.team_name}</div>
                            </div>
                            <div class="logo-actions">
                                <button class="admin-btn" onclick="renameLogo('${logo.team_name.replace(/'/g, "\\'")}')" title="Change team name">Rename</button>
                                <button class="admin-btn danger" onclick="deleteLogo('${logo.team_name.replace(/'/g, "\\'")}')">Delete</button>
                            </div>
                        </div>
                    `;
                }
                html += '</div>';
                container.innerHTML = html;
            } catch(e) {
                container.innerHTML = '<div class="no-data">Failed to load approved logos</div>';
            }
        }
        
        async function deleteLogo(teamName) {
            if (!confirm('Delete the logo for ' + teamName + '?')) return;
            
            try {
                const resp = await fetch('/api/logos/delete', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({team_name: teamName})
                });
                const data = await resp.json();
                
                if (data.success) {
                    showToast('Logo deleted for ' + teamName, 'success');
                    loadApprovedLogos();
                } else {
                    showToast('Error: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch(e) {
                showToast('Failed to delete logo', 'error');
            }
        }
        
        async function renameLogo(oldTeamName) {
            const newTeamName = prompt('Enter the new team name for this logo:', oldTeamName);
            if (!newTeamName || newTeamName.trim() === '' || newTeamName.trim() === oldTeamName) return;
            
            try {
                const resp = await fetch('/api/logos/rename', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({old_team_name: oldTeamName, new_team_name: newTeamName.trim()})
                });
                const data = await resp.json();
                
                if (data.success) {
                    showToast('Logo renamed from ' + oldTeamName + ' to ' + newTeamName.trim(), 'success');
                    loadApprovedLogos();
                } else {
                    showToast('Error: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch(e) {
                showToast('Failed to rename logo', 'error');
            }
        }
        
        // Load active cycle info
        (async function() {
            try {
                const resp = await fetch('/api/active-cycle');
                const data = await resp.json();
                const container = document.getElementById('active-cycle-info');
                if (container && data.active) {
                    const daysLeft = Math.ceil((new Date(data.end_date) - new Date()) / (1000*60*60*24));
                    container.innerHTML = `
                        <div style="padding: 15px; background: rgba(0,212,255,0.1); border: 1px solid var(--echo-cyan); border-radius: 8px; margin-bottom: 15px;">
                            <div style="font-family: 'Orbitron', sans-serif; color: var(--echo-cyan); font-size: 1.1em; margin-bottom: 8px;">Active Cycle: ${data.name}</div>
                            <div style="color: var(--echo-text-dim);">Started: ${data.start_date} | Ends: ${data.end_date} (${daysLeft > 0 ? daysLeft + ' days left' : 'Ending soon'})</div>
                        </div>
                    `;
                } else if (container) {
                    container.innerHTML = '<div style="color: var(--echo-text-dim); margin-bottom: 10px;">No active cycle. Start one below.</div>';
                }
            } catch(e) {}
        })();
        
// Register Service Worker for PWA
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(() => {});
        }
        
        // Copy avatar URL to clipboard
        async function copyAvatarUrl(btn, avatarUrl) {
            try {
                await navigator.clipboard.writeText(avatarUrl);
                btn.classList.add('copied');
                showToast('Avatar URL copied!', 'success');
                setTimeout(() => {
                    btn.classList.remove('copied');
                }, 2000);
            } catch(e) {
                // Fallback for older browsers
                const textarea = document.createElement('textarea');
                textarea.value = avatarUrl;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                btn.classList.add('copied');
                showToast('Avatar URL copied!', 'success');
                setTimeout(() => {
                    btn.classList.remove('copied');
                }, 2000);
            }
        }
        
        // Chat functionality
        const chatIntervals = {};
        const chatMentionableUsers = {};  // match_id -> array of {id, name, avatar}
        const chatMentionableRoles = {};  // match_id -> array of {id, name, color}
        
        function toggleChat(matchId, btn) {
            // Navigate directly to the match's broadcast tab
            switchTab('match-' + matchId);
        }
        
        function toggleChatGroup(matchId) {
            // Legacy function - no longer used with new individual tabs
            // Keep for backwards compatibility, just switch to the match tab
            switchTab('match-' + matchId);
        }
        
        async function loadChatMessages(matchId) {
            const messagesDiv = document.getElementById('chat-messages-' + matchId);
            if (!messagesDiv) return;
            
            try {
                const resp = await fetch('/api/chat/messages?match_id=' + encodeURIComponent(matchId));
                const data = await resp.json();
                
                if (!data.success) {
                    messagesDiv.innerHTML = '<div class="chat-error">' + (data.error || 'Failed to load messages') + '</div>';
                    return;
                }
                
                if (!data.channel_exists) {
                    messagesDiv.innerHTML = '<div class="chat-info">Private channel not created yet. Create the channel first.</div>';
                    return;
                }
                
                // Store mentionable users and roles for this match
                if (data.mentionable_users) {
                    chatMentionableUsers[matchId] = data.mentionable_users;
                }
                if (data.mentionable_roles) {
                    chatMentionableRoles[matchId] = data.mentionable_roles;
                }
                
                if (data.messages.length === 0) {
                    messagesDiv.innerHTML = '<div class="chat-info">No messages yet. Say hello!</div>';
                    return;
                }
                
                // Build messages HTML
                let html = '';
                let lastAuthor = null;
                let lastMsgDate = null;
                
                // Store messages for reply preview lookup
                const messagesMap = {};
                for (const msg of data.messages) {
                    messagesMap[msg.id] = msg;
                }
                
                for (const msg of data.messages) {
                    const msgDate = new Date(msg.timestamp);
                    const today = new Date();
                    const isToday = msgDate.toDateString() === today.toDateString();
                    const yesterday = new Date(today);
                    yesterday.setDate(yesterday.getDate() - 1);
                    const isYesterday = msgDate.toDateString() === yesterday.toDateString();
                    
                    // Show date separator if different day
                    const currentDateStr = msgDate.toDateString();
                    if (currentDateStr !== lastMsgDate) {
                        let dateLabel;
                        if (isToday) {
                            dateLabel = 'Today';
                        } else if (isYesterday) {
                            dateLabel = 'Yesterday';
                        } else {
                            dateLabel = msgDate.toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'});
                        }
                        html += `<div class="chat-date-separator"><span>${dateLabel}</span></div>`;
                        lastMsgDate = currentDateStr;
                        lastAuthor = null; // Reset author grouping on new day
                    }
                    
                    // If this message is a reply, reset author grouping
                    const isReply = msg.reply_to_id && messagesMap[msg.reply_to_id];
                    if (isReply) {
                        lastAuthor = null;
                    }
                    
                    const isNewAuthor = msg.author_id !== lastAuthor;
                    lastAuthor = msg.author_id;
                    
                    const time = msgDate.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
                    const botClass = msg.is_bot ? ' chat-msg-bot' : '';
                    
                    // Format content with images, links, and mentions
                    const formattedContent = formatMessageContent(msg.content);
                    
                    // Build reply reference HTML if this is a reply
                    let replyRefHtml = '';
                    if (isReply) {
                        const refMsg = messagesMap[msg.reply_to_id];
                        const refText = refMsg.content.length > 60 ? refMsg.content.substring(0, 60) + '...' : refMsg.content;
                        replyRefHtml = `<div class="chat-msg-reply-ref" onclick="scrollToMessage('${matchId}', '${msg.reply_to_id}')">
                            <span class="chat-msg-reply-ref-icon">↩</span>
                            <span class="chat-msg-reply-ref-author">${escapeHtml(refMsg.author_name)}</span>
                            <span class="chat-msg-reply-ref-text">${escapeHtml(refText)}</span>
                        </div>`;
                    }
                    
                    // Reply button (shown on hover via CSS)
                    const replyBtn = `<div class="chat-msg-actions">
                        <button class="chat-action-btn" onclick="startReply('${matchId}', '${msg.id}', '${escapeHtml(msg.author_name).replace(/'/g, "\\\\'")}', '${escapeHtml(msg.content.substring(0, 80)).replace(/'/g, "\\\\'")}')">↩ Reply</button>
                    </div>`;
                    
                    if (isNewAuthor) {
                        html += `<div class="chat-msg-wrapper" data-msg-id="${msg.id}"><div class="chat-msg${botClass}">
                            ${replyBtn}
                            <img src="${msg.author_avatar}" class="chat-avatar" alt="">
                            <div class="chat-content">
                                ${replyRefHtml}
                                <div class="chat-header">
                                    <span class="chat-author">${escapeHtml(msg.author_name)}</span>
                                    <span class="chat-time">${time}</span>
                                </div>
                                <div class="chat-text">${formattedContent}</div>
                            </div>
                        </div></div>`;
                    } else {
                        html += `<div class="chat-msg-wrapper" data-msg-id="${msg.id}"><div class="chat-msg chat-msg-continued${botClass}">
                            ${replyBtn}
                            <div class="chat-content">
                                ${replyRefHtml}
                                <div class="chat-text">${formattedContent}</div>
                            </div>
                        </div></div>`;
                    }
                }
                
                const wasAtBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop <= messagesDiv.clientHeight + 50;
                messagesDiv.innerHTML = html;
                
                // Auto-scroll to bottom if user was already at bottom
                if (wasAtBottom) {
                    messagesDiv.scrollTop = messagesDiv.scrollHeight;
                }
            } catch (e) {
                messagesDiv.innerHTML = '<div class="chat-error">Network error loading messages</div>';
            }
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function formatMentions(text) {
            // Highlight @mentions in the text
            return text.replace(/@([^ @]+)/g, '<span class="chat-mention">@$1</span>');
        }
        
        function formatMessageContent(rawText) {
            // Image URL patterns
            const imageExtensions = /\\.(jpg|jpeg|png|gif|webp|bmp)(\\?[^\\s]*)?$/i;
            
            // Find all URLs in the text
            const urlPattern = /(https?:\\/\\/[^\\s<]+)/gi;
            let images = [];
            let textParts = [];
            let lastIndex = 0;
            let match;
            
            // Reset regex
            urlPattern.lastIndex = 0;
            
            // Process text to extract URLs
            while ((match = urlPattern.exec(rawText)) !== null) {
                const url = match[0];
                const isImage = imageExtensions.test(url) || 
                               url.includes('cdn.discordapp.com/attachments') ||
                               url.includes('media.discordapp.net/attachments') ||
                               url.includes('media.discordapp.net/ephemeral');
                
                // Add text before this URL
                if (match.index > lastIndex) {
                    textParts.push({type: 'text', content: rawText.substring(lastIndex, match.index)});
                }
                
                if (isImage) {
                    images.push(url);
                } else {
                    textParts.push({type: 'link', content: url});
                }
                
                lastIndex = match.index + url.length;
            }
            
            // Add remaining text
            if (lastIndex < rawText.length) {
                textParts.push({type: 'text', content: rawText.substring(lastIndex)});
            }
            
            // Build formatted HTML
            let formattedText = '';
            for (const part of textParts) {
                if (part.type === 'text') {
                    // Escape HTML and format mentions
                    formattedText += formatMentions(escapeHtml(part.content));
                } else if (part.type === 'link') {
                    // Render as clickable link
                    formattedText += `<a href="${escapeHtml(part.content)}" target="_blank" rel="noopener" style="color: var(--echo-cyan); word-break: break-all;">${escapeHtml(part.content)}</a>`;
                }
            }
            
            // Add images at the end
            if (images.length > 0) {
                if (images.length === 1) {
                    formattedText += `<img src="${escapeHtml(images[0])}" class="chat-image" onclick="openLightbox(this.src)" alt="Image" loading="lazy" onerror="this.style.display='none'">`;
                } else {
                    formattedText += '<div class="chat-images-container">';
                    for (const img of images) {
                        formattedText += `<img src="${escapeHtml(img)}" class="chat-image" onclick="openLightbox(this.src)" alt="Image" loading="lazy" onerror="this.style.display='none'">`;
                    }
                    formattedText += '</div>';
                }
            }
            
            return formattedText || '&nbsp;';  // Return non-breaking space if empty
        }
        
        function openLightbox(imageUrl) {
            const lightbox = document.createElement('div');
            lightbox.className = 'image-lightbox';
            lightbox.innerHTML = `<img src="${imageUrl}" alt="Full size image">`;
            lightbox.onclick = () => lightbox.remove();
            document.body.appendChild(lightbox);
            // Close on Escape key
            const closeOnEsc = (e) => {
                if (e.key === 'Escape') {
                    lightbox.remove();
                    document.removeEventListener('keydown', closeOnEsc);
                }
            };
            document.addEventListener('keydown', closeOnEsc);
        }
        
        function convertMentionsToDiscord(text, matchId) {
            // Convert @username to <@user_id> and @role to <@&role_id> for Discord
            const users = chatMentionableUsers[matchId] || [];
            const roles = chatMentionableRoles[matchId] || [];
            let result = text;
            
            // Convert role mentions first (to avoid partial matches with usernames)
            // Match both "@Team: Name" and "@Name" for team roles
            for (const role of roles) {
                // Try full role name first
                const fullRegex = new RegExp('@' + escapeRegex(role.name) + '(?![\\\\w])', 'gi');
                result = result.replace(fullRegex, '<@&' + role.id + '>');
                // Also try display name (without Team: prefix)
                if (role.displayName) {
                    const shortRegex = new RegExp('@' + escapeRegex(role.displayName) + '(?![\\\\w])', 'gi');
                    result = result.replace(shortRegex, '<@&' + role.id + '>');
                }
            }
            
            // Convert user mentions
            for (const user of users) {
                const regex = new RegExp('@' + escapeRegex(user.name) + '(?![\\\\w])', 'gi');
                result = result.replace(regex, '<@' + user.id + '>');
            }
            
            return result;
        }
        
        function escapeRegex(str) {
            return str.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        }
        
        // Track current reply state per match
        const chatReplyState = {};  // matchId -> {messageId, authorName, content}
        
        async function sendChatMessage(matchId, event) {
            if (event) event.preventDefault();
            
            const input = document.getElementById('chat-input-' + matchId);
            const btn = document.getElementById('chat-send-' + matchId);
            if (!input || !btn) return;
            
            let message = input.value.trim();
            if (!message) return;
            
            // Convert @mentions to Discord format
            message = convertMentionsToDiscord(message, matchId);
            
            // Get reply_to_id if replying
            const replyToId = chatReplyState[matchId]?.messageId || null;
            
            btn.disabled = true;
            
            try {
                const resp = await fetch('/api/chat/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({match_id: matchId, message: message, reply_to_id: replyToId})
                });
                const data = await resp.json();
                
                if (data.success) {
                    input.value = '';
                    hideMentionSuggestions(matchId);
                    cancelReply(matchId);  // Clear reply state
                    // Immediately reload messages
                    await loadChatMessages(matchId);
                } else {
                    showToast(data.error || 'Failed to send message', 'error');
                }
            } catch (e) {
                showToast('Network error', 'error');
            }
            
            btn.disabled = false;
            input.focus();
        }
        
        function startReply(matchId, messageId, authorName, content) {
            // Store reply state
            chatReplyState[matchId] = {messageId, authorName, content};
            
            // Show reply preview
            const preview = document.getElementById('chat-reply-preview-' + matchId);
            if (preview) {
                const authorEl = preview.querySelector('.chat-reply-preview-author');
                const textEl = preview.querySelector('.chat-reply-preview-text');
                if (authorEl) authorEl.textContent = 'Replying to ' + authorName;
                if (textEl) textEl.textContent = content.length > 80 ? content.substring(0, 80) + '...' : content;
                preview.classList.add('active');
            }
            
            // Focus input
            const input = document.getElementById('chat-input-' + matchId);
            if (input) input.focus();
        }
        
        function cancelReply(matchId) {
            // Clear reply state
            delete chatReplyState[matchId];
            
            // Hide reply preview
            const preview = document.getElementById('chat-reply-preview-' + matchId);
            if (preview) {
                preview.classList.remove('active');
            }
        }
        
        function scrollToMessage(matchId, messageId) {
            const messagesDiv = document.getElementById('chat-messages-' + matchId);
            if (!messagesDiv) return;
            
            const msgEl = messagesDiv.querySelector(`[data-msg-id="${messageId}"]`);
            if (msgEl) {
                msgEl.scrollIntoView({behavior: 'smooth', block: 'center'});
                // Flash highlight effect
                msgEl.style.transition = 'background 0.3s';
                msgEl.style.background = 'rgba(0,212,255,0.2)';
                setTimeout(() => {
                    msgEl.style.background = '';
                }, 1500);
            }
        }
        
        function handleChatKeypress(matchId, event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendChatMessage(matchId);
            }
        }
        
        function handleChatInput(matchId, event) {
            const input = event.target;
            const value = input.value;
            const cursorPos = input.selectionStart;
            
            // Find if we're typing a mention (@ followed by partial name)
            const textBeforeCursor = value.substring(0, cursorPos);
            // Match @ followed by any non-space characters (to support "Team: Name")
            const mentionMatch = textBeforeCursor.match(/@([^ ]*)$/);
            
            if (mentionMatch) {
                const partial = mentionMatch[1].toLowerCase();
                showMentionSuggestions(matchId, partial, cursorPos - mentionMatch[0].length);
            } else {
                hideMentionSuggestions(matchId);
            }
        }
        
        function showMentionSuggestions(matchId, partial, mentionStart) {
            const suggestionsDiv = document.getElementById('chat-suggestions-' + matchId);
            if (!suggestionsDiv) return;
            
            const users = chatMentionableUsers[matchId] || [];
            const roles = chatMentionableRoles[matchId] || [];
            
            const filteredUsers = users.filter(u => u.name.toLowerCase().includes(partial));
            // Filter roles by displayName (team name without "Team:" prefix)
            const filteredRoles = roles.filter(r => 
                (r.displayName && r.displayName.toLowerCase().includes(partial)) ||
                r.name.toLowerCase().includes(partial)
            );
            
            if (filteredUsers.length === 0 && filteredRoles.length === 0) {
                suggestionsDiv.style.display = 'none';
                return;
            }
            
            let html = '';
            
            // Show roles first (with role icon/badge) - use displayName for easier typing
            for (const role of filteredRoles.slice(0, 4)) {
                const insertName = role.displayName || role.name;
                html += `<div class="mention-suggestion mention-role" onclick="insertMention('${matchId}', '${escapeHtml(insertName)}', ${mentionStart})">
                    <span class="mention-role-badge" style="background: ${role.color}">@</span>
                    <span>${escapeHtml(role.displayName || role.name)}</span>
                </div>`;
            }
            
            // Then show users
            for (const user of filteredUsers.slice(0, 6)) {
                html += `<div class="mention-suggestion" onclick="insertMention('${matchId}', '${escapeHtml(user.name)}', ${mentionStart})">
                    <img src="${user.avatar}" class="mention-avatar" alt="">
                    <span>${escapeHtml(user.name)}</span>
                </div>`;
            }
            
            suggestionsDiv.innerHTML = html;
            suggestionsDiv.style.display = 'block';
        }
        
        function hideMentionSuggestions(matchId) {
            const suggestionsDiv = document.getElementById('chat-suggestions-' + matchId);
            if (suggestionsDiv) {
                suggestionsDiv.style.display = 'none';
            }
        }
        
        function insertMention(matchId, name, mentionStart) {
            const input = document.getElementById('chat-input-' + matchId);
            if (!input) return;
            
            const value = input.value;
            const beforeMention = value.substring(0, mentionStart);
            const afterCursor = value.substring(input.selectionStart);
            
            input.value = beforeMention + '@' + name + ' ' + afterCursor;
            input.focus();
            
            // Set cursor after the inserted mention
            const newPos = mentionStart + name.length + 2;
            input.setSelectionRange(newPos, newPos);
            
            hideMentionSuggestions(matchId);
        }
        
        // Initialize active match tab chat on page load
        (function() {
            const activeMatchTab = document.querySelector('.tab-content.match-tab-content.active');
            if (activeMatchTab) {
                const matchId = activeMatchTab.dataset.matchId;
                if (matchId) {
                    loadChatMessages(matchId);
                    if (!chatIntervals[matchId]) {
                        chatIntervals[matchId] = setInterval(() => loadChatMessages(matchId), 5000);
                    }
                }
            }
        })();
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


def _build_match_card(match: dict, claims: list[dict], users: dict[int, str], current_user_id: int | None, is_lead: bool = False) -> str:
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
            
            # Show unclaim button if it's the user's own claim OR they're a lead
            if is_mine or is_lead:
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
            # Anyone logged in can claim an open slot
            if current_user_id:
                claim_btn = f'<button class="claim-btn" onclick="claimSlot(\'{match_id}\', \'{role}\', {slot})">Claim</button>'
                if is_lead:
                    # Leads also get an assign button
                    assign_btn = f'<button class="assign-btn" onclick="showAssignModal(\'{match_id}\', \'{role}\', {slot}, \'{label}\')">Assign</button>'
                    button = f'{claim_btn}{assign_btn}'
                else:
                    button = claim_btn
            else:
                button = ""
            return f'''
                <div class="claim-slot open">
                    <div class="slot-info">
                        <span class="role-label">{label}:</span>
                        <span class="open-text">Open</span>
                    </div>
                    <div class="slot-buttons">{button}</div>
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
    
    my_claim_attr = ' data-my-claim="true"' if has_my_claim else ''
    
    # Calculate additional filter attributes
    total_slots = 4  # caster1, caster2, camop, sideline
    filled_slots = len(claims)
    has_open = filled_slots < total_slots
    open_attr = ' data-has-open="true"' if has_open else ''
    status_attr = f' data-status="{status}"'
    match_type_attr = f' data-match-type="{match.get("match_type", "")}"' if match.get("match_type") else ''
    
    # Date and timestamp for filtering - use timestamp to derive normalized YYYY-MM-DD date
    match_timestamp = match.get("match_timestamp", 0)
    if match_timestamp:
        from datetime import datetime
        from dateutil import tz as dateutil_tz
        match_dt = datetime.fromtimestamp(match_timestamp, tz=dateutil_tz.gettz(config.TIMEZONE))
        normalized_date = match_dt.strftime("%Y-%m-%d")
    else:
        normalized_date = match.get("match_date", "")
    date_attr = f' data-date="{normalized_date}" data-timestamp="{match_timestamp}"'
    
    return f'''
        <div class="{card_class}"{my_claim_attr}{open_attr}{status_attr}{match_type_attr}{date_attr}>
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
    
    # Require login and crew role to view the page
    if not session:
        oauth_configured = config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET
        if oauth_configured:
            # Show login page
            return web.Response(text=f'''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{config.LEAGUE_NAME} - Broadcast Hub</title>
    
    <!-- Discord/Social Embed -->
    <meta property="og:type" content="website">
    <meta property="og:url" content="{config.WEB_PUBLIC_URL or f"https://casterbot.mooo.com:{config.WEB_PORT}"}">
    <meta property="og:title" content="Broadcast Hub">
    <meta property="og:description" content="Claim matches and manage casts for {config.LEAGUE_NAME}">
    <meta property="og:site_name" content="{config.LEAGUE_NAME}">
    <meta name="theme-color" content="#ff6a00">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="Broadcast Hub">
    <meta name="twitter:description" content="Claim matches and manage casts for {config.LEAGUE_NAME}">
    
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --echo-bg: #0a0a0f;
            --echo-card: #12121a;
            --echo-border: #2a2a3a;
            --echo-orange: #ff6a00;
            --echo-cyan: #00d4ff;
            --echo-text: #e4e4e7;
            --echo-text-dim: #8e9297;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', sans-serif;
            background: var(--echo-bg);
            color: var(--echo-text);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }}
        .login-container {{
            text-align: center;
            padding: 40px;
            background: var(--echo-card);
            border: 1px solid var(--echo-border);
            border-radius: 16px;
            max-width: 400px;
        }}
        h1 {{
            font-family: 'Orbitron', sans-serif;
            color: var(--echo-orange);
            margin-bottom: 16px;
        }}
        p {{
            color: var(--echo-text-dim);
            margin-bottom: 24px;
        }}
        .login-btn {{
            display: inline-block;
            background: linear-gradient(135deg, #5865F2, #7289DA);
            color: white;
            padding: 14px 32px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .login-btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(88,101,242,0.4);
        }}
    </style>
</head>
<body>
    <div class="login-container">
        <h1>{config.LEAGUE_NAME}</h1>
        <p>Login with Discord to access the Broadcast Hub.<br>Requires a Caster or CamOp role.</p>
        <a href="/login" class="login-btn">Login with Discord</a>
    </div>
</body>
</html>
            ''', content_type='text/html')
        else:
            return web.Response(text="OAuth not configured", status=500)
    
    # Check if user has a crew role
    is_crew = await _is_crew_member(bot, current_user_id)
    if not is_crew:
        display_name = session.get("global_name") or session["username"]
        return web.Response(text=f'''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CasterBot - Access Denied</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --echo-bg: #0a0a0f;
            --echo-card: #12121a;
            --echo-border: #2a2a3a;
            --echo-orange: #ff6a00;
            --echo-cyan: #00d4ff;
            --echo-text: #e4e4e7;
            --echo-text-dim: #8e9297;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', sans-serif;
            background: var(--echo-bg);
            color: var(--echo-text);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }}
        .denied-container {{
            text-align: center;
            padding: 40px;
            background: var(--echo-card);
            border: 1px solid var(--echo-border);
            border-radius: 16px;
            max-width: 450px;
        }}
        h1 {{
            font-family: 'Orbitron', sans-serif;
            color: #ed4245;
            margin-bottom: 16px;
        }}
        p {{
            color: var(--echo-text-dim);
            margin-bottom: 24px;
            line-height: 1.6;
        }}
        .user-info {{
            color: var(--echo-text);
            margin-bottom: 16px;
        }}
        .logout-btn {{
            display: inline-block;
            background: var(--echo-border);
            color: var(--echo-text);
            padding: 12px 24px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
        }}
        .logout-btn:hover {{
            background: #3a3a4a;
        }}
    </style>
</head>
<body>
    <div class="denied-container">
        <h1>ACCESS DENIED</h1>
        <p class="user-info">Logged in as <strong>{display_name}</strong></p>
        <p>You need a Caster, CamOp, or Training role to access the casting schedule.</p>
        <a href="/logout" class="logout-btn">Logout</a>
    </div>
</body>
</html>
        ''', content_type='text/html')
    
    # Check if user is admin
    is_admin = await _is_admin(bot, current_user_id) if current_user_id else False
    
    # Determine active tab
    active_tab = request.query.get("tab", "schedule")
    valid_tabs = ["schedule", "leaderboard"]
    if is_admin:
        valid_tabs.append("admin")
    # Match tabs (match-*) are also valid
    if active_tab not in valid_tabs and not active_tab.startswith("match-"):
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
        
        # Check for custom profile picture first
        custom_pic = await db.get_profile_picture(user_id)
        if custom_pic:
            base_url = config.WEB_PUBLIC_URL.rstrip("/") if config.WEB_PUBLIC_URL else ""
            avatar_url = f"{base_url}/profile-pic/{user_id}"
            has_custom_pic = "true"
        elif avatar_hash:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
            has_custom_pic = "false"
        else:
            # Default avatar
            default_avatar = (user_id >> 22) % 6
            avatar_url = f"https://cdn.discordapp.com/embed/avatars/{default_avatar}.png"
            has_custom_pic = "false"
        
        user_bar = f'''
            <div class="user-bar" data-user-id="{user_id}">
                <div class="profile-menu-container">
                    <div class="profile-menu-trigger" onclick="toggleProfileMenu(event)">
                        <img src="{avatar_url}" class="user-avatar" id="user-avatar-img" alt="">
                        <span class="user-name">{display_name}</span>
                    </div>
                    <div class="profile-dropdown" id="profile-dropdown">
                        <label class="profile-dropdown-item" for="profile-pic-input">
                            &#128247; Change Profile Picture
                        </label>
                        <button class="profile-dropdown-item" onclick="resetProfilePic()" style="display: {has_custom_pic == 'true' and 'block' or 'none'};" id="reset-pic-btn">
                            &#8634; Reset to Discord Avatar
                        </button>
                        <div class="profile-dropdown-divider"></div>
                        <a href="/logout" class="profile-dropdown-item">&#128682; Logout</a>
                    </div>
                    <input type="file" id="profile-pic-input" class="profile-pic-input" accept="image/*" onchange="uploadProfilePic(this)">
                </div>
                <button class="refresh-btn" onclick="refreshPage()">&#x21bb; Refresh</button>
            </div>
        '''
    else:
        oauth_configured = config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET
        if oauth_configured:
            user_bar = '''
                <div class="user-bar">
                    <span style="color: #8e9297;">Login to claim matches</span>
                    <a href="/login" class="login-btn">Login with Discord</a>
                    <button class="refresh-btn" onclick="refreshPage()">&#x21bb; Refresh</button>
                </div>
            '''
        else:
            user_bar = '''
                <div class="user-bar">
                    <span style="color: #8e9297;">View-only mode (OAuth not configured)</span>
                    <button class="refresh-btn" onclick="refreshPage()">&#x21bb; Refresh</button>
                </div>
            '''
    
    # Build schedule content - fetch matches first (needed for filter bar)
    matches = await db.get_all_matches_sorted_by_time()
    
    # Build filter bar (only for logged in users)
    if session:
        # Collect unique match types for filter options
        match_types = set()
        match_types.add("Challenge")  # Always include Challenge
        for match in matches:
            if match.get("match_type"):
                match_types.add(match["match_type"])
        
        match_type_options = ''.join(f'<option value="type:{mt}">{mt}</option>' for mt in sorted(match_types))
        
        # Simplified filter bar with quick chips and dropdown
        filter_bar = f'''
            <div class="filter-bar-wrapper">
                <div class="filter-bar">
                    <div class="filter-quick">
                        <span class="filter-chip active" onclick="quickFilter('all', this)">All</span>
                        <span class="filter-chip" onclick="quickFilter('my-claims', this)">Mine</span>
                        <span class="filter-chip" onclick="quickFilter('open-slots', this)">Open</span>
                        <span class="filter-chip" onclick="quickFilter('today', this)">Today</span>
                        <span class="filter-chip" onclick="quickFilter('live-soon', this)">Live</span>
                    </div>
                    <button class="filter-toggle" onclick="toggleFilters(this)" title="More filters">
                        <svg viewBox="0 0 24 24"><path d="M10 18h4v-2h-4v2zM3 6v2h18V6H3zm3 7h12v-2H6v2z"/></svg>
                    </button>
                    <div class="filter-controls" id="filter-controls">
                        <select class="filter-select" id="schedule-filter" onchange="applyScheduleFilter()">
                            <option value="all">All Matches</option>
                            <optgroup label="Status">
                                <option value="my-claims">My Claims Only</option>
                                <option value="open-slots">Open Slots</option>
                                <option value="live-soon">Live / Soon</option>
                            </optgroup>
                            <optgroup label="Date">
                                <option value="today">Today</option>
                                <option value="tomorrow">Tomorrow</option>
                            </optgroup>
                            <optgroup label="Type">{match_type_options}</optgroup>
                        </select>
                        <input type="datetime-local" class="filter-datetime" id="filter-datetime" onchange="applyDateTimeFilter()" title="Filter by date/time">
                        <button class="filter-clear-btn" onclick="clearFilters()">Reset All</button>
                    </div>
                </div>
            </div>
        '''
    else:
        filter_bar = ''
    
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
            guild = bot.get_guild(config.GUILD_ID)
            for user_id in all_user_ids:
                try:
                    # Try guild member first (more reliable)
                    if guild:
                        member = guild.get_member(user_id)
                        if member:
                            users[user_id] = member.display_name
                            continue
                    # Fall back to user lookup
                    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                    users[user_id] = user.display_name if hasattr(user, "display_name") else user.name
                except Exception:
                    users[user_id] = f"User #{user_id}"
        
        cards = []
        for match in matches:
            claims = match_claims.get(match["match_id"], [])
            cards.append(_build_match_card(match, claims, users, current_user_id, is_admin))
        
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
        guild = bot.get_guild(config.GUILD_ID) if bot else None
        for idx, entry in enumerate(leaderboard_data, start=1):
            user_id = entry["user_id"]
            cast_count = entry["cast_count"]
            
            # Get user info
            user_name = f"User #{user_id}"
            avatar_url = await get_user_avatar_url(bot, user_id)
            
            if bot:
                try:
                    # Try guild member first
                    member = guild.get_member(user_id) if guild else None
                    if member:
                        user_name = member.display_name
                    else:
                        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                        user_name = user.display_name if hasattr(user, "display_name") else user.name
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
            
            # Escape quotes in avatar URL for JS
            avatar_url_escaped = avatar_url.replace("'", "\\'")
            rows.append(f'''
                <div class="{row_class}">
                    <div class="rank {rank_class}">{idx}</div>
                    <div class="caster-info">
                        <button class="caster-avatar-btn" onclick="copyAvatarUrl(this, '{avatar_url_escaped}')" title="Copy avatar URL">
                            <img src="{avatar_url}" class="caster-avatar" alt="">
                        </button>
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
    
    # Build broadcast tabs - individual tabs for each match user has claimed
    broadcast_tabs = ''
    broadcast_tab_contents = ''
    if current_user_id and matches:
        tab_buttons = []
        tab_contents = []
        for match in matches:
            # Check if user has a claim on this match OR is admin
            claims = match_claims.get(match["match_id"], [])
            user_has_claim = any(c["user_id"] == current_user_id for c in claims)
            
            # Show match if user has a claim (or is admin)
            if user_has_claim or is_admin:
                match_id = match["match_id"]
                simple_id = match.get("simple_id", "?")
                team_a = match.get("team_a", "TBD")
                team_b = match.get("team_b", "TBD")
                match_time = match.get("match_time", "")
                match_date = match.get("match_date", "")
                
                # Check if requirements met for controls
                has_caster = any(c for c in claims if c["role"] == "caster")
                has_camop = any(c for c in claims if c["role"] == "camop")
                has_channel = bool(match.get("private_channel_id"))
                stream_channel = match.get("stream_channel")
                can_create = has_caster and has_camop and not has_channel
                can_ready = has_channel
                can_go_live = has_channel and has_caster and has_camop and stream_channel
                
                create_disabled = "" if can_create else "disabled"
                ready_disabled = "" if can_ready else "disabled"
                live_disabled = "" if can_go_live else "disabled"
                
                status_dot_class = "has-channel" if has_channel else "no-channel"
                match_timestamp = match.get("match_timestamp", 0)
                
                # Build stream channel options
                stream_warning = "warning" if not stream_channel else ""
                stream_options = '<option value="">⚠️ Select Channel</option>'
                for ch_num, (label, url) in config.STREAM_CHANNELS.items():
                    selected = "selected" if stream_channel == ch_num else ""
                    stream_options += f'<option value="{ch_num}" {selected}>{label}</option>'
                
                # Build chat section (only if channel exists)
                if has_channel:
                    chat_section = f'''
                        <div class="match-chat-section">
                            <div class="chat-messages" id="chat-messages-{match_id}">
                                <div class="chat-info">Loading messages...</div>
                            </div>
                            <div class="chat-input-container">
                                <div class="chat-reply-preview" id="chat-reply-preview-{match_id}">
                                    <div class="chat-reply-preview-content">
                                        <div class="chat-reply-preview-author">Replying to...</div>
                                        <div class="chat-reply-preview-text"></div>
                                    </div>
                                    <button type="button" class="chat-reply-cancel" onclick="cancelReply('{match_id}')">✕</button>
                                </div>
                                <div class="mention-suggestions" id="chat-suggestions-{match_id}"></div>
                                <form class="chat-input-row" onsubmit="sendChatMessage('{match_id}', event); return false;">
                                    <input type="text" class="chat-input" id="chat-input-{match_id}" 
                                           placeholder="Type a message... (Use @ to mention)"
                                           onkeypress="handleChatKeypress('{match_id}', event)"
                                           oninput="handleChatInput('{match_id}', event)" autocomplete="off">
                                    <button type="submit" class="chat-send-btn" id="chat-send-{match_id}">Send</button>
                                </form>
                            </div>
                        </div>
                    '''
                else:
                    chat_section = '''
                        <div class="match-no-chat">
                            <p>Create a channel to enable crew chat</p>
                        </div>
                    '''
                
                # Sidebar tab button
                match_tab_active = "active" if active_tab == f"match-{match_id}" else ""
                tab_buttons.append(f'''
                    <button class="tab-match-btn {match_tab_active}" onclick="switchTab('match-{match_id}')" data-tab="match-{match_id}" data-timestamp="{match_timestamp}">
                        <span class="match-label"><span class="match-status-dot {status_dot_class}"></span>#{simple_id} - {team_a} vs {team_b}</span>
                        <span class="match-time-label">{match_date} {match_time}</span>
                    </button>
                ''')
                
                # Tab content panel
                tab_contents.append(f'''
                    <div id="tab-match-{match_id}" class="tab-content match-tab-content {match_tab_active}" data-match-id="{match_id}">
                        <div class="match-panel-header">
                            <h2>#{simple_id} - {team_a} vs {team_b}</h2>
                            <p class="match-panel-time">{match_date} {match_time}</p>
                        </div>
                        <div class="match-panel-controls">
                            <div class="stream-row">
                                <span class="stream-label">Stream:</span>
                                <select class="stream-select {stream_warning}" onchange="setStreamChannel('{match_id}', this)">
                                    {stream_options}
                                </select>
                            </div>
                            <div class="broadcast-btns">
                                <button class="broadcast-btn create" onclick="createChannel('{match_id}')" {create_disabled}>Create Channel</button>
                                <button class="broadcast-btn golive" onclick="goLive('{match_id}')" {live_disabled}>Go Live</button>
                                <button class="broadcast-btn ready" onclick="crewReady('{match_id}')" {ready_disabled}>Crew Ready</button>
                            </div>
                        </div>
                        {chat_section}
                    </div>
                ''')
        
        if tab_buttons:
            broadcast_tabs = f'''
                <div class="tab-category expanded" id="broadcast-category">
                    <div class="tab-category-header" onclick="toggleCategory('broadcast-category')">
                        <span>Broadcasts</span>
                        <span class="tab-category-arrow">β–Ό</span>
                    </div>
                    <div class="tab-category-items">
                        {"".join(tab_buttons)}
                    </div>
                </div>
            '''
            broadcast_tab_contents = "\n".join(tab_contents)
        else:
            broadcast_tabs = f'''
                <div class="tab-category" id="broadcast-category">
                    <div class="tab-category-header" onclick="toggleCategory('broadcast-category')">
                        <span>Broadcasts</span>
                        <span class="tab-category-arrow">β–Ό</span>
                    </div>
                    <div class="tab-category-items">
                        <div class="tab-category-empty">No matches claimed</div>
                    </div>
                </div>
            '''
    else:
        broadcast_tabs = f'''
            <div class="tab-category" id="broadcast-category">
                <div class="tab-category-header" onclick="toggleCategory('broadcast-category')">
                    <span>Broadcasts</span>
                    <span class="tab-category-arrow">β–Ό</span>
                </div>
                <div class="tab-category-items">
                    <div class="tab-category-empty">Login to see your matches</div>
                </div>
            </div>
        '''
    
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
                        <h3>Force Delete Match</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-desc">Force delete a match without counting toward leaderboard. Deletes private channel and claim message.</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>Match</label>
                                <select class="admin-select" id="force-delete-match">
                                    {match_options}
                                </select>
                            </div>
                            <button class="admin-btn danger" onclick="adminForceDelete()">Force Delete</button>
                        </div>
                        <div class="admin-result" id="result-force-delete"></div>
                    </div>
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Force Create Match</h3>
                    </div>
                    <div class="admin-row">
                        <div class="admin-row-desc">Create a match manually (not from the Google Sheet). Useful for scrimmages, special events, etc.</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>Team A</label>
                                <input type="text" class="admin-input" id="force-create-team-a" placeholder="Team name">
                            </div>
                            <div class="admin-input-group">
                                <label>Team B</label>
                                <input type="text" class="admin-input" id="force-create-team-b" placeholder="Team name">
                            </div>
                            <div class="admin-input-group">
                                <label>Date/Time</label>
                                <input type="datetime-local" class="admin-input" id="force-create-datetime">
                            </div>
                            <div class="admin-input-group">
                                <label>Type (optional)</label>
                                <input type="text" class="admin-input" id="force-create-type" placeholder="e.g. Scrimmage, Playoff">
                            </div>
                            <button class="admin-btn success" onclick="adminForceCreateMatch()">Create Match</button>
                        </div>
                        <div class="admin-result" id="result-force-create"></div>
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
                        <h3>Leaderboard Cycles</h3>
                    </div>
                    <div class="admin-row" id="active-cycle-info"></div>
                    <div class="admin-row">
                        <div class="admin-row-desc">Start a new cycle. Leaderboard will auto-archive when the cycle ends.</div>
                        <div class="admin-form">
                            <div class="admin-input-group">
                                <label>Cycle Name</label>
                                <input type="text" class="admin-input" id="cycle-name" placeholder="e.g. Season 5 Cycle 1" style="width: 200px;">
                            </div>
                            <div class="admin-input-group">
                                <label>Weeks</label>
                                <input type="number" class="admin-input" id="cycle-weeks" placeholder="5" min="1" value="5" style="width: 80px;">
                            </div>
                            <button class="admin-btn success" onclick="adminStartCycle()">Start Cycle</button>
                            <button class="admin-btn danger" onclick="adminEndCycle()">End Now</button>
                        </div>
                        <div class="admin-result" id="result-cycle"></div>
                    </div>
                </div>
            </div>
        '''
    else:
        admin_tab_btn = ''
        admin_tab_content = ''
    
    # Build logos review tab (only for admins)
    if is_admin and config.TEAM_LOGO_CHANNEL_ID:
        logos_tab_btn = '<button class="tab-btn admin" onclick="switchTab(\'logos\')">Team Logos</button>'
        logos_tab_content = '''
            <div id="tab-logos" class="tab-content">
                <div class="admin-warning">
                    <strong>Team Logo Review</strong> — Review and approve team logo submissions.
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Pending Submissions</h3>
                        <button class="admin-btn" onclick="loadPendingLogos()">Refresh</button>
                    </div>
                    <div id="pending-logos-container">
                        <div class="loading">Loading pending submissions...</div>
                    </div>
                </div>
                
                <div class="admin-section">
                    <div class="admin-section-header">
                        <h3>Approved Logos</h3>
                    </div>
                    <div id="approved-logos-container">
                        <div class="loading">Loading approved logos...</div>
                    </div>
                </div>
            </div>
        '''
    else:
        logos_tab_btn = ''
        logos_tab_content = ''
    
    html = (HTML_TEMPLATE
        .replace("{league_name}", config.LEAGUE_NAME)
        .replace("{site_url}", config.WEB_PUBLIC_URL or f"https://{config.WEB_HOST}:{config.WEB_PORT}")
        .replace("{user_bar}", user_bar)
        .replace("{season_badge}", season_badge)
        .replace("{filter_bar}", filter_bar)
        .replace("{admin_tab_btn}", admin_tab_btn)
        .replace("{admin_tab_content}", admin_tab_content)
        .replace("{logos_tab_btn}", logos_tab_btn)
        .replace("{logos_tab_content}", logos_tab_content)
        .replace("{content}", content)
        .replace("{cycle_selector}", cycle_selector)
        .replace("{cycle_info}", cycle_info)
        .replace("{leaderboard_content}", leaderboard_content)
        .replace("{cycle_history}", cycle_history)
        .replace("{broadcast_tabs}", broadcast_tabs)
        .replace("{broadcast_tab_contents}", broadcast_tab_contents)
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
    assign_user_id = data.get("assign_user_id")  # Optional: for leads to assign others
    
    # Convert assign_user_id to int if it's a string (preserves precision from JS)
    if assign_user_id and isinstance(assign_user_id, str):
        try:
            assign_user_id = int(assign_user_id)
        except ValueError:
            return web.json_response({"success": False, "error": "Invalid user ID"}, status=400)
    
    if not all([match_id, role, slot]):
        return web.json_response({"success": False, "error": "Missing parameters"}, status=400)
    
    user_id = session["user_id"]
    
    # If assigning someone else, check if requester is a lead
    if assign_user_id and assign_user_id != user_id:
        bot = request.app.get("bot")
        is_lead = False
        if bot and (config.WEB_LEAD_ROLE_ID or config.STAFF_ROLE_ID):
            guild = bot.get_guild(config.GUILD_ID)
            if guild:
                member = guild.get_member(user_id)
                allowed_admin_roles = [r for r in [config.WEB_LEAD_ROLE_ID, config.STAFF_ROLE_ID] if r]
                if member and any(rid in [r.id for r in member.roles] for rid in allowed_admin_roles):
                    is_lead = True
        
        if not is_lead:
            return web.json_response({"success": False, "error": "Only leads can assign others"}, status=403)
        
        # Use the assigned user ID
        user_id = assign_user_id
    
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
    
    # Check if user owns this slot OR is a lead
    current_holder = await db.get_slot_holder(match_id, role, slot)
    is_own_slot = (current_holder == user_id)
    
    # Check if user is a lead
    is_lead = False
    bot = request.app.get("bot")
    if bot and (config.WEB_LEAD_ROLE_ID or config.STAFF_ROLE_ID):
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            allowed_admin_roles = [r for r in [config.WEB_LEAD_ROLE_ID, config.STAFF_ROLE_ID] if r]
            if member and any(rid in [r.id for r in member.roles] for rid in allowed_admin_roles):
                is_lead = True
    
    # Allow if it's their own slot OR they're a lead
    if not is_own_slot and not is_lead:
        return web.json_response({"success": False, "error": "You can only unclaim your own slots"}, status=403)
    
    # Unclaim the slot - use current_holder if lead is removing someone else's claim
    target_user_id = current_holder if (is_lead and not is_own_slot) else user_id
    await db.unclaim_slot(match_id, target_user_id, role, slot)
    
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
    
    # Check if user is lead OR has a claim on this match
    user_id = session["user_id"]
    claims = await db.get_claims(match_id)
    has_claim = any(c["user_id"] == user_id for c in claims)
    
    is_lead = False
    if config.WEB_LEAD_ROLE_ID or config.STAFF_ROLE_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            allowed_admin_roles = [r for r in [config.WEB_LEAD_ROLE_ID, config.STAFF_ROLE_ID] if r]
            if member and any(rid in [r.id for r in member.roles] for rid in allowed_admin_roles):
                is_lead = True
    
    if not is_lead and not has_claim:
        return web.json_response({"success": False, "error": "Only crew members can manage broadcasts"}, status=403)
    
    # Check if channel already exists
    if match.get("private_channel_id"):
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            existing = guild.get_channel(match["private_channel_id"])
            if existing:
                return web.json_response({"success": False, "error": "Channel already exists"}, status=400)
        await db.clear_private_channel(match_id)
    
    # Check requirements (reuse claims from above)
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
    
    # Check if user is lead OR has a claim on this match
    user_id = session["user_id"]
    claims = await db.get_claims(match_id)
    has_claim = any(c["user_id"] == user_id for c in claims)
    
    is_lead = False
    if config.WEB_LEAD_ROLE_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and config.WEB_LEAD_ROLE_ID in [r.id for r in member.roles]:
                is_lead = True
    
    if not is_lead and not has_claim:
        return web.json_response({"success": False, "error": "Only crew members can manage broadcasts"}, status=403)
    
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
    
    # Check if user is lead OR has a claim on this match
    user_id = session["user_id"]
    match_claims = await db.get_claims(match_id)
    has_claim = any(c["user_id"] == user_id for c in match_claims)
    
    is_lead = False
    if config.WEB_LEAD_ROLE_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and config.WEB_LEAD_ROLE_ID in [r.id for r in member.roles]:
                is_lead = True
    
    if not is_lead and not has_claim:
        return web.json_response({"success": False, "error": "Only crew members can manage broadcasts"}, status=403)
    
    if not match.get("private_channel_id"):
        return web.json_response({"success": False, "error": "Create the channel first"}, status=400)
    
    # Reuse match_claims from above
    casters = [c for c in match_claims if c["role"] == "caster"]
    camops = [c for c in match_claims if c["role"] == "camop"]
    if not casters or not camops:
        return web.json_response({"success": False, "error": "Need at least 1 caster and 1 cam op"}, status=400)
    
    # Check stream channel is selected
    if not match.get("stream_channel"):
        return web.json_response({"success": False, "error": "Please select a stream channel first"}, status=400)
    
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
    
    # Use selected stream channel
    stream_channel = match.get("stream_channel")
    channel_label, twitch_url = config.STREAM_CHANNELS[stream_channel]
    announcement = f"# [EchoMasterLeague]({twitch_url}) We are live now casting {teams_text}"
    if live_ping:
        announcement += f"\n{live_ping}"
    
    await live_channel.send(announcement)
    return web.json_response({"success": True})


async def api_set_stream_channel_handler(request: web.Request) -> web.Response:
    """API endpoint to set the stream channel for a match."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    match_id = data.get("match_id")
    stream_channel = data.get("stream_channel")
    
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    if stream_channel is None or stream_channel not in config.STREAM_CHANNELS:
        return web.json_response({"success": False, "error": "Invalid stream channel"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    # Check if user is lead OR has a claim on this match
    user_id = session["user_id"]
    match_claims = await db.get_claims(match_id)
    has_claim = any(c["user_id"] == user_id for c in match_claims)
    
    is_lead = False
    if bot and config.WEB_LEAD_ROLE_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and config.WEB_LEAD_ROLE_ID in [r.id for r in member.roles]:
                is_lead = True
    
    if not is_lead and not has_claim:
        return web.json_response({"success": False, "error": "Only crew members can update stream channel"}, status=403)
    
    await db.set_stream_channel(match_id, stream_channel)
    return web.json_response({"success": True})


async def api_user_avatar_handler(request: web.Request) -> web.Response:
    """API endpoint to get a user's avatar URL."""
    user_id_str = request.query.get("user_id")
    if not user_id_str:
        return web.json_response({"success": False, "error": "Missing user_id parameter"}, status=400)
    
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"success": False, "error": "Invalid user_id"}, status=400)
    
    bot = request.app.get("bot")
    
    # Get avatar URL (custom pic or Discord avatar)
    avatar_url = await get_user_avatar_url(bot, user_id)
    
    # Get username and display name
    username = f"User #{user_id}"
    display_name = f"User #{user_id}"
    
    if bot:
        try:
            guild = bot.get_guild(config.GUILD_ID)
            member = guild.get_member(user_id) if guild else None
            
            if member:
                username = member.name
                display_name = member.display_name
            else:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                username = user.name
                display_name = user.display_name if hasattr(user, "display_name") else user.name
        except Exception:
            pass
    
    return web.json_response({
        "success": True,
        "user_id": user_id,
        "avatar_url": avatar_url,
        "username": username,
        "display_name": display_name
    })


async def api_proxy_avatar_handler(request: web.Request) -> web.Response:
    """Proxy endpoint to download avatar images (bypasses CORS). Requires login."""
    import aiohttp as aiohttp_client
    
    # Require login
    session = _get_session(request)
    if not session:
        return web.Response(text="Login required", status=401)
    
    url = request.query.get("url")
    if not url:
        return web.Response(text="Missing url parameter", status=400)
    
    # Only allow Discord CDN URLs for security
    allowed_domains = ["cdn.discordapp.com", "discord.com"]
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.netloc not in allowed_domains:
        return web.Response(text="Invalid URL domain", status=400)
    
    try:
        async with aiohttp_client.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return web.Response(text="Failed to fetch image", status=resp.status)
                
                content_type = resp.headers.get("Content-Type", "image/png")
                data = await resp.read()
                
                return web.Response(
                    body=data,
                    content_type=content_type,
                    headers={
                        "Content-Disposition": "attachment",
                        "Cache-Control": "public, max-age=3600"
                    }
                )
    except Exception as e:
        log.error(f"Avatar proxy error: {e}")
        return web.Response(text="Failed to fetch image", status=500)


async def get_user_avatar_url(bot, user_id: int) -> str:
    """Get user's avatar URL - custom profile pic if set, otherwise Discord avatar."""
    # Check for custom profile picture first
    custom_pic = await db.get_profile_picture(user_id)
    if custom_pic:
        base_url = config.WEB_PUBLIC_URL.rstrip("/") if config.WEB_PUBLIC_URL else ""
        return f"{base_url}/profile-pic/{user_id}"
    
    # Fall back to Discord avatar
    if bot:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and member.avatar:
                return member.avatar.url
    
    # Default Discord avatar
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


async def profile_pic_handler(request: web.Request) -> web.Response:
    """Serve a user's custom profile picture."""
    user_id_str = request.match_info.get("user_id")
    if not user_id_str:
        return web.Response(text="Missing user_id", status=400)
    
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.Response(text="Invalid user_id", status=400)
    
    filename = await db.get_profile_picture(user_id)
    if not filename:
        # Redirect to Discord default avatar
        default_avatar = (user_id >> 22) % 6
        raise web.HTTPFound(f"https://cdn.discordapp.com/embed/avatars/{default_avatar}.png")
    
    filepath = config.PROFILE_PICS_DIR / filename
    if not filepath.exists():
        # File missing, redirect to Discord default
        default_avatar = (user_id >> 22) % 6
        raise web.HTTPFound(f"https://cdn.discordapp.com/embed/avatars/{default_avatar}.png")
    
    return web.FileResponse(filepath, headers={
        "Cache-Control": "public, max-age=3600"
    })


async def api_profile_pic_upload_handler(request: web.Request) -> web.Response:
    """API endpoint to upload a custom profile picture."""
    import uuid
    from pathlib import Path
    
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    user_id = session["user_id"]
    
    # Check if user is a crew member
    bot = request.app.get("bot")
    if bot:
        is_crew = await _is_crew_member(bot, user_id)
        if not is_crew:
            return web.json_response({"success": False, "error": "Only crew members can upload profile pictures"}, status=403)
    
    # Parse multipart form
    try:
        reader = await request.multipart()
        field = await reader.next()
        
        if not field or field.name != "image":
            return web.json_response({"success": False, "error": "No image field in upload"}, status=400)
        
        # Get content type - try multiple methods
        content_type = getattr(field, 'content_type', None) or field.headers.get("Content-Type", "") or ""
        
        # Also check filename extension as fallback
        filename_ext = ""
        if field.filename:
            filename_ext = field.filename.lower().rsplit(".", 1)[-1] if "." in field.filename else ""
        
        # Validate it's an image
        allowed_exts = {"jpg", "jpeg", "png", "gif", "webp"}
        is_valid_image = content_type.startswith("image/") or filename_ext in allowed_exts
        
        if not is_valid_image:
            return web.json_response({"success": False, "error": "File must be an image"}, status=400)
        
        # Get file extension
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        # Prefer content-type, fall back to filename extension
        if content_type in ext_map:
            ext = ext_map[content_type]
        elif filename_ext in {"jpg", "jpeg"}:
            ext = ".jpg"
        elif filename_ext == "png":
            ext = ".png"
        elif filename_ext == "gif":
            ext = ".gif"
        elif filename_ext == "webp":
            ext = ".webp"
        else:
            ext = ".png"  # Default
        
        # Read file data with size limit
        max_size = 5 * 1024 * 1024  # 5MB
        chunks = []
        total_size = 0
        while True:
            chunk = await field.read_chunk(size=64 * 1024)  # Read 64KB chunks
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > max_size:
                return web.json_response({"success": False, "error": "Image too large (max 5MB)"}, status=400)
            chunks.append(chunk)
        data = b"".join(chunks)
        
        if not data:
            return web.json_response({"success": False, "error": "Empty file"}, status=400)
        
        # Create profile pics directory if needed
        config.PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)
        
        # Delete old profile picture if exists
        old_filename = await db.delete_profile_picture(user_id)
        if old_filename:
            old_path = config.PROFILE_PICS_DIR / old_filename
            if old_path.exists():
                old_path.unlink()
        
        # Save new file
        filename = f"{user_id}_{uuid.uuid4().hex[:8]}{ext}"
        filepath = config.PROFILE_PICS_DIR / filename
        filepath.write_bytes(data)
        
        # Update database
        await db.set_profile_picture(user_id, filename)
        
        base_url = config.WEB_PUBLIC_URL.rstrip("/") if config.WEB_PUBLIC_URL else ""
        return web.json_response({
            "success": True,
            "avatar_url": f"{base_url}/profile-pic/{user_id}"
        })
        
    except Exception as e:
        log.error(f"Profile picture upload error: {e}")
        return web.json_response({"success": False, "error": "Upload failed"}, status=500)


async def api_profile_pic_delete_handler(request: web.Request) -> web.Response:
    """API endpoint to delete a custom profile picture (revert to Discord avatar)."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    user_id = session["user_id"]
    
    # Delete old profile picture
    old_filename = await db.delete_profile_picture(user_id)
    if old_filename:
        old_path = config.PROFILE_PICS_DIR / old_filename
        if old_path.exists():
            old_path.unlink()
    
    return web.json_response({"success": True})


async def api_crew_members_handler(request: web.Request) -> web.Response:
    """API endpoint to get list of crew members (for lead assignment dropdown)."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    # Check if user is a lead
    user_id = session["user_id"]
    is_lead = False
    guild = bot.get_guild(config.GUILD_ID)
    if guild and config.WEB_LEAD_ROLE_ID:
        member = guild.get_member(user_id)
        if member and config.WEB_LEAD_ROLE_ID in [r.id for r in member.roles]:
            is_lead = True
    
    if not is_lead:
        return web.json_response({"success": False, "error": "Only leads can access crew list"}, status=403)
    
    if not guild:
        return web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    # Get all crew role IDs
    crew_role_ids = set()
    if config.CASTER_ROLE_ID:
        crew_role_ids.add(config.CASTER_ROLE_ID)
    if config.CAMOP_ROLE_ID:
        crew_role_ids.add(config.CAMOP_ROLE_ID)
    if config.CASTER_TRAINING_ROLE_ID:
        crew_role_ids.add(config.CASTER_TRAINING_ROLE_ID)
    if config.CAMOP_TRAINING_ROLE_ID:
        crew_role_ids.add(config.CAMOP_TRAINING_ROLE_ID)
    
    if not crew_role_ids:
        return web.json_response({"success": False, "error": "No crew roles configured"}, status=500)
    
    # Fetch members from each crew role to ensure we have all of them
    crew_members = []
    seen_ids = set()
    
    for role_id in crew_role_ids:
        role = guild.get_role(role_id)
        if role:
            for member in role.members:
                if member.bot or member.id in seen_ids:
                    continue
                seen_ids.add(member.id)
                avatar_url = await get_user_avatar_url(bot, member.id)
                crew_members.append({
                    "user_id": str(member.id),  # String to preserve precision in JS
                    "username": member.name,
                    "display_name": member.display_name,
                    "avatar_url": avatar_url
                })
    
    # Sort by display name
    crew_members.sort(key=lambda m: m["display_name"].lower())
    
    return web.json_response({"success": True, "members": crew_members})


def _check_rpc_key(request: web.Request) -> bool:
    """Check if request has valid RPC API key."""
    if not config.RPC_API_KEY:
        return False
    api_key = request.headers.get("X-API-Key") or request.query.get("api_key")
    return api_key == config.RPC_API_KEY


async def api_matches_handler(request: web.Request) -> web.Response:
    """Public API endpoint to get all matches with claim info (no auth required)."""
    bot = request.app.get("bot")
    guild = bot.get_guild(config.GUILD_ID) if bot else None
    
    # Helper to get team members from role
    def get_team_members(team_name: str) -> list:
        if not guild or not team_name:
            return []
        
        team_name_lower = team_name.lower()
        team_role = None
        
        # Find the "Team: X" role
        for role in guild.roles:
            if role.name.lower().startswith("team:"):
                role_team_name = role.name[5:].strip().lower()
                if role_team_name == team_name_lower:
                    team_role = role
                    break
        
        if not team_role:
            return []
        
        members = []
        for member in team_role.members:
            if member.bot:
                continue
            members.append({
                "user_id": str(member.id),
                "username": member.name,
                "display_name": member.display_name,
            })
        
        # Sort by display name
        members.sort(key=lambda m: m["display_name"].lower())
        return members
    
    # Helper to get team logo URL
    async def get_team_logo_url(team_name: str) -> str | None:
        logo = await db.get_team_logo(team_name)
        if logo:
            base_url = config.WEB_PUBLIC_URL.rstrip("/") if config.WEB_PUBLIC_URL else ""
            return f"{base_url}/team-logo/{team_name}"
        return None
    
    # Get all active matches
    matches = await db.get_all_matches_sorted_by_time()
    
    result = []
    for match in matches:
        match_id = match["match_id"]
        claims = await db.get_claims(match_id)
        
        # Build casters list
        casters = []
        for slot in range(1, 3):  # Slots 1 and 2
            claim = next((c for c in claims if c["role"] == "caster" and c["slot"] == slot), None)
            if claim:
                user_id = claim["user_id"]
                user_info = {"user_id": str(user_id), "slot": slot}
                
                # Get user display name and avatar from Discord
                if guild:
                    member = guild.get_member(user_id)
                    if member:
                        user_info["display_name"] = member.display_name
                        user_info["username"] = member.name
                        user_info["avatar_url"] = await get_user_avatar_url(bot, user_id)
                
                casters.append(user_info)
        
        # Build cam op info
        cam_op = None
        cam_claim = next((c for c in claims if c["role"] == "camop" and c["slot"] == 1), None)
        if cam_claim:
            user_id = cam_claim["user_id"]
            cam_op = {"user_id": str(user_id)}
            
            if guild:
                member = guild.get_member(user_id)
                if member:
                    cam_op["display_name"] = member.display_name
                    cam_op["username"] = member.name
                    cam_op["avatar_url"] = await get_user_avatar_url(bot, user_id)
        
        # Build sideline info (if claimed)
        sideline = None
        sideline_claim = next((c for c in claims if c["role"] == "sideline" and c["slot"] == 1), None)
        if sideline_claim:
            user_id = sideline_claim["user_id"]
            sideline = {"user_id": str(user_id)}
            
            if guild:
                member = guild.get_member(user_id)
                if member:
                    sideline["display_name"] = member.display_name
                    sideline["username"] = member.name
                    sideline["avatar_url"] = await get_user_avatar_url(bot, user_id)
        
        # Get team logos
        team_a_logo = await get_team_logo_url(match["team_a"])
        team_b_logo = await get_team_logo_url(match["team_b"])
        
        result.append({
            "id": match.get("simple_id"),
            "match_id": match_id,  # Full match_id for RPC calls
            "team_a": match["team_a"],
            "team_b": match["team_b"],
            "team_a_logo": team_a_logo,
            "team_b_logo": team_b_logo,
            "team_a_roster": get_team_members(match["team_a"]),
            "team_b_roster": get_team_members(match["team_b"]),
            "match_date": match["match_date"],
            "match_time": match["match_time"],
            "match_timestamp": match.get("match_timestamp"),
            "week_number": match.get("week_number"),
            "match_type": match.get("match_type"),
            "stream_channel": match.get("stream_channel"),
            "casters": casters,
            "cam_op": cam_op,
            "sideline": sideline,
        })
    
    return web.json_response({"success": True, "matches": result})


# ============ RPC API (for remote apps with API key) ============

async def rpc_create_channel_handler(request: web.Request) -> web.Response:
    """RPC endpoint to create private match channel (requires API key)."""
    log.info("[RPC] create_channel request received from %s", request.remote)
    if not _check_rpc_key(request):
        log.warning("[RPC] create_channel rejected - invalid API key from %s", request.remote)
        return web.json_response({"success": False, "error": "Invalid or missing API key"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    # Accept simple match ID
    match_id_param = data.get("match_id") or data.get("id")
    if not match_id_param:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    # Look up by simple_id if numeric
    if isinstance(match_id_param, int) or (isinstance(match_id_param, str) and match_id_param.isdigit()):
        match = await db.get_match_by_simple_id(int(match_id_param))
    else:
        match = await db.get_match(match_id_param)
    
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    match_id = match["match_id"]
    
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
        log.info("[RPC] create_channel success - match=%s channel_id=%s", match_id_param, channel.id)
        await _refresh_discord_message(bot, match_id)
        return web.json_response({"success": True, "channel_id": channel.id})
    else:
        log.error("[RPC] create_channel failed - match=%s could not create channel", match_id_param)
        return web.json_response({"success": False, "error": "Failed to create channel"}, status=500)


async def rpc_crew_ready_handler(request: web.Request) -> web.Response:
    """RPC endpoint to send crew ready message (requires API key)."""
    log.info("[RPC] crew_ready request received from %s", request.remote)
    if not _check_rpc_key(request):
        log.warning("[RPC] crew_ready rejected - invalid API key from %s", request.remote)
        return web.json_response({"success": False, "error": "Invalid or missing API key"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    # Accept simple match ID
    match_id_param = data.get("match_id") or data.get("id")
    if not match_id_param:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    # Look up by simple_id if numeric
    if isinstance(match_id_param, int) or (isinstance(match_id_param, str) and match_id_param.isdigit()):
        match = await db.get_match_by_simple_id(int(match_id_param))
    else:
        match = await db.get_match(match_id_param)
    
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    match_id = match["match_id"]
    
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
    log.info("[RPC] crew_ready success - match=%s", match_id_param)
    return web.json_response({"success": True})


async def rpc_go_live_handler(request: web.Request) -> web.Response:
    """RPC endpoint to post live announcement (requires API key)."""
    log.info("[RPC] go_live request received from %s", request.remote)
    if not _check_rpc_key(request):
        log.warning("[RPC] go_live rejected - invalid API key from %s", request.remote)
        return web.json_response({"success": False, "error": "Invalid or missing API key"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    # Accept simple match ID
    match_id_param = data.get("match_id") or data.get("id")
    if not match_id_param:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    # Look up by simple_id if numeric
    if isinstance(match_id_param, int) or (isinstance(match_id_param, str) and match_id_param.isdigit()):
        match = await db.get_match_by_simple_id(int(match_id_param))
    else:
        match = await db.get_match(match_id_param)
    
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    match_id = match["match_id"]
    
    if not match.get("private_channel_id"):
        return web.json_response({"success": False, "error": "Create the channel first"}, status=400)
    
    # Check requirements
    claims = await db.get_claims(match_id)
    casters = [c for c in claims if c["role"] == "caster"]
    camops = [c for c in claims if c["role"] == "camop"]
    if not casters or not camops:
        return web.json_response({"success": False, "error": "Need at least 1 caster and 1 cam op"}, status=400)
    
    # Check stream channel is selected
    if not match.get("stream_channel"):
        return web.json_response({"success": False, "error": "Please select a stream channel first"}, status=400)
    
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
    
    # Use selected stream channel
    stream_channel = match.get("stream_channel")
    channel_label, twitch_url = config.STREAM_CHANNELS[stream_channel]
    announcement = f"# [EchoMasterLeague]({twitch_url}) We are live now casting {teams_text}"
    if live_ping:
        announcement += f"\n{live_ping}"
    
    await live_channel.send(announcement)
    log.info("[RPC] go_live success - match=%s stream_channel=%s", match_id_param, stream_channel)
    return web.json_response({"success": True})


async def rpc_set_stream_channel_handler(request: web.Request) -> web.Response:
    """RPC endpoint to set the stream channel for a match (requires API key)."""
    log.info("[RPC] set_stream_channel request received from %s", request.remote)
    if not _check_rpc_key(request):
        log.warning("[RPC] set_stream_channel rejected - invalid API key from %s", request.remote)
        return web.json_response({"success": False, "error": "Invalid or missing API key"}, status=401)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    # Accept simple match ID
    match_id_param = data.get("match_id") or data.get("id")
    stream_channel = data.get("stream_channel") or data.get("channel")
    
    if not match_id_param:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    if stream_channel is None or stream_channel not in config.STREAM_CHANNELS:
        return web.json_response({"success": False, "error": "Invalid stream channel (use 1 or 2)"}, status=400)
    
    # Look up by simple_id if numeric
    if isinstance(match_id_param, int) or (isinstance(match_id_param, str) and match_id_param.isdigit()):
        match = await db.get_match_by_simple_id(int(match_id_param))
    else:
        match = await db.get_match(match_id_param)
    
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    await db.set_stream_channel(match["match_id"], stream_channel)
    log.info("[RPC] set_stream_channel success - match=%s channel=%s", match_id_param, stream_channel)
    return web.json_response({"success": True})


async def rpc_get_match_handler(request: web.Request) -> web.Response:
    """RPC endpoint to get a single match by ID (requires API key)."""
    log.info("[RPC] get_match request received from %s (id=%s)", request.remote, request.query.get("id") or request.query.get("match_id"))
    if not _check_rpc_key(request):
        log.warning("[RPC] get_match rejected - invalid API key from %s", request.remote)
        return web.json_response({"success": False, "error": "Invalid or missing API key"}, status=401)
    
    bot = request.app.get("bot")
    guild = bot.get_guild(config.GUILD_ID) if bot else None
    
    # Helper to get team members from role
    def get_team_members(team_name: str) -> list:
        if not guild or not team_name:
            return []
        
        team_name_lower = team_name.lower()
        team_role = None
        
        for role in guild.roles:
            if role.name.lower().startswith("team:"):
                role_team_name = role.name[5:].strip().lower()
                if role_team_name == team_name_lower:
                    team_role = role
                    break
        
        if not team_role:
            return []
        
        members = []
        for member in team_role.members:
            if member.bot:
                continue
            members.append({
                "user_id": str(member.id),
                "username": member.name,
                "display_name": member.display_name,
            })
        
        members.sort(key=lambda m: m["display_name"].lower())
        return members
    
    # Accept ID from query param or path
    match_id_param = request.query.get("id") or request.query.get("match_id")
    if not match_id_param:
        return web.json_response({"success": False, "error": "Missing id parameter"}, status=400)
    
    # Look up by simple_id if numeric
    if match_id_param.isdigit():
        match = await db.get_match_by_simple_id(int(match_id_param))
    else:
        match = await db.get_match(match_id_param)
    
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    match_id = match["match_id"]
    claims = await db.get_claims(match_id)
    
    # Build casters list
    casters = []
    for slot in range(1, 3):
        claim = next((c for c in claims if c["role"] == "caster" and c["slot"] == slot), None)
        if claim:
            user_id = claim["user_id"]
            user_info = {"user_id": str(user_id), "slot": slot}
            if guild:
                member = guild.get_member(user_id)
                if member:
                    user_info["display_name"] = member.display_name
                    user_info["avatar_url"] = await get_user_avatar_url(bot, user_id)
            casters.append(user_info)
    
    # Build cam op info
    cam_op = None
    cam_claim = next((c for c in claims if c["role"] == "camop" and c["slot"] == 1), None)
    if cam_claim:
        user_id = cam_claim["user_id"]
        cam_op = {"user_id": str(user_id)}
        if guild:
            member = guild.get_member(user_id)
            if member:
                cam_op["display_name"] = member.display_name
                cam_op["avatar_url"] = await get_user_avatar_url(bot, user_id)
    
    # Get team logos
    async def get_team_logo_url(team_name: str) -> str | None:
        if not team_name:
            return None
        logo = await db.get_team_logo(team_name)
        if logo:
            return f"{config.WEB_PUBLIC_URL}/team-logo/{team_name}"
        return None
    
    team_a_logo = await get_team_logo_url(match["team_a"])
    team_b_logo = await get_team_logo_url(match["team_b"])
    
    result = {
        "id": match.get("simple_id"),
        "match_id": match_id,  # Include full match_id for button matching
        "team_a": match["team_a"],
        "team_b": match["team_b"],
        "team_a_logo": team_a_logo,
        "team_b_logo": team_b_logo,
        "team_a_roster": get_team_members(match["team_a"]),
        "team_b_roster": get_team_members(match["team_b"]),
        "match_date": match["match_date"],
        "match_time": match["match_time"],
        "match_timestamp": match.get("match_timestamp"),
        "stream_channel": match.get("stream_channel"),
        "has_channel": bool(match.get("private_channel_id")),
        "casters": casters,
        "cam_op": cam_op,
    }
    
    return web.json_response({"success": True, "match": result})


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
        await db.set_setting("season", str(season))
        await db.set_setting("week", str(week))
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
    """Admin API: Start a new leaderboard cycle."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    name = data.get("name")
    weeks = data.get("weeks", 5)
    
    if not name:
        return web.json_response({"success": False, "error": "Missing cycle name"}, status=400)
    
    try:
        result = await db.start_cycle(name, weeks)
        msg = f"Started '{name}' ({weeks} weeks, ends {result['end_date']})"
        if result.get("archived_id"):
            msg += f" - Previous cycle archived"
        return web.json_response({"success": True, "message": msg})
    except Exception as e:
        log.error(f"Admin start cycle failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_end_cycle_handler(request: web.Request) -> web.Response:
    """Admin API: End active cycle now."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        cycle_id = await db.end_active_cycle()
        if cycle_id:
            return web.json_response({"success": True, "message": f"Cycle ended and archived (#{cycle_id})"})
        else:
            return web.json_response({"success": False, "error": "No active cycle to end"}, status=400)
    except Exception as e:
        log.error(f"Admin end cycle failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_force_delete_handler(request: web.Request) -> web.Response:
    """Admin API: Force delete a match without counting toward leaderboard."""
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
    
    try:
        deleted_items = []
        
        # Delete private channel if it exists
        if match.get("private_channel_id") and bot:
            try:
                private_channel = bot.get_channel(match["private_channel_id"])
                if private_channel:
                    await private_channel.delete(reason="Force deleted via admin panel")
                    deleted_items.append("private channel")
            except Exception as e:
                log.error(f"Failed to delete private channel: {e}")
        
        # Delete claim message if it exists
        if match.get("message_id") and bot:
            try:
                from . import config
                claim_channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
                if claim_channel:
                    msg = await claim_channel.fetch_message(match["message_id"])
                    await msg.delete()
                    deleted_items.append("claim message")
            except Exception as e:
                log.error(f"Failed to delete claim message: {e}")
        
        # Delete match from DB (does NOT increment leaderboard)
        await db.delete_match(match_id)
        deleted_items.append("match data")
        
        return web.json_response({
            "success": True, 
            "message": f"Force deleted {match['team_a']} vs {match['team_b']}. Deleted: {', '.join(deleted_items)}"
        })
    except Exception as e:
        log.error(f"Admin force delete failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_admin_force_create_match_handler(request: web.Request) -> web.Response:
    """Admin API: Force create a match not from the Google Sheet."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    team_a = data.get("team_a", "").strip()
    team_b = data.get("team_b", "").strip()
    datetime_str = data.get("datetime")
    match_type = data.get("match_type")
    
    if not team_a or not team_b:
        return web.json_response({"success": False, "error": "Missing team names"}, status=400)
    if not datetime_str:
        return web.json_response({"success": False, "error": "Missing datetime"}, status=400)
    
    try:
        # Parse datetime (format: YYYY-MM-DDTHH:MM from datetime-local input)
        from datetime import datetime as dt
        match_dt = dt.fromisoformat(datetime_str)
        
        # Generate a unique match_id (manual matches use a special prefix)
        import hashlib
        import time
        hash_input = f"manual:{team_a}:{team_b}:{int(time.time())}"
        match_id = f"manual_{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"
        
        # Format date/time strings
        match_date = match_dt.strftime("%Y-%m-%d")
        match_time = match_dt.strftime("%H:%M")
        match_timestamp = int(match_dt.timestamp())
        
        # Insert into database
        await db.upsert_match(
            match_id=match_id,
            team_a=team_a,
            team_b=team_b,
            match_date=match_date,
            match_time=match_time,
            match_timestamp=match_timestamp,
            match_type=match_type,
        )
        
        # Post claim message if bot is available
        if bot:
            from .views import ClaimView
            claim_channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
            if claim_channel:
                match = await db.get_match(match_id)
                claims = await db.get_claims(match_id)
                view = ClaimView(match_id, match, claims)
                bot.add_view(view)
                try:
                    msg = await claim_channel.send(view=view)
                    await db.set_message_id(match_id, msg.id, claim_channel.id)
                    log.info(f"Posted claim message for manual match: {team_a} vs {team_b}")
                except Exception as e:
                    log.error(f"Failed to post claim message: {e}")
        
        match = await db.get_match(match_id)
        simple_id = match.get("simple_id", "?") if match else "?"
        
        return web.json_response({
            "success": True,
            "message": f"Created match #{simple_id}: {team_a} vs {team_b}"
        })
    except Exception as e:
        log.error(f"Admin force create match failed: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


# ============ Team Logo Review ============

async def api_logo_pending_handler(request: web.Request) -> web.Response:
    """Get pending logo submissions from the logo submission channel."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    if not config.TEAM_LOGO_CHANNEL_ID:
        return web.json_response({"success": False, "error": "TEAM_LOGO_CHANNEL_ID not configured"}, status=500)
    
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    channel = bot.get_channel(config.TEAM_LOGO_CHANNEL_ID)
    if not channel:
        return web.json_response({"success": False, "error": "Logo channel not found"}, status=500)
    
    # Get all approved message IDs so we can filter them out
    approved_logos = await db.get_all_team_logos()
    approved_msg_ids = {logo["discord_message_id"] for logo in approved_logos if logo.get("discord_message_id")}
    
    pending = []
    try:
        # Read recent messages with images
        async for msg in channel.history(limit=100):
            # Skip if already approved
            if msg.id in approved_msg_ids:
                continue
            
            # Skip if no attachments with images
            image_attachments = [a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")]
            if not image_attachments:
                continue
            
            # Find the user's team role
            team_name = None
            member = guild.get_member(msg.author.id)
            if member:
                for role in member.roles:
                    if role.name.lower().startswith("team:"):
                        team_name = role.name[5:].strip()
                        break
            
            if not team_name:
                continue  # Skip if user doesn't have a team role
            
            # Check if this team already has an approved logo
            existing_logo = await db.get_team_logo(team_name)
            
            pending.append({
                "message_id": str(msg.id),
                "user_id": str(msg.author.id),
                "username": msg.author.name,
                "display_name": msg.author.display_name,
                "team_name": team_name,
                "image_url": image_attachments[0].url,
                "image_filename": image_attachments[0].filename,
                "posted_at": msg.created_at.isoformat(),
                "has_existing_logo": existing_logo is not None,
            })
    except Exception as e:
        log.error(f"Failed to read logo channel: {e}")
        return web.json_response({"success": False, "error": "Failed to read logo channel"}, status=500)
    
    return web.json_response({"success": True, "pending": pending})


async def api_logo_approve_handler(request: web.Request) -> web.Response:
    """Approve a team logo submission."""
    import aiohttp as aiohttp_client
    import uuid
    
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    message_id = data.get("message_id")
    team_name = data.get("team_name")
    image_url = data.get("image_url")
    
    if not message_id or not team_name or not image_url:
        return web.json_response({"success": False, "error": "Missing required fields"}, status=400)
    
    try:
        message_id_int = int(message_id)
    except ValueError:
        return web.json_response({"success": False, "error": "Invalid message_id"}, status=400)
    
    # Download the image
    try:
        async with aiohttp_client.ClientSession() as client_session:
            async with client_session.get(image_url) as resp:
                if resp.status != 200:
                    return web.json_response({"success": False, "error": "Failed to download image"}, status=500)
                
                content_type = resp.headers.get("Content-Type", "")
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                }
                ext = ext_map.get(content_type, ".png")
                
                image_data = await resp.read()
    except Exception as e:
        log.error(f"Failed to download logo image: {e}")
        return web.json_response({"success": False, "error": "Failed to download image"}, status=500)
    
    # Create logos directory if needed
    config.TEAM_LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Delete old logo if exists
    old_filename = await db.delete_team_logo(team_name)
    if old_filename:
        old_path = config.TEAM_LOGOS_DIR / old_filename
        if old_path.exists():
            old_path.unlink()
    
    # Save new logo
    safe_team_name = "".join(c if c.isalnum() else "_" for c in team_name)
    filename = f"{safe_team_name}_{uuid.uuid4().hex[:8]}{ext}"
    filepath = config.TEAM_LOGOS_DIR / filename
    filepath.write_bytes(image_data)
    
    # Save to database
    await db.set_team_logo(team_name, filename, message_id_int, session["user_id"])
    
    # Add checkmark reaction to the message
    if config.TEAM_LOGO_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.TEAM_LOGO_CHANNEL_ID)
            if channel:
                msg = await channel.fetch_message(message_id_int)
                await msg.add_reaction("\u2705")  # ✅
        except Exception as e:
            log.warning(f"Failed to add reaction to logo message: {e}")
    
    return web.json_response({"success": True, "message": f"Approved logo for {team_name}"})


async def api_logo_reject_handler(request: web.Request) -> web.Response:
    """Reject a logo submission (optionally delete the Discord message)."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    message_id = data.get("message_id")
    delete_message = data.get("delete_message", False)
    
    if not message_id:
        return web.json_response({"success": False, "error": "Missing message_id"}, status=400)
    
    # Add X reaction or delete the Discord message
    if config.TEAM_LOGO_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.TEAM_LOGO_CHANNEL_ID)
            if channel:
                msg = await channel.fetch_message(int(message_id))
                if delete_message:
                    await msg.delete()
                else:
                    await msg.add_reaction("\u274C")  # ❌
        except Exception as e:
            log.warning(f"Failed to react/delete logo message: {e}")
    
    return web.json_response({"success": True, "message": "Logo rejected"})


async def api_logo_list_handler(request: web.Request) -> web.Response:
    """Get all approved team logos."""
    logos = await db.get_all_team_logos()
    
    base_url = config.WEB_PUBLIC_URL.rstrip("/") if config.WEB_PUBLIC_URL else ""
    
    result = []
    for logo in logos:
        result.append({
            "team_name": logo["team_name"],
            "logo_url": f"{base_url}/team-logo/{logo['team_name']}",
            "approved_at": logo["approved_at"],
        })
    
    return web.json_response({"success": True, "logos": result})


async def api_logo_delete_handler(request: web.Request) -> web.Response:
    """Delete an approved team logo."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    team_name = data.get("team_name")
    if not team_name:
        return web.json_response({"success": False, "error": "Missing team_name"}, status=400)
    
    old_filename = await db.delete_team_logo(team_name)
    if old_filename:
        old_path = config.TEAM_LOGOS_DIR / old_filename
        if old_path.exists():
            old_path.unlink()
        return web.json_response({"success": True, "message": f"Deleted logo for {team_name}"})
    else:
        return web.json_response({"success": False, "error": "No logo found for this team"}, status=404)


async def api_logo_rename_handler(request: web.Request) -> web.Response:
    """Rename a team logo to a different team."""
    session, error = await _check_admin(request)
    if error:
        return error
    
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    
    old_team_name = data.get("old_team_name")
    new_team_name = data.get("new_team_name")
    
    if not old_team_name or not new_team_name:
        return web.json_response({"success": False, "error": "Missing team names"}, status=400)
    
    if old_team_name.strip() == new_team_name.strip():
        return web.json_response({"success": False, "error": "Team names are the same"}, status=400)
    
    success = await db.rename_team_logo(old_team_name, new_team_name.strip())
    if success:
        return web.json_response({"success": True, "message": f"Renamed logo from {old_team_name} to {new_team_name}"})
    else:
        return web.json_response({"success": False, "error": "No logo found for this team"}, status=404)


async def team_logo_handler(request: web.Request) -> web.Response:
    """Serve a team's logo image."""
    from urllib.parse import unquote
    
    team_name = unquote(request.match_info.get("team_name", ""))
    if not team_name:
        return web.Response(text="Missing team_name", status=400)
    
    logo = await db.get_team_logo(team_name)
    if not logo:
        return web.Response(text="Logo not found", status=404)
    
    filepath = config.TEAM_LOGOS_DIR / logo["filename"]
    if not filepath.exists():
        return web.Response(text="Logo file not found", status=404)
    
    return web.FileResponse(filepath, headers={
        "Cache-Control": "public, max-age=3600"
    })


async def api_active_cycle_handler(request: web.Request) -> web.Response:
    """API endpoint to get active cycle info."""
    active = await db.get_active_cycle()
    if active:
        return web.json_response({"active": True, **active})
    else:
        return web.json_response({"active": False})


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.Response(text="OK")


async def manifest_handler(request: web.Request) -> web.Response:
    """Serve PWA manifest."""
    manifest = {
        "name": "EML Broadcast Hub",
        "short_name": "EML Caster",
        "description": f"{config.LEAGUE_NAME} Broadcast Hub - Claim matches and manage casts",
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


async def api_chat_messages_handler(request: web.Request) -> web.Response:
    """API endpoint to get recent messages from a private match channel."""
    session = _get_session(request)
    if not session:
        return web.json_response({"success": False, "error": "Not logged in"}, status=401)
    
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"success": False, "error": "Bot not available"}, status=500)
    
    match_id = request.query.get("match_id")
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    # Check if user has access to this match (has a claim or is lead)
    user_id = session["user_id"]
    claims = await db.get_claims(match_id)
    has_claim = any(c["user_id"] == user_id for c in claims)
    
    is_lead = False
    if config.WEB_LEAD_ROLE_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and config.WEB_LEAD_ROLE_ID in [r.id for r in member.roles]:
                is_lead = True
    
    if not is_lead and not has_claim:
        return web.json_response({"success": False, "error": "No access to this match chat"}, status=403)
    
    # Check if private channel exists
    if not match.get("private_channel_id"):
        return web.json_response({"success": True, "messages": [], "channel_exists": False})
    
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    channel = guild.get_channel(match["private_channel_id"])
    if not channel:
        return web.json_response({"success": True, "messages": [], "channel_exists": False, "mentionable_users": [], "mentionable_roles": []})
    
    # Get mentionable users (members with access to the channel)
    mentionable_users = []
    try:
        for member in channel.members:
            if member.bot:
                continue
            avatar_url = await get_user_avatar_url(bot, member.id)
            mentionable_users.append({
                "id": str(member.id),
                "name": member.display_name,
                "avatar": avatar_url,
            })
        mentionable_users.sort(key=lambda m: m["name"].lower())
    except Exception as e:
        log.error(f"Failed to get mentionable users: {e}")
    
    # Get mentionable roles (only team roles for teams in this match)
    mentionable_roles = []
    try:
        team_a_lower = match.get("team_a", "").lower()
        team_b_lower = match.get("team_b", "").lower()
        for role in guild.roles:
            # Only include "Team:" roles that match this match's teams
            if role.name.lower().startswith("team:"):
                team_name = role.name[5:].strip()
                team_name_lower = team_name.lower()
                if team_name_lower == team_a_lower or team_name_lower == team_b_lower:
                    # Use role color or default gray
                    color = f"#{role.color.value:06x}" if role.color.value else "#99aab5"
                    mentionable_roles.append({
                        "id": str(role.id),
                        "name": role.name,
                        "displayName": team_name,  # Team name without "Team:" prefix
                        "color": color,
                    })
        mentionable_roles.sort(key=lambda r: r["name"].lower())
    except Exception as e:
        log.error(f"Failed to get mentionable roles: {e}")
    
    # Get recent messages (last 50)
    messages = []
    try:
        async for msg in channel.history(limit=50, oldest_first=False):
            msg_id = str(msg.id)
            
            # Convert Discord mentions to readable names
            content = _convert_mentions_to_names(msg.content, guild)
            
            # Get reply_to_id if this message is a reply
            reply_to_id = None
            if msg.reference and msg.reference.message_id:
                reply_to_id = str(msg.reference.message_id)
            
            # Check if this is a bot message sent via web UI
            web_sender = _web_sent_messages.get(msg_id)
            if msg.author.bot and web_sender:
                # Use the web sender's info for display
                # Web-sent messages may also store reply info
                if not reply_to_id and web_sender.get("reply_to_id"):
                    reply_to_id = web_sender["reply_to_id"]
                messages.append({
                    "id": msg_id,
                    "author_id": web_sender["sender_id"],
                    "author_name": web_sender["sender_name"],
                    "author_avatar": web_sender["sender_avatar"],
                    "is_bot": False,  # Show as user message in UI
                    "is_web_sent": True,  # Flag to indicate it was sent via web
                    "content": content,
                    "timestamp": msg.created_at.isoformat(),
                    "reply_to_id": reply_to_id,
                })
            else:
                avatar_url = await get_user_avatar_url(bot, msg.author.id)
                messages.append({
                    "id": msg_id,
                    "author_id": str(msg.author.id),
                    "author_name": msg.author.display_name,
                    "author_avatar": avatar_url,
                    "is_bot": msg.author.bot,
                    "content": content,
                    "timestamp": msg.created_at.isoformat(),
                    "reply_to_id": reply_to_id,
                })
        messages.reverse()  # Show oldest first
    except Exception as e:
        log.error(f"Failed to fetch chat messages: {e}")
        return web.json_response({"success": False, "error": "Failed to fetch messages"}, status=500)
    
    return web.json_response({
        "success": True, 
        "messages": messages, 
        "channel_exists": True,
        "mentionable_users": mentionable_users,
        "mentionable_roles": mentionable_roles,
    })


async def api_chat_send_handler(request: web.Request) -> web.Response:
    """API endpoint to send a message to a private match channel as the bot."""
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
    message = data.get("message", "").strip()
    reply_to_id = data.get("reply_to_id")  # Optional: message ID to reply to
    
    if not match_id:
        return web.json_response({"success": False, "error": "Missing match_id"}, status=400)
    if not message:
        return web.json_response({"success": False, "error": "Message cannot be empty"}, status=400)
    if len(message) > 2000:
        return web.json_response({"success": False, "error": "Message too long (max 2000 characters)"}, status=400)
    
    match = await db.get_match(match_id)
    if not match:
        return web.json_response({"success": False, "error": "Match not found"}, status=404)
    
    # Check if user has access to this match (has a claim or is lead)
    user_id = session["user_id"]
    claims = await db.get_claims(match_id)
    has_claim = any(c["user_id"] == user_id for c in claims)
    
    is_lead = False
    guild = bot.get_guild(config.GUILD_ID)
    if config.WEB_LEAD_ROLE_ID and guild:
        member = guild.get_member(user_id)
        if member and config.WEB_LEAD_ROLE_ID in [r.id for r in member.roles]:
            is_lead = True
    
    if not is_lead and not has_claim:
        return web.json_response({"success": False, "error": "No access to this match chat"}, status=403)
    
    # Check if private channel exists
    if not match.get("private_channel_id"):
        return web.json_response({"success": False, "error": "Private channel has not been created yet"}, status=400)
    
    if not guild:
        return web.json_response({"success": False, "error": "Guild not found"}, status=500)
    
    channel = guild.get_channel(match["private_channel_id"])
    if not channel:
        return web.json_response({"success": False, "error": "Channel not found"}, status=404)
    
    # Get sender info for web UI display
    sender_name = session.get("global_name") or session["username"]
    sender_id = session["user_id"]
    sender_avatar = await get_user_avatar_url(bot, sender_id)
    
    # Build message reference if this is a reply
    reference = None
    if reply_to_id:
        try:
            reference = discord.MessageReference(message_id=int(reply_to_id), channel_id=channel.id)
        except (ValueError, TypeError):
            pass  # Invalid message ID, send without reference
    
    # Send message as the bot (no sender attribution in Discord)
    try:
        sent_msg = await channel.send(message, reference=reference, mention_author=False)
        
        # Store sender info for web UI display (including reply info)
        _web_sent_messages[str(sent_msg.id)] = {
            "sender_id": str(sender_id),
            "sender_name": sender_name,
            "sender_avatar": sender_avatar,
            "reply_to_id": reply_to_id,
        }
        
        return web.json_response({
            "success": True, 
            "message_id": str(sent_msg.id),
        })
    except Exception as e:
        log.error(f"Failed to send chat message: {e}")
        return web.json_response({"success": False, "error": "Failed to send message"}, status=500)


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
    app.router.add_post("/api/set_stream_channel", api_set_stream_channel_handler)
    app.router.add_get("/api/user/avatar", api_user_avatar_handler)
    app.router.add_get("/api/proxy-avatar", api_proxy_avatar_handler)
    app.router.add_get("/api/crew-members", api_crew_members_handler)
    app.router.add_get("/api/matches", api_matches_handler)
    # Profile picture routes
    app.router.add_get("/profile-pic/{user_id}", profile_pic_handler)
    app.router.add_post("/api/profile-pic/upload", api_profile_pic_upload_handler)
    app.router.add_post("/api/profile-pic/delete", api_profile_pic_delete_handler)
    # Chat API routes
    app.router.add_get("/api/chat/messages", api_chat_messages_handler)
    app.router.add_post("/api/chat/send", api_chat_send_handler)
    # RPC API routes (for remote apps with API key)
    app.router.add_post("/rpc/create_channel", rpc_create_channel_handler)
    app.router.add_post("/rpc/crew_ready", rpc_crew_ready_handler)
    app.router.add_post("/rpc/go_live", rpc_go_live_handler)
    app.router.add_post("/rpc/set_stream_channel", rpc_set_stream_channel_handler)
    app.router.add_get("/rpc/match", rpc_get_match_handler)
    # Admin API routes
    app.router.add_post("/api/admin/sync", api_admin_sync_handler)
    app.router.add_post("/api/admin/refresh", api_admin_refresh_handler)
    app.router.add_post("/api/admin/force-channel", api_admin_force_channel_handler)
    app.router.add_post("/api/admin/set-week", api_admin_set_week_handler)
    app.router.add_post("/api/admin/edit-leaderboard", api_admin_edit_leaderboard_handler)
    app.router.add_post("/api/admin/reset-leaderboard", api_admin_reset_leaderboard_handler)
    app.router.add_post("/api/admin/start-cycle", api_admin_start_cycle_handler)
    app.router.add_post("/api/admin/end-cycle", api_admin_end_cycle_handler)
    app.router.add_post("/api/admin/force-delete", api_admin_force_delete_handler)
    app.router.add_post("/api/admin/force-create-match", api_admin_force_create_match_handler)
    app.router.add_get("/api/active-cycle", api_active_cycle_handler)
    # Team logo routes
    app.router.add_get("/api/logos/pending", api_logo_pending_handler)
    app.router.add_post("/api/logos/approve", api_logo_approve_handler)
    app.router.add_post("/api/logos/reject", api_logo_reject_handler)
    app.router.add_get("/api/logos", api_logo_list_handler)
    app.router.add_post("/api/logos/delete", api_logo_delete_handler)
    app.router.add_post("/api/logos/rename", api_logo_rename_handler)
    app.router.add_get("/team-logo/{team_name}", team_logo_handler)
    app.router.add_get("/health", health_handler)
    # PWA routes
    app.router.add_get("/manifest.json", manifest_handler)
    app.router.add_get("/sw.js", service_worker_handler)
    app.router.add_get("/icon-192.png", icon_handler)
    app.router.add_get("/icon-512.png", icon_handler)
    
    return app


async def start_web_server(bot=None, host: str = "0.0.0.0", port: int = 8080, ssl_cert: str = "", ssl_key: str = "") -> web.AppRunner:
    """Start the web server. Returns the runner for cleanup."""
    import ssl as ssl_module
    
    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Configure SSL if cert and key provided
    ssl_context = None
    if ssl_cert and ssl_key:
        try:
            ssl_context = ssl_module.create_default_context(ssl_module.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(ssl_cert, ssl_key)
            log.info("SSL enabled for web server")
        except Exception as e:
            log.error(f"Failed to load SSL certificates: {e}")
            log.warning("Falling back to HTTP")
            ssl_context = None
    
    site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    await site.start()
    
    protocol = "https" if ssl_context else "http"
    log.info(f"Web server started at {protocol}://{host}:{port}")
    return runner


async def stop_web_server(runner: web.AppRunner) -> None:
    """Stop the web server."""
    await runner.cleanup()
    log.info("Web server stopped")
