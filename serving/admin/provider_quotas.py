"""Provider quota fetchers for the admin dashboard 'Providers' tab.

Each public fetcher returns a `ProviderQuotaResult`. Errors are converted
to structured results — fetchers never raise out of the gather.
"""

from __future__ import annotations


def _mask_key(key: str) -> str:
    """Mask an API key or cookie for display.

    Returns first 8 + '...' + last 4 if key is at least 16 chars; otherwise
    returns a generic placeholder so we never leak short secrets.
    """
    if len(key) >= 16:
        return f"{key[:8]}...{key[-4:]}"
    return "***configured***"
