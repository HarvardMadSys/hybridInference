"""OpenRouter-compatible API server with intelligent routing and load balancing."""

import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from serving.adapters import (
    BaseAdapter,
    ModelConfig,
    VLLMAdapter,
    DeepSeekAdapter,
    GeminiAdapter,
    LlamaAdapter
)
import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)
from database.database import DatabaseLogger
from database.database_sqlite import SQLiteDatabaseLogger
from serving.servers.rate_limiter import (
    PersistentRateLimiter,
    RateLimitConfig,
    TokenCounter
)

# Configure logging.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load .env from project root.
ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
load_dotenv(ENV_PATH)


@dataclass
class RouteConfig:
    """Configuration for weighted routing between adapters."""
    adapters: List[tuple[BaseAdapter, float]]  # List of (adapter, weight) pairs


class SimpleRouter:
    """Weighted routing for load balancing and automatic fallback.
    
    Distributes requests across multiple adapters based on configured weights,
    with automatic fallback to alternative adapters on failure.
    """
    
    def __init__(self) -> None:
        self.routes: Dict[str, RouteConfig] = {}
    
    def register_route(
        self,
        model_id: str,
        adapters_with_weights: List[tuple[BaseAdapter, float]]
    ) -> None:
        """Register adapters with weights for a model.
        
        Args:
            model_id: Model identifier.
            adapters_with_weights: List of (adapter, weight) tuples.
                Weights are normalized to sum to 1.0.
        """
        total_weight = sum(weight for _, weight in adapters_with_weights)
        if total_weight > 0:
            normalized = [
                (adapter, weight / total_weight)
                for adapter, weight in adapters_with_weights
            ]
            self.routes[model_id] = RouteConfig(adapters=normalized)
    
    def _select_adapter(self, model_id: str) -> Optional[BaseAdapter]:
        """Select an adapter using weighted random selection.
        
        Args:
            model_id: Model identifier.
            
        Returns:
            Selected adapter or None if no route configured.
        """
        route = self.routes.get(model_id)
        if not route or not route.adapters:
            return None
        
        # Weighted random selection.
        rand = random.random()
        cumulative = 0.0
        
        for adapter, weight in route.adapters:
            cumulative += weight
            if rand <= cumulative:
                return adapter
        
        # Fallback to last adapter (handles floating point rounding).
        return route.adapters[-1][0]
    
    async def chat_completion(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        **params: Any
    ) -> Dict[str, Any]:
        """Route chat completion request with automatic fallback.
        
        Args:
            model_id: Model identifier.
            messages: Chat messages.
            **params: Additional parameters.
            
        Returns:
            Response dictionary with routing metadata.
            
        Raises:
            ValueError: If no route configured or all adapters fail.
        """
        primary_adapter = self._select_adapter(model_id)
        if not primary_adapter:
            raise ValueError(f"No route configured for model {model_id}")
        
        logger.debug(f"Routing request for {model_id} to {primary_adapter.config.provider} ({primary_adapter.config.base_url})")
        
        # Try primary adapter first
        try:
            response = await primary_adapter.chat_completion(messages, **params)
            response["_routing"] = {
                "provider": primary_adapter.config.provider,
                "base_url": primary_adapter.config.base_url
            }
            return response
        except Exception as primary_error:
            # Fallback to other adapters.
            route = self.routes[model_id]
            for adapter, _ in route.adapters:
                if adapter == primary_adapter:
                    continue
                try:
                    response = await adapter.chat_completion(messages, **params)
                    response["_routing"] = {
                        "provider": adapter.config.provider,
                        "base_url": adapter.config.base_url,
                        "fallback": True
                    }
                    return response
                except Exception:
                    continue  # Try next adapter.
            
            # All adapters failed.
            raise primary_error
    
    async def stream_chat_completion(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        **params: Any
    ):
        """Stream chat completion with automatic fallback.
        
        Args:
            model_id: Model identifier.
            messages: Chat messages.
            **params: Additional parameters.
            
        Yields:
            Response chunks.
            
        Raises:
            ValueError: If no route configured or all adapters fail.
        """
        primary_adapter = self._select_adapter(model_id)
        if not primary_adapter:
            raise ValueError(f"No route configured for model {model_id}")
        
        logger.debug(f"Routing request for {model_id} to {primary_adapter.config.provider} ({primary_adapter.config.base_url})")
        
        # Try primary adapter
        try:
            async for chunk in primary_adapter.stream_chat_completion(messages, **params):
                yield chunk
            return
        except Exception as primary_error:
            # Fallback to other adapters.
            route = self.routes[model_id]
            for adapter, _ in route.adapters:
                if adapter == primary_adapter:
                    continue
                try:
                    async for chunk in adapter.stream_chat_completion(messages, **params):
                        yield chunk
                    return
                except Exception:
                    continue  # Try next adapter.
            
            # All adapters failed.
            raise primary_error

