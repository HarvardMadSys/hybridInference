from __future__ import annotations

"""Application bootstrap utilities.

This module centralizes initialization and shutdown of core services such as
the routing executor, model registry, database logger, and rate limiter. It is
intentionally free of HTTP concerns so it can be imported from multiple entry
points (e.g., CLI tools, tests, or the FastAPI app factory).
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from database.database import DatabaseLogger
from database.database_sqlite import SQLiteDatabaseLogger
from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from serving.adapters import (
    DeepSeekAdapter,
    GeminiAdapter,
    LlamaAdapter,
    ModelConfig,
    VLLMAdapter,
)
from serving.http import AsyncHTTPClient
from utils.logging_utils import get_logger, setup_logging

from .deps import AppServices
from .rate_limiter import PersistentRateLimiter, RateLimitConfig
from .registry import register_from_models_yaml


logger = get_logger(__name__)


def _init_db_logger() -> Optional[DatabaseLogger]:
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
            data_dir = Path(os.getenv("DATA_DIR") or (project_root / "data"))
            db_dir = data_dir / "db"
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
    """Register models on the router from YAML and environment.

    This mirrors the legacy configuration to preserve behavior during the
    refactor. The preferred source is `config/models.yaml`; environment
    variables act as a fallback for simple setups.
    """

    # Try config-driven model registration first
    try:
        models_path = Path(os.getenv("MODELS_CONFIG", "config/models.yaml"))
        registered = register_from_models_yaml(router, models_path)
        if registered:
            logger.info(f"Registered {registered} routes from {models_path}")
    except Exception as exc:
        logger.warning(f"Failed to load models.yaml: {exc}")

    # Env-based fallback: register local VLLM models (freeinference.org or custom deployment)
    local_base_url = os.getenv("LOCAL_BASE_URL", "")
    offload_flag = os.getenv("OFFLOAD", "0").strip().lower()
    offload_enabled = offload_flag in ("1", "true", "yes")

    if local_base_url and not offload_enabled:
        llama_config = ModelConfig(
            id="llama-4-scout",
            name="Llama 4 Scout 17B",
            provider="vllm",
            base_url=local_base_url.rstrip("/"),
            provider_model_id="/models/meta-llama_Llama-4-Scout-17B-16E",
            quantization="bf16",
            context_length=262144,
            max_output_length=16384,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "frequency_penalty",
                "presence_penalty",
                "stop",
                "max_tokens",
                "seed",
            ],
        )
        llama_adapter = VLLMAdapter(llama_config)
        for alias in [
            "llama-4-scout",
            "/models/meta-llama_Llama-4-Scout-17B-16E",
        ]:
            router.register_route(alias, [(llama_adapter, 1.0)])

        qwen_config = ModelConfig(
            id="qwen3-coder",
            name="Qwen3 Coder 480B",
            provider="vllm",
            base_url=local_base_url.rstrip("/"),
            provider_model_id="/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8",
            quantization="fp8",
            context_length=32768,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature",
                "top_p",
                "top_k",
                "frequency_penalty",
                "presence_penalty",
                "stop",
                "max_tokens",
                "seed",
            ],
        )
        qwen_adapter = VLLMAdapter(qwen_config)
        for alias in [
            "qwen3-coder",
            "/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8",
        ]:
            router.register_route(alias, [(qwen_adapter, 1.0)])
        logger.info("Registered local VLLM models")
    elif local_base_url and offload_enabled:
        logger.info("OFFLOAD=1 detected: Skipping local VLLM model registration")

    # Register DeepSeek
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        config = ModelConfig(
            id="deepseek-chat",
            name="DeepSeek Chat",
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key=deepseek_key,
            quantization="bf16",
            context_length=65536,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature",
                "top_p",
                "max_tokens",
                "stop",
                "frequency_penalty",
                "presence_penalty",
            ],
        )
        deepseek_adapter = DeepSeekAdapter(config)
        router.register_route("deepseek-chat", [(deepseek_adapter, 1.0)])
        logger.info("Registered DeepSeek adapter")

    # Register Gemini
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        config = ModelConfig(
            id="gemini-2.5-flash",
            name="Gemini 2.5 Flash",
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key=gemini_key,
            quantization="bf16",
            input_modalities=["text", "image"],
            output_modalities=["text"],
            context_length=1048576,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=["temperature", "top_p", "top_k", "max_tokens", "stop"],
        )
        gemini_adapter = GeminiAdapter(config)
        router.register_route("gemini-2.5-flash", [(gemini_adapter, 1.0)])
        logger.info("Registered Gemini adapter")

    # Llama API
    llama_api_base = os.getenv("LLAMA_BASE_URL")
    llama_api_key = os.getenv("LLAMA_API_KEY")
    if llama_api_base and llama_api_key and llama_api_base != local_base_url:
        llama4_scout_config = ModelConfig(
            id="llama-4-scout",
            name="Llama 4 Scout",
            provider="llama",
            base_url=llama_api_base.rstrip("/"),
            api_key=llama_api_key,
            context_length=262144,
            max_output_length=16384,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "frequency_penalty",
                "presence_penalty",
                "stop",
                "max_tokens",
                "seed",
            ],
        )
        llama33_70b_config = ModelConfig(
            id="llama-3.3-70b-instruct",
            name="Llama 3.3 70B Instruct",
            provider="llama",
            base_url=llama_api_base.rstrip("/"),
            api_key=llama_api_key,
            context_length=131072,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "frequency_penalty",
                "presence_penalty",
                "stop",
                "max_tokens",
                "seed",
            ],
        )
        llama4_adapter = LlamaAdapter(llama4_scout_config)
        llama33_adapter = LlamaAdapter(llama33_70b_config)
        router.register_route("llama-4-scout", [(llama4_adapter, 1.0)])
        router.register_route("llama-3.3-70b-instruct", [(llama33_adapter, 1.0)])
        logger.info("Registered Llama API adapters")


def _apply_routing_manager(router: RouteExecutor) -> Optional[RoutingManager]:
    """Optionally load the routing manager and apply weights from YAML.

    Returns:
        Optional[RoutingManager]: The active manager when configuration exists,
        otherwise None.
    """

    try:
        routing_cfg_path = Path(os.getenv("ROUTING_CONFIG", "config/routing.yaml"))
        if routing_cfg_path.exists():
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
        logger.info("No routing config found; using default routes")
    except Exception as exc:
        logger.warning(f"RoutingManager failed to initialize: {exc}")
    return None


def _configure_rate_limiter(limiter: PersistentRateLimiter) -> None:
    """Configure model-specific rate limits from environment variables."""

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
        await db_logger.initialize()

    # Models into router
    await _init_router_and_models(router)

    # Routing manager (optional)
    routing_manager = _apply_routing_manager(router)

    # Rate limiter (optional)
    rate_limiter: Optional[PersistentRateLimiter] = None
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
        await services.db_logger.cleanup()

    # Persist limiter state
    if services.rate_limiter:
        await services.rate_limiter._persist_state()

    # Routing manager health monitor
    if services.routing_manager:
        await services.routing_manager.shutdown()

    # Close shared HTTP client
    try:
        await AsyncHTTPClient.shared().close()
    except Exception:
        pass
