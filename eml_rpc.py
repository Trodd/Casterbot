#!/usr/bin/env python3
"""
EML Caster RPC Client - CLI helper for Mel production tool integration.

Usage:
    python eml_rpc.py <action> <match_id> [options]

Actions:
    create_channel  - Create the private match channel
    crew_ready      - Send "crew ready" message to teams
    go_live         - Post live announcement
    set_channel     - Set stream channel (1 or 2)
    get_match       - Get match details

Examples:
    python eml_rpc.py create_channel 42
    python eml_rpc.py create_channel nocturne_Valiants_03272026_1030_PM
    python eml_rpc.py set_channel 42 --channel 1
    python eml_rpc.py crew_ready 42
    python eml_rpc.py go_live 42
    python eml_rpc.py get_match 42

Environment:
    EML_RPC_URL      - Base URL (default: http://localhost:8080)
    EML_RPC_API_KEY  - API key for authentication (required)
"""

import argparse
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def get_config():
    """Get configuration from environment."""
    url = os.environ.get("EML_RPC_URL", "http://localhost:8080")
    api_key = os.environ.get("EML_RPC_API_KEY", "")
    return url.rstrip("/"), api_key


def rpc_call(endpoint: str, method: str = "POST", data: dict = None, api_key: str = ""):
    """Make an RPC call to the EML Caster bot."""
    url, key = get_config()
    if api_key:
        key = api_key
    
    if not key:
        print("Error: EML_RPC_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)
    
    full_url = f"{url}{endpoint}"
    
    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
    }
    
    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(full_url, data=body, headers=headers, method=method)
    
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result
    except HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            error_json = json.loads(error_body)
            print(f"Error: {error_json.get('error', error_body)}", file=sys.stderr)
        except json.JSONDecodeError:
            print(f"HTTP Error {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def cmd_create_channel(args):
    """Create private match channel."""
    result = rpc_call("/rpc/create_channel", data={"id": args.match_id})
    if result.get("success"):
        print(f"Channel created (ID: {result.get('channel_id')})")
    else:
        print(f"Failed: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def cmd_crew_ready(args):
    """Send crew ready message."""
    result = rpc_call("/rpc/crew_ready", data={"id": args.match_id})
    if result.get("success"):
        print("Crew ready message sent")
    else:
        print(f"Failed: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def cmd_go_live(args):
    """Post live announcement."""
    result = rpc_call("/rpc/go_live", data={"id": args.match_id})
    if result.get("success"):
        print("Live announcement posted")
    else:
        print(f"Failed: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def cmd_set_channel(args):
    """Set stream channel."""
    result = rpc_call("/rpc/set_stream_channel", data={
        "id": args.match_id,
        "channel": args.channel,
    })
    if result.get("success"):
        print(f"Stream channel set to {args.channel}")
    else:
        print(f"Failed: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def cmd_get_match(args):
    """Get match details."""
    result = rpc_call(f"/rpc/match?id={args.match_id}", method="GET")
    if result.get("success"):
        match = result.get("match", {})
        print(json.dumps(match, indent=2))
    else:
        print(f"Failed: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="EML Caster RPC Client",
        epilog="Set EML_RPC_URL and EML_RPC_API_KEY environment variables.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    
    # create_channel
    p = subparsers.add_parser("create_channel", help="Create private match channel")
    p.add_argument("match_id", help="Match ID (numeric or full key)")
    p.set_defaults(func=cmd_create_channel)
    
    # crew_ready
    p = subparsers.add_parser("crew_ready", help="Send crew ready message")
    p.add_argument("match_id", help="Match ID (numeric or full key)")
    p.set_defaults(func=cmd_crew_ready)
    
    # go_live
    p = subparsers.add_parser("go_live", help="Post live announcement")
    p.add_argument("match_id", help="Match ID (numeric or full key)")
    p.set_defaults(func=cmd_go_live)
    
    # set_channel
    p = subparsers.add_parser("set_channel", help="Set stream channel")
    p.add_argument("match_id", help="Match ID (numeric or full key)")
    p.add_argument("--channel", "-c", type=int, choices=[1, 2], required=True, help="Stream channel (1 or 2)")
    p.set_defaults(func=cmd_set_channel)
    
    # get_match
    p = subparsers.add_parser("get_match", help="Get match details")
    p.add_argument("match_id", help="Match ID (numeric or full key)")
    p.set_defaults(func=cmd_get_match)
    
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