router = SimpleRouter()
db_logger: Optional[DatabaseLogger] = None


# Global rate limiter instance.
rate_limiter: Optional[PersistentRateLimiter] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    await startup_event()
    yield
    await shutdown_event()


app = FastAPI(
    title="OpenRouter-Compatible API Server",
    description="Unified API server supporting VLLM, DeepSeek, Gemini, and Llama models",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def startup_event() -> None:
    """Initialize server components on startup."""
    global db_logger, router, rate_limiter
    
    # Initialize database logger.
    # Use SQLite for easy demo (no PostgreSQL needed).
    use_sqlite = os.getenv("USE_SQLITE_LOG", "true").lower() == "true"
    
    if use_sqlite:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'db', 'openrouter_logs.db')
        db_logger = SQLiteDatabaseLogger(db_path)
        await db_logger.initialize()
        logger.info(f"SQLite database initialized at {db_path}")
    else:
        # PostgreSQL if configured.
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            db_config = {
                "dsn": db_url
            }
            db_logger = DatabaseLogger(db_config)
            await db_logger.initialize()
            logger.info("PostgreSQL database initialized")
    
    # Register local VLLM models (freeinference.org or custom deployment).
    local_base_url = os.getenv("LOCAL_BASE_URL", "")
    
    if local_base_url:
        # Register Llama-4-Scout model.
        llama_config = ModelConfig(
            id="/models/meta-llama_Llama-4-Scout-17B-16E",
            name="Llama 4 Scout 17B",
            provider="vllm",
            base_url=local_base_url.rstrip("/"),
            context_length=262144,  # 256K context
            max_output_length=16384,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature", "top_p", "top_k", "min_p",
                "frequency_penalty", "presence_penalty",
                "stop", "max_tokens", "seed"
            ]
        )
        llama_adapter = VLLMAdapter(llama_config)
        
        # Register with essential aliases only
        llama_aliases = [
            "/models/meta-llama_Llama-4-Scout-17B-16E",
            "llama-4-scout"
        ]
        for alias in llama_aliases:
            router.register_route(alias, [(llama_adapter, 1.0)])
        
        # Register Qwen3-Coder model.
        qwen_config = ModelConfig(
            id="/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8",
            name="Qwen3 Coder 480B",
            provider="vllm",
            base_url=local_base_url.rstrip("/"),
            context_length=32768,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature", "top_p", "top_k",
                "frequency_penalty", "presence_penalty",
                "stop", "max_tokens", "seed"
            ]
        )
        qwen_adapter = VLLMAdapter(qwen_config)
        
        # Register with essential aliases only
        qwen_aliases = [
            "/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8",
            "qwen3-coder"
        ]
        for alias in qwen_aliases:
            router.register_route(alias, [(qwen_adapter, 1.0)])
        
        logger.info("Registered local VLLM models")
    
    # Register DeepSeek (single endpoint, no routing needed).
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        config = ModelConfig(
            id="deepseek-chat",
            name="DeepSeek Chat",
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key=deepseek_key,
            context_length=65536,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature", "top_p", "max_tokens",
                "stop", "frequency_penalty", "presence_penalty"
            ]
        )
        deepseek_adapter = DeepSeekAdapter(config)
        
        # Register with single standard name
        router.register_route("deepseek-chat", [(deepseek_adapter, 1.0)])
        
        logger.info("Registered DeepSeek adapter")
        
        # Configure rate limit: 1M tokens per day (configurable via env)
        # Rate limiting will be configured after rate_limiter initialization
    
    # Register Gemini (single endpoint, no routing needed).
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        config = ModelConfig(
            id="gemini-2.5-flash",
            name="Gemini 2.5 Flash",
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key=gemini_key,
            context_length=1048576,
            max_output_length=8192,
            supports_tools=True,
            supports_structured_output=True,
            supported_params=[
                "temperature", "top_p", "top_k",
                "max_tokens", "stop"
            ]
        )
        gemini_adapter = GeminiAdapter(config)
        
        # Register with single standard name
        router.register_route("gemini-2.5-flash", [(gemini_adapter, 1.0)])
        
        logger.info("Registered Gemini adapter")
        
        # Configure rate limit: 1M tokens per minute (configurable via env)  
        # Rate limiting will be configured after rate_limiter initialization
    
    # Optional: Setup Llama API routing if configured separately.
    llama_api_base = os.getenv("LLAMA_API_BASE", "")
    llama_api_key = os.getenv("LLAMA_API_KEY")
    
    if llama_api_base and llama_api_key:
        # Only register Llama API if it's different from local base.
        if llama_api_base != local_base_url:
            api_config = ModelConfig(
                id="llama-api",
                name="Llama API",
                provider="llama",
                base_url=llama_api_base.rstrip("/"),
                api_key=llama_api_key,
                context_length=262144,
                max_output_length=16384,
                supports_tools=True,
                supports_structured_output=True,
                supported_params=[
                    "temperature", "top_p", "top_k", "min_p",
                    "frequency_penalty", "presence_penalty",
                    "stop", "max_tokens", "seed"
                ]
            )
            llama_api_adapter = LlamaAdapter(api_config)
            router.register_route("llama-api", [(llama_api_adapter, 1.0)])
            logger.info("Registered Llama API adapter")
    
    # Initialize rate limiter with persistence.
    if os.getenv("RATE_LIMIT_ENABLED", "1") == "1":
        rate_limiter = PersistentRateLimiter()
        
        # Configure DeepSeek rate limit.
        if deepseek_key:
            deepseek_tpd = int(os.getenv("DEEPSEEK_TPD_LIMIT", "1000000"))
            if deepseek_tpd > 0:
                config = RateLimitConfig(
                    model_id="deepseek-chat",
                    window_seconds=86400,
                    capacity_tokens=deepseek_tpd,
                    burst_multiplier=1.0,
                    queue_size=50,
                    enable_persistence=True
                )
                rate_limiter.configure(config)
                logger.info(f"Configured DeepSeek rate limit: {deepseek_tpd:,} tokens per day")
        
        # Configure Gemini rate limit.
        if gemini_key:
            gemini_tpm = int(os.getenv("GEMINI_TPM_LIMIT", "1000000"))
            if gemini_tpm > 0:
                config = RateLimitConfig(
                    model_id="gemini-2.5-flash",
                    window_seconds=60,
                    capacity_tokens=gemini_tpm,
                    burst_multiplier=1.0,
                    queue_size=100,
                    enable_persistence=True
                )
                rate_limiter.configure(config)
                logger.info(f"Configured Gemini rate limit: {gemini_tpm:,} tokens per minute")
        
        await rate_limiter.initialize()
        logger.info("Rate limiter initialized with persistence")


