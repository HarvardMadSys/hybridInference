"""In-memory status store with optional JSON persistence.

Holds the latest probe result for each model plus a bounded history, and can
load/save that state to disk so a restart does not lose recent history.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from status_monitor.prober import ProbeResult

logger = logging.getLogger(__name__)


class StatusStore:
    """Thread-safe store of the latest and historical probe results per model."""

    def __init__(self, *, history_size: int = 100, state_path: str | None = None) -> None:
        """Initializes the store.

        Args:
            history_size: Maximum number of historical results kept per model.
            state_path: Optional path used by :meth:`load` and :meth:`save`.
        """
        self._history_size = max(1, history_size)
        self._state_path = Path(state_path) if state_path else None
        self._latest: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._lock = Lock()

    def record(self, result: ProbeResult) -> None:
        """Records a probe result as the latest entry for its model."""
        entry = result.to_dict()
        with self._lock:
            self._latest[result.model_id] = entry
            history = self._history.setdefault(
                result.model_id, deque(maxlen=self._history_size)
            )
            history.append(entry)

    def snapshot(self) -> dict[str, Any]:
        """Returns a JSON-serializable snapshot of all model statuses."""
        with self._lock:
            models = []
            for model_id, latest in sorted(self._latest.items()):
                history = list(self._history.get(model_id, []))
                models.append(
                    {
                        "model_id": model_id,
                        "latest": latest,
                        "history": history,
                        "uptime_ratio": _uptime_ratio(history),
                    }
                )
            healthy = sum(1 for m in models if m["latest"].get("ok"))
            return {
                "models": models,
                "total": len(models),
                "healthy": healthy,
                "unhealthy": len(models) - healthy,
            }

    def load(self) -> None:
        """Loads persisted history from ``state_path`` if it exists."""
        if not self._state_path or not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load state from %s: %s", self._state_path, exc)
            return
        with self._lock:
            for model_id, data in (raw.get("models") or {}).items():
                history = deque(data.get("history", []), maxlen=self._history_size)
                self._history[model_id] = history
                if history:
                    self._latest[model_id] = history[-1]

    def save(self) -> None:
        """Persists current history to ``state_path`` (atomic write)."""
        if not self._state_path:
            return
        with self._lock:
            payload = {
                "models": {
                    model_id: {"history": list(history)}
                    for model_id, history in self._history.items()
                }
            }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.warning("Could not save state to %s: %s", self._state_path, exc)


def _uptime_ratio(history: list[dict[str, Any]]) -> float | None:
    """Computes the fraction of successful probes in ``history``."""
    if not history:
        return None
    ok = sum(1 for entry in history if entry.get("ok"))
    return round(ok / len(history), 4)
