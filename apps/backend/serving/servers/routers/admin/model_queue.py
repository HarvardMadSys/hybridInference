"""Admin view of an on-demand model host's queue: Admin -> Batches -> Model queue.

An on-demand host serves a model only once its scheduler gives the model's job
a GPU (a Slurm cluster's queue, for example), and until then answers its
requests ``model_queued``, which the gateway reads as waiting for capacity:
interactive callers are told so and batch requests wait. The host describes
that queue at a status address, ``MODEL_QUEUE_STATUS_URL``. This view reads it,
names this gateway's models routed to each served model, and counts the batch
requests waiting for them.

The status is JSON (``docs/developer/hpc-model-host.md`` describes it): the
host's ``cluster`` and ``scheduler`` names, ``refreshed_at`` (its last read of
the scheduler's queue), and ``models``, each with its served name, ``state``
(``serving``, ``loading``, ``queued`` or ``idle``) and ``jobs``. Times are epoch
seconds. A field the host leaves out reads as unknown, and one it adds is
ignored.

The address is fetched with ``MODEL_QUEUE_STATUS_API_KEY`` (``LOCAL_API_KEY``
when that is empty) as a bearer key. It is an administrator's setting, but the
request still follows no redirect, refuses cloud metadata addresses and reads
at most ``_MAX_STATUS_BYTES``, and a failure is reported in the response, not
raised, so the page can say what went wrong.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from fastapi import APIRouter, Depends
from pydantic import ValidationError

from serving.config.app_config import config_value
from serving.schemas_admin import ModelQueueResponse
from serving.servers.deps import get_batch_store, get_services, verify_admin_access
from serving.utils.logging import get_logger
from serving.utils.metadata_addresses import refuse_metadata_socket

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")

STATUS_URL_KEY = "MODEL_QUEUE_STATUS_URL"
STATUS_API_KEY_KEY = "MODEL_QUEUE_STATUS_API_KEY"
#: The key a status address takes when its own is empty: the one the gateway
#: already shares with its local inference proxies.
FALLBACK_API_KEY_KEY = "LOCAL_API_KEY"

#: How long the page waits for the host. Its status answers from memory.
_STATUS_TIMEOUT_SECONDS = 10.0
#: Largest status read. A host with a few dozen models answers in kilobytes.
_MAX_STATUS_BYTES = 2 * 1024 * 1024
_DEFAULT_PORTS = {"http": 80, "https": 443}


class StatusUnavailable(Exception):
    """The host's status could not be read; the message says why, for the page."""


def _setting(key: str) -> str:
    return (config_value(key) or "").strip()


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Scheme, host and port of *url*, the default port filled in; None if not a URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    return scheme, parts.hostname.lower(), port or _DEFAULT_PORTS.get(scheme)


async def fetch_status(url: str, api_key: str) -> Any:
    """GET the host's status at *url*, signed with *api_key* when there is one.

    Raises:
        StatusUnavailable: The address is not http(s), or the host could not be
            reached, refused the key, answered another status, more than
            ``_MAX_STATUS_BYTES`` or something other than JSON.
    """
    if urlsplit(url).scheme.lower() not in _DEFAULT_PORTS or _origin(url) is None:
        raise StatusUnavailable(f"{STATUS_URL_KEY} is not an http or https address.")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_STATUS_TIMEOUT_SECONDS),
                connector=aiohttp.TCPConnector(socket_factory=refuse_metadata_socket),
            ) as session,
            # A redirect would carry the key to wherever it points.
            session.get(url, headers=headers, allow_redirects=False) as response,
        ):
            body = await response.content.read(_MAX_STATUS_BYTES + 1)
            status = response.status
    except TimeoutError:
        raise StatusUnavailable(
            f"The queue status address did not answer within {_STATUS_TIMEOUT_SECONDS:.0f} s."
        ) from None
    except aiohttp.ClientError as exc:
        raise StatusUnavailable(f"The queue status address could not be reached: {exc}") from None
    if status in (401, 403):
        raise StatusUnavailable(
            f"The queue status address refused the key (HTTP {status}). Check "
            f"{STATUS_API_KEY_KEY}, or {FALLBACK_API_KEY_KEY} when it is empty."
        )
    if status != 200:
        raise StatusUnavailable(f"The queue status address answered HTTP {status}.")
    if len(body) > _MAX_STATUS_BYTES:
        raise StatusUnavailable(
            f"The queue status address answered more than {_MAX_STATUS_BYTES // 1024} KiB."
        )
    try:
        return json.loads(body)
    except ValueError:
        raise StatusUnavailable(
            "The queue status address answered something other than JSON."
        ) from None