async def shutdown_event() -> None:
    """Clean up resources on shutdown."""
    if db_logger:
        await db_logger.cleanup()
    
    if rate_limiter:
        await rate_limiter._persist_state()


@app.get("/")
async def root() -> Dict[str, Any]:
    """Root endpoint showing API information."""
    return {
        "message": "OpenRouter-Compatible API Server",
        "version": "2.0.0",
        "features": ["Load balancing", "Automatic fallback", "Database logging", "Advanced rate limiting"],
        "endpoints": {
            "/v1/chat/completions": "Chat completions endpoint",
            "/v1/models": "List available models",
            "/openrouter/models": "OpenRouter format models list",
            "/routing": "Show routing configuration",
            "/stats": "API usage statistics",
            "/health": "Health check",
            "/rate-limits": "Rate limit metrics for all models",
            "/rate-limits/{model_id}": "Rate limit status for specific model",
            "/rate-limits/{model_id}/reset": "Reset circuit breaker (POST)"
        }
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Health check endpoint."""
    routes_count = len(router.routes)
    
    return {
        "status": "healthy",
        "routes_configured": routes_count,
        "database_connected": db_logger is not None
    }


@app.get("/routing")
async def get_routing() -> Dict[str, Any]:
    """Show current routing configuration."""
    routing_info = {}
    for model_id, route in router.routes.items():
        routing_info[model_id] = [
            {
                "provider": adapter.config.provider,
                "base_url": adapter.config.base_url,
                "weight": f"{weight * 100:.0f}%"
            }
            for adapter, weight in route.adapters
        ]
    
    return {
        "routes": routing_info,
        "description": "Weight distribution for each model. Requests are randomly distributed based on weights."
    }


@app.get("/v1/models")
@app.get("/openrouter/models")
async def list_models() -> Dict[str, Any]:
    """List available models in OpenRouter format."""
    models = []
    for model_id in router.routes.keys():
        models.append({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "openrouter"
        })
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Handle chat completion requests with routing and fallback."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON in request body")
    
    model = body.get("model")
    if not model:
        raise HTTPException(400, "Missing 'model' in request")
    
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(400, "Missing 'messages' in request")
    
    # Check if model has routing configured
    if model not in router.routes:
        raise HTTPException(404, f"Model '{model}' not found")
    
    # Extract parameters
    params = {}
    for key in ["temperature", "top_p", "top_k", "min_p", 
                "max_tokens", "stop", "seed",
                "frequency_penalty", "presence_penalty",
                "tools", "tool_choice", "response_format"]:
        if key in body:
            params[key] = body[key]
    
    # Rate limit check with advanced features
    if rate_limiter:
        priority = 1 if authorization else 0  # Higher priority for authenticated requests
        success, metadata = await rate_limiter.acquire_tokens(
            model_id=model,
            messages=messages,
            max_tokens=params.get("max_tokens"),
            priority=priority,
            timeout=30.0
        )
        
        if not success:
            error_detail = {
                "error": {
                    "type": "rate_limit_exceeded",
                    "message": metadata.get("error", "Rate limit exceeded"),
                    "model": model,
                    "retry_after": metadata.get("retry_after", 60)
                }
            }
            
            if "tokens_requested" in metadata:
                error_detail["error"]["tokens_requested"] = metadata["tokens_requested"]
            if "queue_size" in metadata:
                error_detail["error"]["queue_size"] = metadata["queue_size"]
            
            headers = {
                "X-RateLimit-RetryAfter": str(metadata.get("retry_after", 60)),
                "X-RateLimit-Model": model
            }
            # Enrich headers with capacity, remaining, and window if available
            if rate_limiter:
                status = rate_limiter.get_status(model)
                if status.get("configured"):
                    headers.update({
                        "X-RateLimit-Limit": str(status.get("capacity")),
                        "X-RateLimit-Remaining": str(int(status.get("tokens_available", 0))),
                        "X-RateLimit-Window": str(int(status.get("window_seconds", 0)))
                    })
            
            raise HTTPException(
                status_code=429,
                detail=error_detail,
                headers=headers
            )
    
    # Generate request ID.
    request_id = f"req_{int(time.time() * 1000000)}"
    start_time = time.time()
    
    # Extract metadata for logging.
    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": request.client.host if request.client else None,
        "authorization": bool(authorization)
    }
    
    # Handle streaming.
    if body.get("stream", False):
        async def stream_generator():
            try:
                async for chunk in router.stream_chat_completion(model, messages, **params):
                    # Forward adapter SSE chunks directly. The adapter is
                    # responsible for emitting a final usage chunk.
                    yield chunk
                
                # Log the streaming request.
                if db_logger:
                    await db_logger.log_request(
                        request_id=request_id,
                        model_id=model,
                        provider="router",
                        prompt=messages,
                        response={"stream": True},
                        usage=None,  # Usage will be in the stream
                        latency_ms=int((time.time() - start_time) * 1000),
                        status_code=200,
                        params=params,
                        metadata=metadata
                    )
            except Exception as e:  # Consider catching specific exceptions
                if db_logger:
                    await db_logger.log_request(
                        request_id=request_id,
                        model_id=model,
                        provider="router",
                        prompt=messages,
                        response=None,
                        usage=None,
                        latency_ms=int((time.time() - start_time) * 1000),
                        status_code=500,
                        error=str(e),
                        params=params,
                        metadata=metadata
                    )
                # Release tokens on streaming error.
                if rate_limiter:
                    estimated_tokens = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
                    await rate_limiter.release_tokens(model, estimated_tokens)
                
                error_chunk = {
                    "error": {
                        "message": str(e),
                        "type": "server_error",
                        "code": 500
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
        
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no"
            }
        )
    
    # Non-streaming request.
    try:
        response = await router.chat_completion(model, messages, **params)
        
        # Log the request.
        if db_logger:
            # Extract provider from routing metadata if available.
            provider = "router"
            if "_routing" in response:
                provider = response["_routing"]["provider"]
                metadata.update(response["_routing"])
                del response["_routing"]  # Remove internal metadata.
            else:
                provider = "router"
            
            await db_logger.log_request(
                request_id=request_id,
                model_id=model,
                provider=provider,
                prompt=messages,
                response=response,
                usage=response.get("usage"),
                latency_ms=int((time.time() - start_time) * 1000),
                status_code=200,
                params=params,
                metadata=metadata
            )
        
        # Log actual token usage for metrics.
        if rate_limiter:
            actual_tokens = response.get("usage", {}).get("total_tokens")
            if actual_tokens:
                estimated = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
                if abs(actual_tokens - estimated) > estimated * 0.2:
                    logger.warning(f"Token estimation variance for {model}: "
                                 f"estimated {estimated}, actual {actual_tokens}")
        
        return response
        
    except Exception as e:
        # Release tokens on error.
        if rate_limiter:
            estimated_tokens = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
            await rate_limiter.release_tokens(model, estimated_tokens)
        # Log the error.
        if db_logger:
            provider = "router"
            await db_logger.log_request(
                request_id=request_id,
                model_id=model,
                provider=provider,
                prompt=messages,
                response=None,
                usage=None,
                latency_ms=int((time.time() - start_time) * 1000),
                status_code=500,
                error=str(e),
                params=params,
                metadata=metadata
            )
        
        raise HTTPException(500, str(e))


@app.get("/stats")
async def get_stats(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    hours: int = 24
):
    if not db_logger:
        return {"error": "Database logging not configured"}
    
    stats = await db_logger.get_stats(
        model_id=model,
        provider=provider,
        hours=hours
    )
    
    return {
        "period_hours": hours,
        "filters": {
            "model": model,
            "provider": provider
        },
        "stats": stats
    }


@app.get("/rate-limits/{model_id}")
async def get_rate_limit_status(model_id: str):
    """Get rate limit status and metrics for a specific model."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    
    status = rate_limiter.get_status(model_id)
    if not status.get("configured"):
        raise HTTPException(404, f"No rate limit configured for model '{model_id}'")
    
    return status


@app.get("/rate-limits")
async def get_all_rate_limits():
    """Get rate limit metrics for all models."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    
    return rate_limiter.get_metrics()


@app.post("/rate-limits/{model_id}/reset")
async def reset_circuit_breaker(model_id: str):
    """Reset circuit breaker for a model (admin endpoint)."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    
    rate_limiter.reset_circuit_breaker(model_id)
    return {"message": f"Circuit breaker reset for {model_id}"}


@app.post("/v1/completions")
async def completions(request: Request):
    # For backward compatibility, convert to chat completions.
    body = await request.json()
    
    # Convert prompt to messages format.
    prompt = body.get("prompt", "")
    messages = [{"role": "user", "content": prompt}]
    
    # Update body.
    body["messages"] = messages
    del body["prompt"]
    
    # Forward to chat completions.
    request._body = json.dumps(body).encode()
    return await chat_completions(request)


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8080"))
    workers = int(os.getenv("WORKERS", "1"))
    
    if workers > 1:
        uvicorn.run(
            "serving.servers.openrouter:app",
            host="0.0.0.0",
            port=port,
            workers=workers,
            loop="uvloop",
            log_level="info"
        )
    else:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            loop="uvloop",
            log_level="info"
        )
