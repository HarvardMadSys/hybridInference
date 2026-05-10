"""Track Qwen3.6 benchmark pipeline state.

Sentinel-file based resumability. Each pipeline step writes a small marker
file under `results/.state/` when it completes successfully. Re-running the
pipeline checks for the sentinel and skips the step if present.

Usage:
    store = StateStore(config.STATE_DIR)
    if not store.is_done("vllm_prefill_done"):
        run_vllm_prefill()
        store.mark_done("vllm_prefill_done")
"""
from pathlib import Path


class StateStore:
    """Filesystem-backed sentinel store. One file per step."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def is_done(self, step: str) -> bool:
        """Return True if `step` has been marked complete."""
        return (self._root / step).exists()

    def mark_done(self, step: str) -> None:
        """Mark `step` as complete by touching its sentinel file."""
        (self._root / step).touch()

    def clear(self, step: str) -> None:
        """Remove the sentinel for `step`. No-op if not present."""
        path = self._root / step
        if path.exists():
            path.unlink()
