"""Application bootstrap utilities.

This module centralizes initialization and shutdown of core services such as
the routing executor, model registry, database logger, and rate limiter. It is
intentionally free of HTTP concerns so it can be imported from multiple entry
points (e.g., CLI tools, tests, or the FastAPI app factory).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from dotenv import load_dotenv

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from serving.adapters import ClaudeSubscriptionAdapter, CodexSubscriptionAdapter
from serving.config.settings import get_settings
from serving.http import AsyncHTTPClient
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore
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
    """Initialize PostgreSQL database logger from environment configuration.

    Returns:
        Optional[DatabaseLogger]: A PostgreSQL logger instance or None when
        database logging is explicitly disabled.
    """
    # Allow explicit opt-out via DB_ENABLED=false
    if os.getenv("DB_ENABLED", "true").lower() in ("false", "0", "no"):
        logger.info("Database logging disabled via DB_ENABLED=false")
        return None

    try:
        # Get settings dynamically to support test environment overrides
        settings = get_settings()

        # Use centralized settings instead of get_db_config()
        db_config = {
            "host": settings.db_host,
            "port": settings.db_port,
            "database": settings.db_name,
            "user": settings.db_user,
            "password": settings.db_password,
        }
        logger.info(
            f"Initializing PostgreSQL logger: "
            f"{db_config['user']}@{db_config['host']}:{db_config['port']}/{db_config['database']}"
        )
        logger.info(
            f"Database privacy: store_full_content={settings.db_store_full_content}, "
            f"4-token chunked hash enabled"
        )
        # Always use 4-token chunked hash
        return DatabaseLogger(
            db_config,
            store_full_prompts=settings.db_store_full_content,
            use_chunked_hash=True,
        )
    except Exception as exc:
        logger.warning(f"Failed to create database logger: {exc}")
        return None


def _warn_missing_subscription_files(router: RouteExecutor) -> None:
    """Warn at startup if subscription adapters are configured but accounts files are missing."""
    needs_codex = False
    needs_claude = False

    for route in router.routes.values():
        for adapter, _ in route.adapters:
            if isinstance(adapter, CodexSubscriptionAdapter):
                needs_codex = True
            elif isinstance(adapter, ClaudeSubscriptionAdapter):
                needs_claude = True

    if not needs_codex and not needs_claude:
        return

    settings = get_settings()

    if needs_codex and not Path(settings.codex_accounts_file).exists():
        logger.warning(
            f"codex_sub route configured but accounts file not found: "
            f"{settings.codex_accounts_file} — requests will fail until credentials are imported"
        )
    if needs_claude and not Path(settings.claude_sub_accounts_file).exists():
        logger.warning(
            f"claude_sub route configured but accounts file not found: "
            f"{settings.claude_sub_accounts_file} — requests will fail until credentials are imported"
        )


async def _init_router_and_models(router: RouteExecutor) -> dict:
    """Register models on the router from YAML configuration.

    All models should be configured via YAML for consistency and flexibility.
    Supports hybrid mode where a single model can have multiple adapters
    (e.g., local VLLM and remote API) for failover and load balancing.

    Returns:
        dict: Embedding adapters keyed by model id.
    """
    embedding_adapters: dict = {}

    # Load models from YAML configuration
    try:
        models_env = os.getenv("MODELS_CONFIG")
        models_path = Path(models_env or "config/models.yaml")
        if models_env and not models_path.exists():
            logger.warning(f"Models config not found: {models_path}")
        elif models_path.exists():
            registered = register_from_models_yaml(
                router, models_path, embedding_adapters=embedding_adapters
            )
            if registered:
                logger.info(f"Registered {registered} routes from {models_path}")
            if embedding_adapters:
                logger.info(
                    f"Registered {len(embedding_adapters)} embedding adapter(s): "
                    f"{list(embedding_adapters.keys())}"
                )
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

    _warn_missing_subscription_files(router)
    return embedding_adapters


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
            # Configure rate limit for both Gemini models with same policy
            for model_id in ["gemini-2.5-flash", "gemini-2.5-flash-preview-09-2025"]:
                cfg = RateLimitConfig(
                    model_id=model_id,
                    window_seconds=60,
                    capacity_tokens=gemini_tpm,
                    burst_multiplier=1.0,
                    queue_size=100,
                    enable_persistence=True,
                )
                limiter.configure(cfg)
            logger.info(f"Configured Gemini limit: {gemini_tpm:,}/min (both models)")

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

    # GLM models: default 1M tokens per hour
    glm_key = os.getenv("ZAI_API_KEY")
    if glm_key:
        glm_tph = int(os.getenv("GLM_TPH_LIMIT", "1000000"))  # 1M tokens per hour
        if glm_tph > 0:
            for model_id in (
                "glm-4.5",
                "glm-4.6",
                "glm-4.7",
                "glm-4.7-flash",
                "glm-5",
                "glm-5-turbo",
                "glm-5.1",
            ):
                cfg = RateLimitConfig(
                    model_id=model_id,
                    window_seconds=3600,  # 1 hour
                    capacity_tokens=glm_tph,
                    burst_multiplier=1.0,
                    queue_size=50,
                    enable_persistence=True,
                )
                limiter.configure(cfg)
            logger.info(
                f"Configured GLM limits: {glm_tph:,}/hour "
                "(glm-4.5, glm-4.6, glm-4.7, glm-4.7-flash, glm-5, glm-5-turbo, glm-5.1)"
            )


async def initialize() -> AppServices:
    """Initialize application services.

    Loads environment variables, sets up logging, constructs the router,
    registers models, optionally applies routing weights, initializes database
    logging, and configures the persistent rate limiter.

    Returns:
        AppServices: A typed container with initialized services.
    """
    import asyncio

    # Load environment first so logging picks up LOG_FORMAT/LOG_LEVEL.
    load_dotenv()
    setup_logging()

    router = RouteExecutor()

    # Database logger with retry logic
    db_logger = _init_db_logger()
    if db_logger:
        max_retries = 3
        retry_delay = 2  # seconds
        for attempt in range(max_retries):
            try:
                await db_logger.initialize()
                logger.info("Database logger initialized successfully")
                # Proactively update metric on successful initialization
                from serving.observability.metrics import DATABASE_CONNECTED

                DATABASE_CONNECTED.set(1)
                break
            except Exception as exc:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Database initialization failed (attempt {attempt + 1}/{max_retries}): {exc}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(
                        f"Database logger failed to initialize after {max_retries} attempts: {exc}. "
                        "Service will start without database logging."
                    )
                    db_logger = None
                    # Proactively update metric on initialization failure
                    from serving.observability.metrics import DATABASE_CONNECTED

                    DATABASE_CONNECTED.set(0)

    # Models into router
    embedding_adapters = await _init_router_and_models(router)

    # Routing manager (optional)
    routing_manager = _apply_routing_manager(router)

    # Rate limiter (optional)
    rate_limiter: PersistentRateLimiter | None = None
    if os.getenv("RATE_LIMIT_ENABLED", "1") == "1":
        rate_limiter = PersistentRateLimiter()
        _configure_rate_limiter(rate_limiter)
        await rate_limiter.initialize()
        logger.info("Rate limiter initialized with persistence")

    # Fairness scheduler (optional; requires rate_limiter for capacity checks)
    fairness_scheduler = None
    if os.getenv("FAIRNESS_ENABLED", "0") == "1":
        from .fairness import VTCFairnessScheduler

        fairness_scheduler = VTCFairnessScheduler(rate_limiter)
        logger.info(
            "VTC fairness scheduler initialised (rate_limiter=%s)",
            "attached" if rate_limiter is not None else "none",
        )

    # Build store abstractions
    operational_store = None
    log_store = None
    settings = get_settings()

    if settings.db_backend == "d1":
        # D1 for operational tables, Postgres for logs
        from serving.storage.d1_client import D1Client
        from serving.storage.d1_operational import D1OperationalStore

        if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
            logger.error(
                "DB_BACKEND=d1 but D1 credentials are missing. "
                "Set D1_ACCOUNT_ID, D1_DATABASE_ID, and D1_API_TOKEN."
            )
        else:
            d1_client = D1Client(
                account_id=settings.d1_account_id,
                database_id=settings.d1_database_id,
                api_token=settings.d1_api_token,
            )
            d1_store = D1OperationalStore(d1_client)
            await d1_store.initialize()
            operational_store = CachedOperationalStore(d1_store, InMemoryCache())
            logger.info("Operational store initialized (D1 + in-memory cache)")

        # Log store still requires Postgres
        if db_logger and db_logger.pool:
            log_store = PostgresLogStore(
                db_logger.pool,
                store_full_prompts=settings.db_store_full_content,
                use_chunked_hash=True,
            )
            logger.info("Log store initialized (Postgres)")
        else:
            logger.warning("DB_BACKEND=d1 but PostgreSQL not available — log store disabled")

    elif db_logger and db_logger.pool:
        # Default: both stores backed by Postgres
        pg_operational = PostgresOperationalStore(db_logger.pool)
        operational_store = CachedOperationalStore(pg_operational, InMemoryCache())
        log_store = PostgresLogStore(
            db_logger.pool,
            store_full_prompts=settings.db_store_full_content,
            use_chunked_hash=True,
        )
        logger.info("Operational store initialized (Postgres + in-memory cache)")
        logger.info("Log store initialized (Postgres)")

    # User statistics collector (optional)
    user_stats_collector = None
    if os.getenv("METRICS_ENABLED", "1") == "1" and operational_store:
        from serving.observability.user_stats import UserStatsCollector

        interval = int(os.getenv("USER_STATS_INTERVAL_SECONDS", "60"))
        user_stats_collector = UserStatsCollector(operational_store, interval_seconds=interval)
        user_stats_collector.start()
        logger.info(f"User stats collector started (interval: {interval}s)")

    # Ensure a shared HTTP client is created lazily; no-op here.
    _ = AsyncHTTPClient.shared()

    return AppServices(
        router=router,
        embedding_adapters=embedding_adapters or None,
        rate_limiter=rate_limiter,
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
        routing_manager=routing_manager,
        user_stats_collector=user_stats_collector,
        fairness_scheduler=fairness_scheduler,
    )


async def shutdown(services: AppServices) -> None:
    """Gracefully shutdown resources initialized in :func:`initialize`.

    Args:
        services: The services container returned by :func:`initialize`.
    """
    # Operational store (closes D1 HTTP client when backend=d1)
    if services.operational_store:
        try:
            await services.operational_store.cleanup()
        except Exception as exc:
            logger.error(f"Operational store cleanup failed: {exc}")

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

    # User stats collector
    if services.user_stats_collector:
        try:
            await services.user_stats_collector.shutdown()
        except Exception as exc:
            logger.error(f"User stats collector shutdown failed: {exc}")

    # Close shared HTTP client
    with contextlib.suppress(Exception):
        await AsyncHTTPClient.shared().close()
