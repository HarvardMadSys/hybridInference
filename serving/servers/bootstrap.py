"""Application bootstrap utilities.

This module centralizes initialization and shutdown of core services such as
the routing executor, model registry, and database logger. It is intentionally
free of HTTP concerns so it can be imported from multiple entry points
(e.g., CLI tools, tests, or the FastAPI app factory).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from dotenv import load_dotenv

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from routing.model_router_registry import ModelRouterRegistry
from serving.adapters import ClaudeSubscriptionAdapter, CodexSubscriptionAdapter
from serving.config.settings import USER_CONCURRENCY_LIMITS, get_settings
from serving.http import AsyncHTTPClient
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils import email_scheduler
from serving.utils.logging import get_logger, setup_logging

from .concurrency import UserConcurrencyLimiter
from .deps import AppServices
from .registry import ModelRegistrationInfo, register_from_models_yaml

logger = get_logger(__name__)

# Strong references to fire-and-forget background tasks created at startup.
# asyncio holds only weak refs to running tasks, so without this set the
# garbage collector can cancel mid-flight tasks.
_BACKGROUND_TASKS: set = set()


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


async def _init_router_and_models(
    router: RouteExecutor,
) -> tuple[dict, list[ModelRegistrationInfo]]:
    """Register models on the router from YAML configuration.

    All models should be configured via YAML for consistency and flexibility.
    Supports hybrid mode where a single model can have multiple adapters
    (e.g., local VLLM and remote API) for failover and load balancing.

    Returns:
        Tuple of (embedding adapters dict, list of ModelRegistrationInfo).
    """
    embedding_adapters: dict = {}
    model_infos: list[ModelRegistrationInfo] = []

    # Load models from YAML configuration
    try:
        models_env = os.getenv("MODELS_CONFIG")
        models_path = Path(models_env or "config/models.yaml")
        if models_env and not models_path.exists():
            logger.warning(f"Models config not found: {models_path}")
        elif models_path.exists():
            registered, model_infos = register_from_models_yaml(
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
    return embedding_adapters, model_infos


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


async def initialize() -> AppServices:
    """Initialize application services.

    Loads environment variables, sets up logging, constructs the router,
    registers models, optionally applies routing weights, and initializes
    database logging.

    Returns:
        AppServices: A typed container with initialized services.
    """
    import asyncio

    # Load environment first so logging picks up LOG_FORMAT/LOG_LEVEL.
    load_dotenv()
    setup_logging()

    router = RouteExecutor()

    # Database logger — only needed when DB_BACKEND is postgres (default).
    # When DB_BACKEND=d1, all data goes to Cloudflare D1; skip Postgres entirely.
    settings = get_settings()
    db_logger = None
    if settings.db_backend != "d1":
        db_logger = _init_db_logger()
        if db_logger:
            max_retries = 3
            retry_delay = 2  # seconds
            for attempt in range(max_retries):
                try:
                    await db_logger.initialize()
                    logger.info("Database logger initialized successfully")
                    from serving.observability.metrics import DATABASE_CONNECTED

                    DATABASE_CONNECTED.set(1)
                    # Start broadcast email scheduler. Tear it down if rehydration
                    # fails to avoid a half-initialized scheduler running in background.
                    if db_logger.pool:
                        try:
                            email_scheduler.start_scheduler(db_logger.pool)
                            await email_scheduler.rehydrate_scheduled_broadcasts()
                            # Provider-stats hourly rollup
                            from serving.admin.provider_stats_rollup import (
                                backfill_if_empty,
                                backfill_token_columns,
                                register_rollup_job,
                            )

                            sched = email_scheduler.get_scheduler()
                            if sched is not None:
                                register_rollup_job(sched, db_logger.pool)

                                # Failed-request Slack alerter (no-op if
                                # SLACK_WEBHOOK_URL is unset).
                                from serving.admin.failed_request_alerter import (
                                    register_alerter_job,
                                )

                                register_alerter_job(db_logger.pool, settings)

                                # Run backfill in the background so a slow 30-day
                                # aggregation on a large api_logs table cannot
                                # block server startup or trip readiness checks.
                                async def _run_backfill(pool=db_logger.pool):
                                    try:
                                        await backfill_if_empty(pool, days=30)
                                    except Exception as bf_exc:
                                        logger.warning(
                                            f"provider-stats backfill failed (non-fatal): {bf_exc}"
                                        )
                                    try:
                                        await backfill_token_columns(pool, days=30)
                                    except Exception as bf_exc:
                                        logger.warning(
                                            f"provider-stats token backfill failed (non-fatal): {bf_exc}"
                                        )

                                _bf_task = asyncio.create_task(_run_backfill())
                                _BACKGROUND_TASKS.add(_bf_task)
                                _bf_task.add_done_callback(_BACKGROUND_TASKS.discard)
                        except Exception as sched_exc:
                            logger.error(f"Email scheduler startup failed: {sched_exc}")
                            try:
                                email_scheduler.stop_scheduler()
                            except Exception as stop_exc:
                                logger.error(
                                    f"Email scheduler teardown after startup failure also failed: "
                                    f"{stop_exc}"
                                )
                    break
                except Exception as exc:
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Database initialization failed (attempt {attempt + 1}/{max_retries}): "
                            f"{exc}. Retrying in {retry_delay}s..."
                        )
                        await asyncio.sleep(retry_delay)
                    else:
                        logger.error(
                            f"Database logger failed to initialize after {max_retries} attempts: "
                            f"{exc}. Service will start without database logging."
                        )
                        db_logger = None
                        from serving.observability.metrics import DATABASE_CONNECTED

                        DATABASE_CONNECTED.set(0)
    else:
        logger.info("DB_BACKEND=d1 — skipping PostgreSQL initialization")

    # Models into router
    embedding_adapters, model_infos = await _init_router_and_models(router)

    # Routing manager (optional)
    routing_manager = _apply_routing_manager(router)

    # RouteWise router (optional, per-model opt-in via models.yaml routing_strategy)
    model_router_registry: ModelRouterRegistry | None = None
    settings = get_settings()
    needs_routewise = settings.enable_routewise or any(
        info.strategy == "routewise" for info in model_infos
    )
    if needs_routewise:
        try:
            from routing.routewise import RouteWiseRouter, load_routewise_config

            rw_config = load_routewise_config()
            routewise_router = RouteWiseRouter(
                fixed_router=router,
                config=rw_config,
                experiment_mode=settings.experiment_mode,
            )
            model_router_registry = ModelRouterRegistry(default_router=router)
            for info in model_infos:
                if info.strategy == "routewise":
                    model_router_registry.register(info.model_id, routewise_router)
                    for alias in info.aliases:
                        model_router_registry.register(alias, routewise_router)
            # TODO: Wire canary rollout from routewise.yaml canary section.
            # Currently configure_canary() is never called; canary config is dead.
            # rw_config has canary fields; call model_router_registry.configure_canary()
            # once canary rollout is ready for production.
            rw_models = [i.model_id for i in model_infos if i.strategy == "routewise"]
            logger.info(f"RouteWise initialized for {len(rw_models)} model(s): {rw_models}")
        except Exception as exc:
            logger.warning(f"RouteWise initialization failed: {exc}. Using fixed routing.")
            model_router_registry = None

    # Build store abstractions
    operational_store = None
    log_store = None

    if settings.db_backend == "d1":
        # D1 for both operational tables and logs — no Postgres needed
        from serving.storage.d1_client import D1Client
        from serving.storage.d1_log import D1LogStore
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
            # Raw D1 stores
            d1_op_store = D1OperationalStore(d1_client)
            await d1_op_store.initialize()

            d1_log_store = D1LogStore(d1_client)
            await d1_log_store.initialize()

            # Dual-write: shadow-write to PostgreSQL when enabled
            if settings.db_dual_write:
                from serving.storage.dual_write import (
                    DualWriteLogStore,
                    DualWriteOperationalStore,
                )

                db_logger = _init_db_logger()
                if db_logger:
                    try:
                        await db_logger.initialize()
                        pg_op = PostgresOperationalStore(db_logger.pool)
                        pg_log = PostgresLogStore(
                            db_logger.pool,
                            store_full_prompts=settings.db_store_full_content,
                            use_chunked_hash=True,
                        )
                        d1_op_store = DualWriteOperationalStore(d1_op_store, pg_op)
                        d1_log_store = DualWriteLogStore(d1_log_store, pg_log)
                        logger.info("Dual-write enabled: D1 primary + PostgreSQL shadow")
                    except Exception as exc:
                        logger.warning(
                            "DB_DUAL_WRITE=1 but PostgreSQL failed to initialize: %s "
                            "— running D1-only without shadow",
                            exc,
                        )
                        db_logger = None
                else:
                    logger.warning(
                        "DB_DUAL_WRITE=1 but PostgreSQL unavailable — "
                        "running D1-only without shadow"
                    )

            # Cache wraps the (possibly dual-write) operational store
            operational_store = CachedOperationalStore(d1_op_store, InMemoryCache())
            log_store = d1_log_store
            logger.info("Operational store initialized (D1 + in-memory cache)")
            logger.info("Log store initialized (D1 with buffered writes)")

    elif db_logger and db_logger.pool:
        # Default: both stores backed by Postgres
        pg_operational = PostgresOperationalStore(db_logger.pool)
        await pg_operational.initialize()
        operational_store = CachedOperationalStore(pg_operational, InMemoryCache())
        log_store = PostgresLogStore(
            db_logger.pool,
            store_full_prompts=settings.db_store_full_content,
            use_chunked_hash=True,
        )
        logger.info("Operational store initialized (Postgres + in-memory cache)")
        logger.info("Log store initialized (Postgres)")

    # Per-user concurrency limiter (always on; in-process)
    user_concurrency_limiter = UserConcurrencyLimiter(USER_CONCURRENCY_LIMITS)
    logger.info("User concurrency limiter initialized: %s", USER_CONCURRENCY_LIMITS)

    # Ensure a shared HTTP client is created lazily; no-op here.
    _ = AsyncHTTPClient.shared()

    return AppServices(
        router=router,
        embedding_adapters=embedding_adapters or None,
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
        routing_manager=routing_manager,
        model_router_registry=model_router_registry,
        user_concurrency_limiter=user_concurrency_limiter,
    )


async def shutdown(services: AppServices) -> None:
    """Gracefully shutdown resources initialized in :func:`initialize`.

    Args:
        services: The services container returned by :func:`initialize`.
    """
    # Broadcast email scheduler
    try:
        email_scheduler.stop_scheduler()
    except Exception as exc:
        logger.error(f"Email scheduler shutdown failed: {exc}")

    # Log store (flushes D1 buffer on shutdown)
    if services.log_store:
        try:
            await services.log_store.cleanup()
        except Exception as exc:
            logger.error(f"Log store cleanup failed: {exc}")

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

    # Routing manager health monitor
    if services.routing_manager:
        try:
            await services.routing_manager.shutdown()
        except Exception as exc:
            logger.error(f"Routing manager shutdown failed: {exc}")

    # Close shared HTTP client
    with contextlib.suppress(Exception):
        await AsyncHTTPClient.shared().close()
