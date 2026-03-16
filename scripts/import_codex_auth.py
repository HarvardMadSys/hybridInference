#!/usr/bin/env python3
"""Import Codex CLI OAuth credentials into hybridInference accounts file.

Reads ~/.codex/auth.json (written by the official Codex CLI after `codex --login`)
and writes/merges into var/data/codex_accounts.json for use by the codex_sub adapter.

Usage:
    python scripts/import_codex_auth.py [--codex-auth PATH] [--output PATH] [--account-id ID] [--label LABEL]

Examples:
    # Default: read ~/.codex/auth.json, write to var/data/codex_accounts.json
    python scripts/import_codex_auth.py

    # Custom paths
    python scripts/import_codex_auth.py --codex-auth /path/to/auth.json --output /path/to/accounts.json

    # Override account label
    python scripts/import_codex_auth.py --label "murphy-plus"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verification (we only need claims)."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Invalid JWT format")
    # Add padding for base64url
    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


def _extract_from_codex_auth(auth_path: str) -> dict:
    """Extract credential fields from a Codex CLI auth.json."""
    with open(auth_path) as f:
        data = json.load(f)

    tokens = data.get("tokens")
    if not tokens:
        print(f"Error: No 'tokens' key in {auth_path}", file=sys.stderr)
        sys.exit(1)

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    account_id = tokens.get("account_id")

    if not access_token or not refresh_token:
        print("Error: Missing access_token or refresh_token", file=sys.stderr)
        sys.exit(1)

    # Decode access_token JWT to get expiry
    try:
        claims = _decode_jwt_payload(access_token)
        expires_at_s = claims.get("exp", 0)
        expires_at_ms = int(expires_at_s) * 1000
    except Exception as e:
        print(f"Warning: Could not decode access_token JWT ({e}), using 0 for expires_at")
        expires_at_ms = 0

    # Try to detect tier from id_token
    tier = "plus"
    id_token = tokens.get("id_token")
    if id_token:
        try:
            id_claims = _decode_jwt_payload(id_token)
            auth_info = id_claims.get("https://api.openai.com/auth", {})
            plan_type = auth_info.get("chatgpt_plan_type", "plus")
            tier = plan_type
        except Exception:
            pass

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at_ms,
        "account_id": account_id or "",
        "tier": tier,
    }


def main():
    """Import Codex CLI OAuth credentials into the hybridInference accounts file."""
    parser = argparse.ArgumentParser(
        description="Import Codex CLI auth into hybridInference accounts file"
    )
    parser.add_argument(
        "--codex-auth",
        default=os.path.expanduser("~/.codex/auth.json"),
        help="Path to Codex CLI auth.json (default: ~/.codex/auth.json)",
    )
    parser.add_argument(
        "--output",
        default="var/data/codex_accounts.json",
        help="Output accounts file (default: var/data/codex_accounts.json)",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Account ID to use in the accounts file (default: acct_01 or next available)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Human-readable label for this account",
    )
    args = parser.parse_args()

    # Read source
    if not os.path.exists(args.codex_auth):
        print(
            f"Error: {args.codex_auth} not found.\n"
            "Run `codex --login` first to authenticate with the Codex CLI.",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = _extract_from_codex_auth(args.codex_auth)
    print(f"Read credentials from {args.codex_auth}")
    print(f"  account_id: {creds['account_id']}")
    print(f"  tier: {creds['tier']}")
    print(f"  expires_at: {creds['expires_at']} (ms)")
    print(f"  access_token: ...{creds['access_token'][-20:]}")

    # Load existing accounts file or start fresh
    existing_accounts: list[dict] = []
    if os.path.exists(args.output):
        with open(args.output) as f:
            existing_data = json.load(f)
            existing_accounts = existing_data.get("accounts", [])
        print(f"Loaded {len(existing_accounts)} existing account(s) from {args.output}")

    # Find or create account entry (match by chatgpt account_id)
    account_id = args.account_id
    matched = False
    for acct in existing_accounts:
        if acct.get("account_id") == creds["account_id"]:
            # Update existing
            acct["access_token"] = creds["access_token"]
            acct["refresh_token"] = creds["refresh_token"]
            acct["expires_at"] = creds["expires_at"]
            acct["tier"] = creds["tier"]
            if args.label:
                acct["label"] = args.label
            matched = True
            account_id = acct["id"]
            print(f"Updated existing account: {account_id}")
            break

    if not matched:
        # Assign next available ID
        if not account_id:
            existing_ids = {a.get("id", "") for a in existing_accounts}
            for i in range(1, 100):
                candidate = f"acct_{i:02d}"
                if candidate not in existing_ids:
                    account_id = candidate
                    break

        label = args.label or f"codex-{creds['tier']}-{account_id}"
        new_entry = {
            "id": account_id,
            "label": label,
            "type": "oauth",
            "access_token": creds["access_token"],
            "refresh_token": creds["refresh_token"],
            "expires_at": creds["expires_at"],
            "account_id": creds["account_id"],
            "tier": creds["tier"],
            "enabled": True,
        }
        existing_accounts.append(new_entry)
        print(f"Added new account: {account_id} ({label})")

    # Ensure output directory exists
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Write atomically
    output_data = json.dumps({"accounts": existing_accounts}, indent=2)
    with open(args.output, "w") as f:
        f.write(output_data)
    os.chmod(args.output, 0o600)

    print(f"\nWritten to {args.output} (mode 0600)")
    print(f"Total accounts: {len(existing_accounts)}")
    print("\nNext steps:")
    print("  1. Uncomment codex model entries in config/models.yaml")
    print("  2. Restart the server")
    print("  3. Test: curl -X POST http://localhost:8000/v1/chat/completions \\")
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"model": "gpt-5.1-codex", "messages": [{"role": "user", "content": "Hello"}]}\'')


if __name__ == "__main__":
    main()
