"""Application bootstrap utilities.

This module centralizes initialization and shutdown of core services such as
the routing executor, model registry, and database logger. It is intentionally
free of HTTP concerns so it can be imported from multiple entry points
(e.g., CLI tools, tests, or the FastAPI app factory).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from routing.model_router_registry import ModelRouterRegistry
from serving.config.model_concurrency import ModelConcurrencyResolver
from serving.config.model_visibility import ModelVisibilityResolver
from serving.config.settings import get_settings
from serving.config.weight_overrides import WeightOverrideResolver
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


async def _refresh_weight_override_snapshots(
    resolver: WeightOverrideResolver,
    *,
    interval_seconds: float = 10.0,
) -> None:
    """Periodically reload route weight overrides so workers converge after admin edits."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await resolver.load_all()
        except Exception:
            logger.warning("Route weight override snapshot refresh failed", exc_info=True)


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
        logger.debug(
            f"Initializing PostgreSQL logger: "
            f"{db_config['user']}@{db_config['host']}:{db_config['port']}/{db_config['database']}"
        )
        logger.info(f"Database privacy: store_full_content={settings.db_store_full_content}")
        return DatabaseLogger(
            db_config,
            store_full_prompts=settings.db_store_full_content,
        )
    except Exception as exc:
        logger.warning(f"Failed to create database logger: {exc}")
        return None


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
                router,
                models_path,
                embedding_adapters=embedding_adapters,
                continue_on_missing_env=True,
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
    # Load environment first so logging picks up LOG_FORMAT/LOG_LEVEL.
    load_dotenv()
    setup_logging()

    if os.environ.get("EXPERIMENT_MODE"):
        logger.warning(
            "EXPERIMENT_MODE env var is set but no longer has any effect — "
            "the flag was removed; fallback routing is now always on."
        )

    router = RouteExecutor()

    settings = get_settings()
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
                        elif settings.slack_webhook_url.strip():
                            logger.warning(
                                "Slack alerter not registered: APScheduler did not start"
                            )

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

    # Models into router
    embedding_adapters, model_infos = await _init_router_and_models(router)

    # Routing manager (optional)
    routing_manager = _apply_routing_manager(router)

    # Per-model router registry — config-driven from models.yaml.
    # Each model's `router:` field selects a strategy from the registry in
    # routing.strategies; `router_params:` is validated by the strategy's
    # Pydantic model.  Models that omit `router:` fall back to
    # routing.yaml's `default_router`.
    settings = get_settings()
    models_config: dict[str, dict[str, Any]] = {}
    for info in model_infos:
        # Effective router: explicit `router:` wins; otherwise the legacy
        # `routing_strategy:` (one-release shim) maps onto `router`.
        effective_router = info.router or info.strategy
        entry: dict[str, Any] = {}
        if effective_router is not None:
            entry["router"] = effective_router
        if info.router_params is not None:
            entry["router_params"] = info.router_params
        # Aliases share the canonical model's config.
        models_config[info.model_id] = entry
        for alias in info.aliases:
            models_config[alias] = entry

    # Load routing.yaml to read `default_router`.  RoutingManager loads the
    # same file internally for weight assignment but does not expose its
    # parsed RoutingConfig; we re-load here cheaply (small YAML).
    default_router_name = "fixed"
    try:
        from routing.config import load_routing_config

        routing_env = os.getenv("ROUTING_CONFIG")
        routing_cfg_path = Path(routing_env or "config/routing.yaml")
        if routing_cfg_path.exists():
            routing_cfg = load_routing_config(routing_cfg_path)
            default_router_name = routing_cfg.default_router
    except Exception as exc:
        logger.warning(f"Failed to read default_router from routing.yaml: {exc}; using 'fixed'.")

    # ENABLE_ROUTEWISE legacy: opts every model into routewise as the default.
    if settings.enable_routewise and default_router_name == "fixed":
        default_router_name = "routewise"

    model_router_registry: ModelRouterRegistry | None = ModelRouterRegistry(
        models_config=models_config,
        default_router_name=default_router_name,
    )
    model_router_registry.bind_fixed_router(router)

    # Eagerly construct routers for every known model so config errors
    # (bad strategy name, bad router_params) surface at boot, not on the
    # first request. Fail-fast per-model: a bad config for one model must
    # not silently disable the registry for all models.
    for info in model_infos:
        try:
            model_router_registry.get_router(info.model_id)
            for alias in info.aliases:
                model_router_registry.get_router(alias)
        except Exception as exc:
            logger.error(
                f"ModelRouterRegistry initialization failed for model '{info.model_id}': {exc}"
            )
            raise
    rw_models = [
        i.model_id
        for i in model_infos
        if type(model_router_registry.get_router(i.model_id)).__name__ == "RouteWiseRouter"
    ]
    if rw_models:
        logger.info(f"RouteWise initialized for {len(rw_models)} model(s): {rw_models}")

    managed_routers = model_router_registry.managed_routers()

    # Build store abstractions
    operational_store = None
    log_store = None

    if db_logger and db_logger.pool:
        pg_operational = PostgresOperationalStore(db_logger.pool)
        await pg_operational.initialize()
        operational_store = CachedOperationalStore(pg_operational, InMemoryCache())
        log_store = PostgresLogStore(
            db_logger.pool,
            store_full_prompts=settings.db_store_full_content,
        )
        logger.info("Operational store initialized (Postgres + in-memory cache)")
        logger.info("Log store initialized (Postgres)")

    # Ensure a shared HTTP client is created lazily; no-op here.
    _ = AsyncHTTPClient.shared()

    # Alerting framework (replaces Prometheus scaffolding). Dark-launched in
    # PR 1: defaults to disabled, no behavior change. Operators flip the
    # ALERTS_ENABLED env var (or set SLACK_ALERTS_WEBHOOK_URL) to turn it on.
    alert_engine = None
    if settings.alerts_enabled:
        try:
            import logging as _stdlogging

            from serving.observability.alert_config import load_alert_config
            from serving.observability.alert_rules import AlertEngine
            from serving.observability.log_handler import AlertingLogHandler

            alert_handler = AlertingLogHandler(maxsize=10_000)
            _req_log = _stdlogging.getLogger("serving.servers.middleware.request_log")
            _req_log.addHandler(alert_handler)
            _req_log.propagate = False
            _stdlogging.getLogger().addHandler(alert_handler)

            alert_cfg = load_alert_config(settings.alerts_config_path)
            alert_engine = AlertEngine(
                handler=alert_handler,
                config=alert_cfg,
                scheduler=email_scheduler.get_scheduler(),
                op_store=operational_store,
                log_store=log_store,
            )
            await alert_engine.start()
            logger.info("alert engine started")
        except Exception:
            logger.exception("Alert engine startup failed")
            alert_engine = None
    else:
        logger.info("alerts disabled (ALERTS_ENABLED=false)")

    # Seed dynamic provider keys from the operational store into the
    # adapter key pools registered during model loading. Best-effort —
    # a failure here should not prevent the server from starting.
    if operational_store is not None:
        try:
            from serving.adapters.dynamic_keys import apply_db_keys_at_boot

            await apply_db_keys_at_boot(operational_store)
        except Exception as exc:
            logger.warning(f"Failed to apply DB-backed provider keys at boot: {exc}")

    # Runtime settings (DB-backed feature flags with TTL cache)
    runtime_settings = None
    if operational_store:
        try:
            from serving.config.runtime_settings import (
                RUNTIME_SETTINGS_REGISTRY,
                init_runtime_settings,
            )

            runtime_settings = init_runtime_settings(operational_store)
            logger.info("Runtime settings initialized")

            for _key in RUNTIME_SETTINGS_REGISTRY:
                try:
                    await runtime_settings.get_bool(_key)
                except Exception:
                    logger.warning(
                        f"Runtime settings cache warmup failed for {_key!r}", exc_info=True
                    )
        except Exception as exc:
            logger.warning(f"Runtime settings initialization failed: {exc}")

    model_visibility_resolver = None
    model_concurrency_resolver = None
    weight_override_resolver = None
    weight_override_refresh_task = None
    if operational_store is not None:
        try:
            model_visibility_resolver = ModelVisibilityResolver(operational_store)
            logger.info("Model visibility resolver initialized")
        except Exception as exc:
            logger.warning(f"Model visibility resolver initialization failed: {exc}")
        try:
            model_concurrency_resolver = ModelConcurrencyResolver(operational_store)
            logger.info("Model concurrency resolver initialized")
        except Exception as exc:
            logger.warning(f"Model concurrency resolver initialization failed: {exc}")
        try:
            weight_override_resolver = WeightOverrideResolver(operational_store)
            await weight_override_resolver.load_all()
            router.weight_override_resolver = weight_override_resolver
            weight_override_refresh_task = asyncio.create_task(
                _refresh_weight_override_snapshots(weight_override_resolver)
            )
            _BACKGROUND_TASKS.add(weight_override_refresh_task)
            weight_override_refresh_task.add_done_callback(_BACKGROUND_TASKS.discard)
            logger.info("Route weight override resolver initialized")
        except Exception as exc:
            logger.warning(f"Route weight override resolver initialization failed: {exc}")

    # Per-user concurrency limiter — reads live caps from RuntimeSettings so
    # operators can tune them at runtime. Falls back to registry defaults
    # when runtime_settings is unavailable (e.g., DB not configured).
    from serving.servers.concurrency import static_limits_provider

    if runtime_settings is not None:
        rt = runtime_settings  # capture for closure

        async def _read_concurrency_limits() -> dict[str, int]:
            trial, free, pro, internal, admin = await asyncio.gather(
                rt.get_int("user_concurrency_trial"),
                rt.get_int("user_concurrency_free"),
                rt.get_int("user_concurrency_pro"),
                rt.get_int("user_concurrency_internal"),
                rt.get_int("user_concurrency_admin"),
            )
            return {
                "trial": trial,
                "free": free,
                "pro": pro,
                "internal": internal,
                "admin": admin,
            }

        user_concurrency_limiter = UserConcurrencyLimiter(_read_concurrency_limits)
        logger.info("User concurrency limiter initialized (runtime-tunable)")
    else:
        user_concurrency_limiter = UserConcurrencyLimiter(
            static_limits_provider({"trial": 1, "free": 3, "pro": 3, "internal": 10, "admin": 10})
        )
        logger.warning(
            "User concurrency limiter initialized with static defaults "
            "(runtime_settings unavailable)"
        )

    # CompletionsLogger — encapsulates DB-log scheduling and RouteWise
    # observation forwarding for /v1/chat/completions. Always constructed
    # (it tolerates ``log_store=None``); the handler depends on a live
    # instance via ``deps.get_completions_logger``.
    from serving.servers.routers.completions_logging import CompletionsLogger

    completions_logger = CompletionsLogger(
        log_store=log_store,
        model_router_registry=model_router_registry,
    )

    # PricingLookup + CostTracker — encapsulate the four pricing-lookup
    # blocks and the ``_schedule_cost_increment`` helper that previously
    # lived in completions.py. Both tolerate ``op_store=None``, so this is
    # always safe to construct even when the operational store is offline.
    from serving.servers.routers.completions_cost import CostTracker, PricingLookup

    pricing_lookup = PricingLookup(router=router)
    cost_tracker = CostTracker(op_store=operational_store, pricing=pricing_lookup)

    # Start managed router lifecycle hooks only after the rest of bootstrap has
    # succeeded, so a later startup failure cannot leave background tasks
    # running without a matching shutdown.
    for managed_router in managed_routers:
        try:
            await managed_router.start()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Managed router start() failed: {exc}")

    return AppServices(
        router=router,
        embedding_adapters=embedding_adapters or None,
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
        routing_manager=routing_manager,
        model_router_registry=model_router_registry,
        managed_routers=managed_routers,
        model_visibility_resolver=model_visibility_resolver,
        model_concurrency_resolver=model_concurrency_resolver,
        weight_override_resolver=weight_override_resolver,
        user_concurrency_limiter=user_concurrency_limiter,
        alert_engine=alert_engine,
        runtime_settings=runtime_settings,
        completions_logger=completions_logger,
        pricing_lookup=pricing_lookup,
        cost_tracker=cost_tracker,
        weight_override_refresh_task=weight_override_refresh_task,
    )