def gateway_models_by_served_name(
    services: Any, status_url: str
) -> dict[str, tuple[set[str], set[str]]]:
    """Map each name the host serves to this gateway's models routed to it there.

    A route is the host's when its ``base_url`` has the status address's scheme,
    host and port, and it serves ``provider_model_id`` (or the model's own id).
    For each served name: the canonical model ids, to show, and every route key,
    aliases included, under which batch requests may be queued.
    """
    origin = _origin(status_url)
    found: dict[str, tuple[set[str], set[str]]] = defaultdict(lambda: (set(), set()))
    routes = getattr(getattr(services, "router", None), "routes", None) or {}
    for route_key, route in routes.items():
        for entry in getattr(route, "adapters", None) or ():
            adapter = entry[0] if isinstance(entry, tuple) else entry
            config = getattr(adapter, "config", None)
            base_url = getattr(config, "base_url", None)
            if config is None or not base_url or _origin(base_url) != origin:
                continue
            served = getattr(config, "provider_model_id", None) or config.id
            canonical, keys = found[served]
            canonical.add(config.id)
            keys.add(route_key)
    return dict(found)


@router.get("/batch/model-queue", response_model=ModelQueueResponse)
async def get_model_queue(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    batch_store=Depends(get_batch_store),
) -> ModelQueueResponse:
    """Return the on-demand host's queue, with the batch requests waiting per model.

    ``configured`` is false while ``MODEL_QUEUE_STATUS_URL`` is empty. When the
    host cannot be read, or answers something that is not a queue status,
    ``error`` says why and the rest is empty. Each model names the gateway
    models routed to it (``gateway_models``) and the batch requests of those
    models waiting and running (``batch_queued`` / ``batch_running``; null when
    the batch store cannot say).

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    url = _setting(STATUS_URL_KEY)
    if not url:
        return ModelQueueResponse(configured=False)
    api_key = _setting(STATUS_API_KEY_KEY) or _setting(FALLBACK_API_KEY_KEY)
    try:
        payload = await fetch_status(url, api_key)
    except StatusUnavailable as exc:
        return ModelQueueResponse(configured=True, error=str(exc))
    if not isinstance(payload, dict):
        return ModelQueueResponse(
            configured=True, error="The queue status address answered something other than a queue."
        )
    try:
        status = ModelQueueResponse.model_validate({**payload, "configured": True, "error": None})
    except ValidationError as exc:
        logger.warning("model queue: the status at %s does not parse: %s", url, exc)
        return ModelQueueResponse(
            configured=True,
            error="The queue status address answered a queue this gateway cannot read.",
        )

    routed = gateway_models_by_served_name(services, url)
    stats: dict[str, dict[str, int]] | None = None
    if batch_store is not None:
        try:
            stats = await batch_store.model_stats()
        except Exception as exc:  # the queue is still worth showing without counts
            logger.warning("model queue: could not count batch requests per model: %s", exc)
    for model in status.models:
        canonical, keys = routed.get(model.model, (set(), set()))
        # Ours, whatever the host sent under these names.
        model.gateway_models = sorted(canonical)
        model.batch_queued = model.batch_running = None
        if stats is not None:
            model.batch_queued = sum(int(stats.get(key, {}).get("queued", 0)) for key in keys)
            model.batch_running = sum(int(stats.get(key, {}).get("running", 0)) for key in keys)
    return status
