#!/usr/bin/env python3
"""Import Claude Code CLI OAuth credentials into hybridInference accounts file.

Reads credentials stored by the official Claude Code CLI and writes/merges
into var/data/claude_accounts.json for use by the claude_sub adapter.

Claude Code stores credentials in ~/.claude/ — the exact file format
depends on the CLI version. This script tries known locations:
  1. ~/.claude/.credentials.json
  2. ~/.claude/credentials.json
  3. ~/.claude/auth.json

Usage:
    python scripts/import_claude_auth.py [--claude-auth PATH] [--output PATH] [--label LABEL]

Examples:
    # Default: auto-detect credentials, write to var/data/claude_accounts.json
    python scripts/import_claude_auth.py

    # Custom paths
    python scripts/import_claude_auth.py --claude-auth /path/to/credentials.json

    # Override account label
    python scripts/import_claude_auth.py --label "murphy-max"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_CLAUDE_CREDENTIAL_PATHS = [
    "~/.claude/.credentials.json",
    "~/.claude/credentials.json",
    "~/.claude/auth.json",
]


def _find_credentials_file() -> str | None:
    """Auto-detect Claude Code CLI credentials file."""
    for path in _CLAUDE_CREDENTIAL_PATHS:
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
    return None


def _extract_credentials(auth_path: str) -> dict:
    """Extract credential fields from a Claude Code credentials file.

    The exact format may vary by CLI version. This function tries to handle
    known variations:
      - Claude Code CLI: {"claudeAiOauth": {"accessToken": "...", ...}}
      - Direct snake_case: {"access_token": "...", "refresh_token": "...", ...}
      - Nested: {"tokens": {"access_token": "...", ...}}
      - Account list: {"accounts": [{"access_token": "...", ...}]}
    """
    with open(auth_path) as f:
        data = json.load(f)

    # Claude Code CLI format: {"claudeAiOauth": {...}}
    if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
        return _extract_from_claude_cli(data["claudeAiOauth"])

    # Try direct format
    if "access_token" in data or "accessToken" in data:
        return _extract_from_flat(data)

    # Try nested tokens format
    if "tokens" in data and isinstance(data["tokens"], dict):
        return _extract_from_flat(data["tokens"])

    # Try account list format
    if "accounts" in data and isinstance(data["accounts"], list) and data["accounts"]:
        return _extract_from_flat(data["accounts"][0])

    print(f"Error: Unrecognized credential format in {auth_path}", file=sys.stderr)
    print(f"Keys found: {list(data.keys())}", file=sys.stderr)
    sys.exit(1)


def _extract_from_claude_cli(entry: dict) -> dict:
    """Extract fields from Claude Code CLI format (camelCase keys).

    Format:
        {
            "accessToken": "sk-ant-oat-...",
            "refreshToken": "...",
            "expiresAt": 1773702740076,      # milliseconds
            "scopes": ["user:inference", ...],
            "subscriptionType": "max",
            "rateLimitTier": "..."
        }
    """
    access_token = entry.get("accessToken", "")
    refresh_token = entry.get("refreshToken", "")

    if not access_token or not refresh_token:
        print("Error: Missing accessToken or refreshToken", file=sys.stderr)
        sys.exit(1)

    expires_at_ms = int(entry.get("expiresAt", 0))
    plan = entry.get("subscriptionType", "pro")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at_ms,
        "organization_id": "",  # Not available in CLI credentials
        "email": "",  # Not available in CLI credentials
        "plan": plan,
    }


def _extract_from_flat(entry: dict) -> dict:
    """Extract fields from a flat credential dict (supports both snake_case and camelCase)."""
    access_token = entry.get("access_token", "") or entry.get("accessToken", "")
    refresh_token = entry.get("refresh_token", "") or entry.get("refreshToken", "")

    if not access_token or not refresh_token:
        print("Error: Missing access_token or refresh_token", file=sys.stderr)
        sys.exit(1)

    # Expiry: try expires_at (ms), exp (s), or expires_in (s from now)
    expires_at_ms = 0
    if "expires_at" in entry:
        val = int(entry["expires_at"])
        # If it looks like seconds (< 2e10), convert to ms
        expires_at_ms = val if val > 2_000_000_000_000 else val * 1000
    elif "exp" in entry:
        expires_at_ms = int(entry["exp"]) * 1000
    elif "expires_in" in entry:
        expires_at_ms = int(time.time() * 1000) + int(entry["expires_in"]) * 1000

    # Organization
    org_id = ""
    org = entry.get("organization")
    if isinstance(org, dict):
        org_id = org.get("uuid", org.get("id", ""))
    elif isinstance(entry.get("organization_id"), str):
        org_id = entry["organization_id"]

    # Email
    email = ""
    acct_info = entry.get("account")
    if isinstance(acct_info, dict):
        email = acct_info.get("email_address", acct_info.get("email", ""))
    elif isinstance(entry.get("email"), str):
        email = entry["email"]

    # Plan
    plan = entry.get("plan", "pro")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at_ms,
        "organization_id": org_id,
        "email": email,
        "plan": plan,
    }


def main():
    """Import Claude Code CLI credentials into the hybridInference accounts file."""
    parser = argparse.ArgumentParser(
        description="Import Claude Code CLI auth into hybridInference accounts file"
    )
    parser.add_argument(
        "--claude-auth",
        default=None,
        help="Path to Claude CLI credentials file (default: auto-detect from ~/.claude/)",
    )
    parser.add_argument(
        "--output",
        default="var/data/claude_accounts.json",
        help="Output accounts file (default: var/data/claude_accounts.json)",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Account ID to use (default: acct_01 or next available)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Human-readable label for this account",
    )
    args = parser.parse_args()

    # Find source credentials
    auth_path = args.claude_auth
    if auth_path is None:
        auth_path = _find_credentials_file()
        if auth_path is None:
            print(
                "Error: Could not find Claude Code CLI credentials.\n"
                "Searched:\n"
                + "\n".join(f"  - {os.path.expanduser(p)}" for p in _CLAUDE_CREDENTIAL_PATHS)
                + "\n\nRun `claude login` first to authenticate with Claude Code CLI,\n"
                "or specify --claude-auth PATH explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not os.path.exists(auth_path):
        print(f"Error: {auth_path} not found.", file=sys.stderr)
        sys.exit(1)

    creds = _extract_credentials(auth_path)
    print(f"Read credentials from {auth_path}")
    print(f"  organization_id: {creds['organization_id'] or '(not found)'}")
    print(f"  email: {creds['email'] or '(not found)'}")
    print(f"  plan: {creds['plan']}")
    print(f"  expires_at: {creds['expires_at']} (ms)")
    token_preview = creds["access_token"]
    if len(token_preview) > 20:
        token_preview = "..." + token_preview[-20:]
    print(f"  access_token: {token_preview}")

    # Load existing accounts file or start fresh
    existing_accounts: list[dict] = []
    if os.path.exists(args.output):
        with open(args.output) as f:
            existing_data = json.load(f)
            existing_accounts = existing_data.get("accounts", [])
        print(f"Loaded {len(existing_accounts)} existing account(s) from {args.output}")

    # Find or create account entry
    # Merge rules (ordered by priority):
    #   1. Explicit --account-id with existing match → update in-place
    #   2. refresh_token matches (same OAuth session) → update in-place
    #   3. org_id matches AND (email matches or either is empty) → update in-place
    #   4. Otherwise → append new account
    account_id = args.account_id
    matched = False
    for acct in existing_accounts:
        is_match = False
        # Rule 1: explicit --account-id
        if (
            (account_id and acct.get("id") == account_id)
            or (not account_id and acct.get("refresh_token") == creds["refresh_token"])
            or (
                not account_id
                and creds["organization_id"]
                and acct.get("organization_id") == creds["organization_id"]
                and (
                    not creds["email"]
                    or not acct.get("email")
                    or acct.get("email") == creds["email"]
                )
            )
        ):
            is_match = True

        if is_match:
            # Check if re-importing into a revoked account → reset to active
            was_revoked = acct.get("state") in ("revoked", "disabled")
            if was_revoked:
                old_reason = acct.get("revoke_reason", "")
                acct["state"] = "active"
                acct["consecutive_failures"] = 0
                acct["revoke_reason"] = ""
                acct["state_changed_at"] = int(time.time() * 1000)
                print(
                    f"Account {acct.get('id')} was {acct.get('state', 'revoked')} "
                    f"(reason: {old_reason or 'unknown'}), "
                    f"resetting to active with new credentials."
                )

            acct["access_token"] = creds["access_token"]
            acct["refresh_token"] = creds["refresh_token"]
            acct["expires_at"] = creds["expires_at"]
            acct["plan"] = creds["plan"]
            if creds["organization_id"]:
                acct["organization_id"] = creds["organization_id"]
            if creds["email"]:
                acct["email"] = creds["email"]
            if args.label:
                acct["label"] = args.label
            matched = True
            account_id = acct["id"]
            print(f"Updated existing account: {account_id}")
            break

    if not matched:
        if not account_id:
            existing_ids = {a.get("id", "") for a in existing_accounts}
            for i in range(1, 100):
                candidate = f"acct_{i:02d}"
                if candidate not in existing_ids:
                    account_id = candidate
                    break

        label = args.label or f"claude-{creds['plan']}-{account_id}"
        new_entry = {
            "id": account_id,
            "label": label,
            "type": "oauth",
            "access_token": creds["access_token"],
            "refresh_token": creds["refresh_token"],
            "expires_at": creds["expires_at"],
            "organization_id": creds["organization_id"],
            "email": creds["email"],
            "plan": creds["plan"],
            "state": "active",
            "state_changed_at": int(time.time() * 1000),
            "consecutive_failures": 0,
            "revoke_reason": "",
        }
        existing_accounts.append(new_entry)
        print(f"Added new account: {account_id} ({label})")

    # Ensure output directory exists
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Write atomically (v2 format with version field)
    output_data = json.dumps({"version": 2, "accounts": existing_accounts}, indent=2)
    with open(args.output, "w") as f:
        f.write(output_data)
    os.chmod(args.output, 0o600)

    print(f"\nWritten to {args.output} (mode 0600)")
    print(f"Total accounts: {len(existing_accounts)}")
    print("\nNext steps:")
    print("  1. Verify claude-sonnet-4.6 is enabled in config/models.yaml")
    print("  2. Restart the server")
    print("  3. Test: curl -X POST http://localhost:8000/v1/chat/completions \\")
    print('       -H "Content-Type: application/json" \\')
    print(
        '       -d \'{"model": "claude-sonnet-4.6", '
        '"messages": [{"role": "user", "content": "Hello"}]}\''
    )


if __name__ == "__main__":
    main()
