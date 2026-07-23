"""Enable ``python -m ops.db.analysis.trace_viewer <trace>``."""

from __future__ import annotations

from ops.db.analysis.trace_viewer.server import main

if __name__ == "__main__":
    raise SystemExit(main())
