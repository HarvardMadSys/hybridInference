"""Background probe scheduler.

Discovers probe targets from the registry (merged with manual overrides) and
probes them all concurrently on a fixed interval.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from status_monitor.prober import probe_model
from status_monitor.registry import load_models

if TYPE_CHECKING:
    from status_monitor.config import AppConfig
    from status_monitor.state import StatusStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeTarget:
    """A single model to probe, with its probe options."""

    model_id: str
    streaming: bool
    max_tokens: int | None
    kind: str = "chat"


def resolve_targets(config: AppConfig) -> list[ProbeTarget]:
    """Resolves the probe targets from the registry plus manual overrides.

    Models discovered from the registry are probed with default options. Manual
    ``e2e_models`` entries override discovered ones (matched by ``model_id``)
    and may add targets not present in the registry.

    Args:
        config: The application configuration.

    Returns:
        The ordered, de-duplicated list of probe targets.
    """
    overrides = {o.model_id: o for o in config.e2e_models}
    targets: list[ProbeTarget] = []
    seen: set[str] = set()

    discovered = load_models(config.registry.path) if config.registry.path else []
    for model in discovered:
        if model.model_id in seen:
            continue
        seen.add(model.model_id)
        override = overrides.get(model.model_id)
        # Embedding models are never streamed regardless of overrides.
        streaming = (override.streaming if override else True) and model.kind != "embedding"
        targets.append(
            ProbeTarget(
                model_id=model.model_id,
                streaming=streaming,
                max_tokens=override.probe_max_tokens if override else None,
                kind=model.kind,
            )
        )

    for model_id, override in overrides.items():
        if model_id in seen:
            continue
        seen.add(model_id)
        targets.append(
            ProbeTarget(
                model_id=model_id,
                streaming=override.streaming,
                max_tokens=override.probe_max_tokens,
                kind="chat",
            )
        )
    return targets


async def probe_once(config: AppConfig, store: StatusStore) -> None:
    """Probes every resolved target once and records the results."""
    targets = resolve_targets(config)
    if not targets:
        logger.warning("No probe targets resolved; check registry path and e2e_models.")
        return
    timeout = httpx.Timeout(
        connect=20.0,
        read=config.settings.default_timeout,
        write=20.0,
        pool=20.0,
    )
    semaphore = asyncio.Semaphore(config.settings.max_concurrency)
    async with httpx.AsyncClient(timeout=timeout) as client:

        async def probe_limited(target: ProbeTarget):  # noqa: ANN202 - returns ProbeResult
            async with semaphore:
                return await probe_model(
                    client,
                    gateway=config.gateway,
                    settings=config.settings,
                    model_id=target.model_id,
                    streaming=target.streaming,
                    max_tokens=target.max_tokens,
                    kind=target.kind,
                )

        results = await asyncio.gather(*(probe_limited(target) for target in targets))
    store.retain(target.model_id for target in targets)
    for result in results:
        store.record(result)
        logger.info(
            "probe model=%s ok=%s latency_ms=%s error=%s",
            result.model_id,
            result.ok,
            result.latency_ms,
            result.error,
        )
    # Persist off the event loop; the write touches the filesystem.
    await asyncio.to_thread(store.save)


async def run_scheduler(config: AppConfig, store: StatusStore) -> None:
    """Runs the probe loop forever, sleeping ``e2e_interval`` between cycles."""
    interval = config.gateway.e2e_interval
    logger.info("Starting probe scheduler (interval=%ss).", interval)
    while True:
        try:
            await probe_once(config, store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the loop die on a probe error
            logger.exception("Probe cycle failed; will retry next interval.")
        await asyncio.sleep(interval)