async def shutdown(services: AppServices) -> None:
    """Gracefully shutdown resources initialized in :func:`initialize`.

    Args:
        services: The services container returned by :func:`initialize`.
    """
    # Alert engine — stop drain task and remove scheduled jobs first so they
    # don't fire while we're tearing down stores below.
    if services.alert_engine is not None:
        try:
            await services.alert_engine.stop()
        except Exception:
            logger.exception("Alert engine shutdown failed")
        handler = getattr(services.alert_engine, "_handler", None)
        if handler is not None:
            import logging as _stdlogging

            _stdlogging.getLogger("serving.servers.middleware.request_log").removeHandler(handler)
            _stdlogging.getLogger().removeHandler(handler)

    # Broadcast email scheduler
    try:
        email_scheduler.stop_scheduler()
    except Exception as exc:
        logger.error(f"Email scheduler shutdown failed: {exc}")

    # Log store
    if services.log_store:
        try:
            await services.log_store.cleanup()
        except Exception as exc:
            logger.error(f"Log store cleanup failed: {exc}")

    # Operational store
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

    # Managed routers (cancels router-owned background tasks)
    for managed_router in services.managed_routers:
        try:
            await managed_router.stop()
        except Exception as exc:
            logger.error(f"Managed router shutdown failed: {exc}")

    if services.weight_override_refresh_task is not None:
        services.weight_override_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await services.weight_override_refresh_task

    # Close shared HTTP client
    with contextlib.suppress(Exception):
        await AsyncHTTPClient.shared().close()
