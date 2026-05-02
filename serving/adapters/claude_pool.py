"""Process-wide shared pool for Claude subscription accounts.

Both ``ClaudeSubscriptionAdapter`` and the Anthropic proxy surface need
the same ``AccountPool`` + ``ClaudeCredentialProvider`` so that
health state (cooldown, consecutive failures) is consistent across all
code paths.

This module provides a lazy-initialised, thread-safe singleton.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from .claude_token import ClaudeCredentialProvider
    from .codex_token import AccountPool

logger = get_logger(__name__)

_provider: ClaudeCredentialProvider | None = None
_pool: AccountPool | None = None
_lock = threading.Lock()


def get_shared_pool() -> tuple[ClaudeCredentialProvider, AccountPool]:
    """Return the process-wide shared credential provider and account pool.

    Initialises on first call using ``claude_sub_*`` settings.  Thread-safe
    via a module-level lock; subsequent calls return the cached instances.
    """
    global _provider, _pool

    if _provider is not None and _pool is not None:
        return _provider, _pool

    with _lock:
        # Double-check after acquiring lock.
        if _provider is not None and _pool is not None:
            return _provider, _pool

        from serving.config.settings import get_settings

        from .claude_token import ClaudeCredentialProvider as _CCP
        from .codex_token import AccountPool as _AP

        settings = get_settings()

        provider = _CCP(
            accounts_file=settings.claude_sub_accounts_file,
            refresh_margin=settings.claude_sub_token_refresh_margin,
        )
        accounts = provider.load_accounts()

        pool = _AP(
            accounts=accounts,
            cooldown=settings.claude_sub_account_cooldown,
            failure_threshold=settings.claude_sub_failure_threshold,
        )

        _provider = provider
        _pool = pool
        logger.info(f"[claude_pool] Shared pool initialised with {len(accounts)} accounts")

        return _provider, _pool
