#!/usr/bin/env python3
"""Inspect Claude subscription accounts — read-only CLI tool.

Reads var/data/claude_accounts.json and displays account state, token expiry,
health, and a summary. No server interaction, no imports from serving code.

Usage:
    python scripts/inspect_claude_accounts.py [--file PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

_DEFAULT_PATH = "var/data/claude_accounts.json"


def _format_expiry(expires_at_ms: int) -> tuple[str, int]:
    """Return (human-readable expiry, seconds remaining)."""
    now_ms = int(time.time() * 1000)
    remaining_s = (expires_at_ms - now_ms) // 1000

    dt = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)
    human = dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    return human, remaining_s


def _format_remaining(seconds: int) -> str:
    """Format seconds remaining as a human-readable string."""
    if seconds <= 0:
        return "EXPIRED"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _state_indicator(state: str) -> str:
    """Return a visual indicator for account state."""
    indicators = {
        "active": "[OK]",
        "cooldown": "[CD]",
        "revoked": "[XX]",
        "disabled": "[--]",
    }
    return indicators.get(state, "[??]")


def main():
    """Read and display Claude subscription account status from the JSON file."""
    parser = argparse.ArgumentParser(description="Inspect Claude subscription accounts")
    parser.add_argument(
        "--file",
        default=_DEFAULT_PATH,
        help=f"Path to accounts file (default: {_DEFAULT_PATH})",
    )
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: {args.file} not found.", file=sys.stderr)
        sys.exit(1)

    with open(args.file) as f:
        data = json.load(f)

    accounts = data.get("accounts", [])
    version = data.get("version", 1)

    print(f"File: {args.file}")
    print(f"Format version: {version}")
    print(f"Total accounts: {len(accounts)}")
    print()

    # State counters
    state_counts: dict[str, int] = {}

    for acct in accounts:
        acct_id = acct.get("id", "???")
        label = acct.get("label", "")
        state = acct.get("state", "active" if acct.get("enabled", True) else "disabled")
        plan = acct.get("plan", "?")
        email = acct.get("email", "")
        org_id = acct.get("organization_id", "")
        expires_at = int(acct.get("expires_at", 0))
        consecutive_failures = int(acct.get("consecutive_failures", 0))
        revoke_reason = acct.get("revoke_reason", "")

        state_counts[state] = state_counts.get(state, 0) + 1

        expiry_str, remaining_s = _format_expiry(expires_at)
        remaining_str = _format_remaining(remaining_s)

        indicator = _state_indicator(state)
        print(f"{indicator} {acct_id} ({label})")
        print(f"    state: {state}  |  plan: {plan}")
        if email:
            print(f"    email: {email}")
        if org_id:
            print(f"    org_id: {org_id}")
        print(f"    token expires: {expiry_str} ({remaining_str})")
        if consecutive_failures > 0:
            print(f"    consecutive_failures: {consecutive_failures}")
        if revoke_reason:
            print(f"    revoke_reason: {revoke_reason}")

        state_changed_at = int(acct.get("state_changed_at", 0))
        if state_changed_at:
            changed_dt = datetime.fromtimestamp(state_changed_at / 1000, tz=timezone.utc)
            print(f"    state_changed_at: {changed_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print()

    # Summary
    print("--- Summary ---")
    for s in ("active", "cooldown", "revoked", "disabled"):
        count = state_counts.get(s, 0)
        if count > 0:
            print(f"  {s}: {count}")

    total_active = state_counts.get("active", 0)
    if total_active == 0:
        print("\n  WARNING: No active accounts!")


if __name__ == "__main__":
    main()
