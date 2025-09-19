from __future__ import annotations

"""Application bootstrap utilities.

This module centralizes initialization and shutdown of core services such as
the routing executor, model registry, database logger, and rate limiter. It is
intentionally free of HTTP concerns so it can be imported from multiple entry
points (e.g., CLI tools, tests, or the FastAPI app factory).
"""

import contextlib
import os
from pathlib import Path

from dotenv import load_dotenv

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from serving.adapters import (
    DeepSeekAdapter,
    GeminiAdapter,
    LlamaAdapter,
)
from serving.http import AsyncHTTPClient
from serving.storage.database import DatabaseLogger
from serving.storage.database_sqlite import SQLiteDatabaseLogger
from serving.utils.logging import get_logger, setup_logging

from .deps import AppServices
from .rate_limiter import PersistentRateLimiter, RateLimitConfig
from .registry import register_from_models_yaml

logger = get_logger(__name__)


def _apply_hard_offload(router: RouteExecutor, local_base_url: str) -> None:
    """Apply hard OFFLOAD by filtering out local adapters from routes.

    In hybrid mode, a model may have both local and remote adapters.
    Hard OFFLOAD removes all local adapters, leaving only remote ones.
    This is different from soft OFFLOAD which adjusts weights via RoutingManager.

    Args:
        router: The route executor to modify
        local_base_url: The base URL identifying local services
    """
    normalized_local = local_base_url.rstrip("/").lower()
    models_affected = 0
    adapters_removed = 0

    for model_id, route in list(router.routes.items()):
        # Filter out local adapters from the route
        original_count = len(route.adapters)
        filtered_adapters = []

        for adapter, weight in route.adapters:
            base_url = getattr(adapter.config, "base_url", None)
            if base_url and isinstance(base_url, str):
                normalized_url = base_url.rstrip("/").lower()
                if normalized_url == normalized_local:
                    # Skip local adapter
                    adapters_removed += 1
                    logger.debug(
                        f"OFFLOAD: Removing local adapter from {model_id} "
                        f"(provider={adapter.config.provider}, base_url={base_url})"
                    )
                else:
                    # Keep remote adapter
                    filtered_adapters.append((adapter, weight))
            else:
                # Keep adapters without base_url (shouldn't happen but be safe)
                filtered_adapters.append((adapter, weight))

        if len(filtered_adapters) < original_count:
            models_affected += 1

        if filtered_adapters:
            # Update route with only non-local adapters
            route.adapters = filtered_adapters
        else:
            # Remove route entirely if no adapters left
            router.routes.pop(model_id)
            logger.info(f"OFFLOAD: Removed route {model_id} (no non-local adapters)")

    if adapters_removed > 0:
        logger.info(
            f"Hard OFFLOAD applied: removed {adapters_removed} local adapters "
            f"from {models_affected} models"
        )


def _init_db_logger() -> DatabaseLogger | None:
    """Initialize a database logger based on environment configuration.

    Returns:
        Optional[DatabaseLogger]: A database logger instance or None when
        logging is disabled or misconfigured.
    """

    use_sqlite = os.getenv("USE_SQLITE_LOG", "true").lower() == "true"
    if use_sqlite:
        sqlite_env = os.getenv("SQLITE_DB_PATH") or os.getenv("OPENROUTER_SQLITE_DB")
        if sqlite_env:
            db_path = Path(sqlite_env).expanduser().resolve()
        else:
            project_root = Path(__file__).resolve().parents[2]
            var_dir = Path(os.getenv("VAR_DIR") or (project_root / "var"))
            db_dir = var_dir / "db"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "openrouter_logs.db"
        logger.info(f"SQLite database path: {db_path}")
        return SQLiteDatabaseLogger(str(db_path))

    db_url = os.getenv("DATABASE_URL")
    if db_url:
        db_config = {"dsn": db_url}
        return DatabaseLogger(db_config)
    return None


async def _init_router_and_models(router: RouteExecutor) -> None:
    """Register models on the router from YAML configuration.

    All models should be configured via YAML for consistency and flexibility.
    Supports hybrid mode where a single model can have multiple adapters
    (e.g., local VLLM and remote API) for failover and load balancing.
    """

    # Load models from YAML configuration
    try:
        models_env = os.getenv("MODELS_CONFIG")
        models_path = Path(models_env or "config/models.yaml")
        if models_env and not models_path.exists():
            logger.warning(f"Models config not found: {models_path}")
        elif models_path.exists():
            registered = register_from_models_yaml(router, models_path)
            if registered:
                logger.info(f"Registered {registered} routes from {models_path}")
    except Exception as exc:
        logger.warning(f"Failed to load models.yaml: {exc}")

    # Hard OFFLOAD: Filter out local adapters from multi-adapter routes
    # This is different from soft OFFLOAD which adjusts weights in RoutingManager
    offload_flag = os.getenv("OFFLOAD", "0").strip().lower()
    offload_enabled = offload_flag in ("1", "true", "yes")

    if offload_enabled:
        local_base_url = os.getenv("LOCAL_BASE_URL", "")
        if local_base_url:
            _apply_hard_offload(router, local_base_url)
        else:
            logger.info("OFFLOAD=1 but LOCAL_BASE_URL not set; no adapters filtered")


