"""
EML Caster RPC Client Library

Direct callable functions for Mel production tool integration.
Import this module and call functions directly.

Setup:
    import eml_client
    eml_client.configure("http://localhost:8080", "your-api-key")

Usage:
    # Using simple numeric ID
    eml_client.create_channel(42)
    eml_client.set_stream_channel(42, channel=1)
    eml_client.crew_ready(42)
    eml_client.go_live(42)
    
    # Using full match key from button ID
    eml_client.create_channel("nocturne_Valiants_03272026_1030_PM")
    
    # Get match info
    match = eml_client.get_match(42)
    print(match["team_a"], "vs", match["team_b"])
"""

import json
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from typing import Any


# Module-level configuration
_config = {
    "url": os.environ.get("EML_RPC_URL", "http://localhost:8080"),
    "api_key": os.environ.get("EML_RPC_API_KEY", ""),
}


class EMLError(Exception):
    """Error from EML Caster API."""
    pass


def configure(url: str = None, api_key: str = None):
    """
    Configure the client.
    
    Args:
        url: Base URL of EML Caster web server (e.g., "http://localhost:8080")
        api_key: RPC API key from .env
    """
    if url:
        _config["url"] = url.rstrip("/")
    if api_key:
        _config["api_key"] = api_key


def _call(endpoint: str, method: str = "POST", data: dict = None) -> dict:
    """Internal: Make an RPC call."""
    if not _config["api_key"]:
        raise EMLError("API key not configured. Call configure() or set EML_RPC_API_KEY env var.")
    
    full_url = f"{_config['url']}{endpoint}"
    headers = {
        "X-API-Key": _config["api_key"],
        "Content-Type": "application/json",
    }
    
    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(full_url, data=body, headers=headers, method=method)
    
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            error_json = json.loads(error_body)
            raise EMLError(error_json.get("error", error_body))
        except json.JSONDecodeError:
            raise EMLError(f"HTTP {e.code}: {error_body}")
    except URLError as e:
        raise EMLError(f"Connection error: {e.reason}")


def get_match(match_id: int | str) -> dict:
    """
    Get match details.
    
    Args:
        match_id: Simple numeric ID (42) or full match key (nocturne_Valiants_03272026_1030_PM)
    
    Returns:
        dict with match info:
            - id: Simple numeric ID
            - match_id: Full match key
            - team_a, team_b: Team names
            - match_date, match_time, match_timestamp
            - stream_channel: Currently selected channel (1 or 2)
            - has_channel: Whether private channel exists
            - casters: List of caster info
            - cam_op: Cam op info or None
    """
    result = _call(f"/rpc/match?id={match_id}", method="GET")
    if not result.get("success"):
        raise EMLError(result.get("error", "Unknown error"))
    return result["match"]


def get_matches() -> list[dict]:
    """
    Get all active matches.
    
    Returns:
        List of match dicts (same format as get_match)
    """
    result = _call("/api/matches", method="GET")
    if not result.get("success"):
        raise EMLError(result.get("error", "Unknown error"))
    return result["matches"]


def set_stream_channel(match_id: int | str, channel: int) -> bool:
    """
    Set the stream channel for a match.
    
    Args:
        match_id: Simple numeric ID or full match key
        channel: Stream channel (1 or 2)
    
    Returns:
        True on success
    """
    if channel not in (1, 2):
        raise EMLError("Channel must be 1 or 2")
    
    result = _call("/rpc/set_stream_channel", data={"id": match_id, "channel": channel})
    if not result.get("success"):
        raise EMLError(result.get("error", "Unknown error"))
    return True


def create_channel(match_id: int | str) -> int:
    """
    Create the private match channel.
    
    Args:
        match_id: Simple numeric ID or full match key
    
    Returns:
        Discord channel ID
    
    Raises:
        EMLError: If channel already exists or requirements not met
    """
    result = _call("/rpc/create_channel", data={"id": match_id})
    if not result.get("success"):
        raise EMLError(result.get("error", "Unknown error"))
    return result.get("channel_id")


def crew_ready(match_id: int | str) -> bool:
    """
    Send "crew ready" message to teams.
    
    Args:
        match_id: Simple numeric ID or full match key
    
    Returns:
        True on success
    
    Raises:
        EMLError: If channel doesn't exist
    """
    result = _call("/rpc/crew_ready", data={"id": match_id})
    if not result.get("success"):
        raise EMLError(result.get("error", "Unknown error"))
    return True


def go_live(match_id: int | str) -> bool:
    """
    Post live announcement.
    
    Args:
        match_id: Simple numeric ID or full match key
    
    Returns:
        True on success
    
    Raises:
        EMLError: If stream channel not selected or requirements not met
    """
    result = _call("/rpc/go_live", data={"id": match_id})
    if not result.get("success"):
        raise EMLError(result.get("error", "Unknown error"))
    return True


# Convenience: Full workflow
def broadcast_workflow(match_id: int | str, stream_channel: int = 1) -> dict:
    """
    Run the full broadcast workflow: set channel -> create channel -> crew ready.
    
    Args:
        match_id: Simple numeric ID or full match key
        stream_channel: Stream channel to use (1 or 2)
    
    Returns:
        dict with status of each step
    """
    results = {"match_id": match_id}
    
    try:
        set_stream_channel(match_id, stream_channel)
        results["set_channel"] = "success"
    except EMLError as e:
        results["set_channel"] = str(e)
    
    try:
        channel_id = create_channel(match_id)
        results["create_channel"] = channel_id
    except EMLError as e:
        results["create_channel"] = str(e)
    
    try:
        crew_ready(match_id)
        results["crew_ready"] = "success"
    except EMLError as e:
        results["crew_ready"] = str(e)
    
    return results