def _apply_routing_manager(router: RouteExecutor) -> RoutingManager | None:
    """Optionally load the routing manager and apply weights from YAML.

    Returns:
        Optional[RoutingManager]: The active manager when configuration exists,
        otherwise None.
    """

    try:
        routing_env = os.getenv("ROUTING_CONFIG")
        routing_cfg_path = Path(routing_env or "config/routing.yaml")
        if routing_env and not routing_cfg_path.exists():
            logger.warning(f"Routing config not found: {routing_cfg_path}")
        elif routing_cfg_path.exists():
            manager = RoutingManager(router, routing_cfg_path)
            manager.load()
            updated = manager.apply()
            if updated:
                logger.info(
                    f"RoutingManager applied weights to {updated} routes from {routing_cfg_path}"
                )
            else:
                logger.info("RoutingManager loaded; no routes updated (check config)")
            return manager
        else:
            logger.info("No routing config found; using default routes")
    except Exception as exc:
        logger.warning(f"RoutingManager failed to initialize: {exc}")
    return None


def _configure_rate_limiter(limiter: PersistentRateLimiter) -> None:
    """Configure model-specific rate limits from environment variables."""

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        gemini_tpm = int(os.getenv("GEMINI_TPM_LIMIT", "1000000"))
        if gemini_tpm > 0:
            cfg = RateLimitConfig(
                model_id="gemini-2.5-flash",
                window_seconds=60,
                capacity_tokens=gemini_tpm,
                burst_multiplier=1.0,
                queue_size=100,
                enable_persistence=True,
            )
            limiter.configure(cfg)
            logger.info(f"Configured Gemini limit: {gemini_tpm:,}/min")

    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        deepseek_tpd = int(os.getenv("DEEPSEEK_TPD_LIMIT", "1000000"))
        if deepseek_tpd > 0:
            cfg = RateLimitConfig(
                model_id="deepseek-chat",
                window_seconds=86400,
                capacity_tokens=deepseek_tpd,
                burst_multiplier=1.0,
                queue_size=50,
                enable_persistence=True,
            )
            limiter.configure(cfg)
            logger.info(f"Configured DeepSeek limit: {deepseek_tpd:,}/day")


async def initialize() -> AppServices:
    """Initialize application services.

    Loads environment variables, sets up logging, constructs the router,
    registers models, optionally applies routing weights, initializes database
    logging, and configures the persistent rate limiter.

    Returns:
        AppServices: A typed container with initialized services.
    """

    setup_logging()
    load_dotenv()

    router = RouteExecutor()

    # Database logger
    db_logger = _init_db_logger()
    if db_logger:
        try:
            await db_logger.initialize()
        except Exception as exc:
            logger.warning(f"Database logger failed to initialize: {exc}")
            db_logger = None

    # Models into router
    await _init_router_and_models(router)

    # Routing manager (optional)
    routing_manager = _apply_routing_manager(router)

    # Rate limiter (optional)
    rate_limiter: PersistentRateLimiter | None = None
    if os.getenv("RATE_LIMIT_ENABLED", "1") == "1":
        rate_limiter = PersistentRateLimiter()
        _configure_rate_limiter(rate_limiter)
        await rate_limiter.initialize()
        logger.info("Rate limiter initialized with persistence")

    # Ensure a shared HTTP client is created lazily; no-op here.
    _ = AsyncHTTPClient.shared()

    return AppServices(
        router=router,
        rate_limiter=rate_limiter,
        db_logger=db_logger,
        routing_manager=routing_manager,
    )


async def shutdown(services: AppServices) -> None:
    """Gracefully shutdown resources initialized in :func:`initialize`.

    Args:
        services: The services container returned by :func:`initialize`.
    """

    # Database logger
    if services.db_logger:
        try:
            await services.db_logger.cleanup()
        except Exception as exc:
            logger.error(f"DB cleanup failed: {exc}")

    # Persist limiter state
    if services.rate_limiter:
        try:
            await services.rate_limiter._persist_state()
        except Exception as exc:
            logger.error(f"Persist rate limiter failed: {exc}")

    # Routing manager health monitor
    if services.routing_manager:
        try:
            await services.routing_manager.shutdown()
        except Exception as exc:
            logger.error(f"Routing manager shutdown failed: {exc}")

    # Close shared HTTP client
    with contextlib.suppress(Exception):
        await AsyncHTTPClient.shared().close()
