"""Server-side rendering of the status dashboard HTML page."""

from __future__ import annotations

import html
from typing import Any

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #0f1115; color: #e6e6e6;
}
h1 { margin: 0 0 .25rem; font-size: 1.5rem; }
.sub { color: #9aa0a6; margin-bottom: 1.5rem; font-size: .9rem; }
.summary { display: flex; gap: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
.pill { padding: .4rem .9rem; border-radius: 999px; font-size: .85rem; font-weight: 600; }
.pill.ok { background: #10331f; color: #4ade80; }
.pill.bad { background: #3a1414; color: #f87171; }
.pill.muted { background: #1c2128; color: #9aa0a6; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
.card { background: #161a21; border: 1px solid #232a33; border-radius: 12px; padding: 1rem 1.1rem; }
.card .top { display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
.model { font-weight: 600; font-size: 1rem; word-break: break-all; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
.dot.up { background: #4ade80; } .dot.down { background: #f87171; }
.metrics { margin-top: .75rem; display: grid; grid-template-columns: 1fr 1fr; gap: .4rem .75rem; font-size: .82rem; }
.metrics .k { color: #9aa0a6; } .metrics .v { text-align: right; }
.spark { margin-top: .8rem; display: flex; gap: 2px; align-items: flex-end; height: 22px; }
.spark span { flex: 1; border-radius: 1px; min-width: 2px; }
.spark span.up { background: #2f6b46; } .spark span.down { background: #7a2b2b; }
.err { margin-top: .5rem; color: #f87171; font-size: .8rem; word-break: break-word; }
footer { margin-top: 2rem; color: #6b7280; font-size: .8rem; }
a { color: #60a5fa; }
"""


def _fmt(value: Any, suffix: str = "") -> str:
    """Formats an optional numeric metric for display."""
    if value is None:
        return "—"
    return f"{value}{suffix}"


def _spark(history: list[dict[str, Any]]) -> str:
    """Renders a small bar strip from the recent probe history."""
    recent = history[-30:]
    bars = []
    for entry in recent:
        cls = "up" if entry.get("ok") else "down"
        latency = entry.get("latency_ms") or 0
        height = max(15, min(100, int(latency / 50))) if entry.get("ok") else 100
        bars.append(f'<span class="{cls}" style="height:{height}%"></span>')
    return "".join(bars)


def _card(model: dict[str, Any]) -> str:
    """Renders one model status card."""
    latest = model.get("latest") or {}
    ok = bool(latest.get("ok"))
    name = html.escape(model["model_id"])
    dot = "up" if ok else "down"
    uptime = model.get("uptime_ratio")
    uptime_str = f"{round(uptime * 100, 1)}%" if uptime is not None else "—"
    err = ""
    if not ok and latest.get("error"):
        err = f'<div class="err">{html.escape(str(latest["error"]))}</div>'
    return f"""
    <div class="card">
      <div class="top">
        <span class="model">{name}</span>
        <span class="dot {dot}"></span>
      </div>
      <div class="metrics">
        <span class="k">Status</span><span class="v">{"UP" if ok else "DOWN"}</span>
        <span class="k">Latency</span><span class="v">{_fmt(latest.get("latency_ms"), " ms")}</span>
        <span class="k">TTFT</span><span class="v">{_fmt(latest.get("ttft_ms"), " ms")}</span>
        <span class="k">Throughput</span><span class="v">{_fmt(latest.get("throughput_tps"), " tok/s")}</span>
        <span class="k">Uptime</span><span class="v">{uptime_str}</span>
        <span class="k">Checked</span><span class="v">{html.escape(str(latest.get("checked_at", "—")))[:19]}</span>
      </div>
      <div class="spark">{_spark(model.get("history", []))}</div>
      {err}
    </div>
    """


def render_dashboard(snapshot: dict[str, Any], *, refresh_seconds: int = 30) -> str:
    """Renders the full status dashboard HTML page.

    Args:
        snapshot: The store snapshot (see :meth:`StatusStore.snapshot`).
        refresh_seconds: Auto-refresh interval for the page.

    Returns:
        A complete HTML document as a string.
    """
    models = snapshot.get("models", [])
    healthy = snapshot.get("healthy", 0)
    unhealthy = snapshot.get("unhealthy", 0)
    total = snapshot.get("total", 0)
    if models:
        cards = "".join(_card(model) for model in models)
    else:
        cards = '<p class="sub">No probe results yet — the first cycle is in progress.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>FreeInference Model Status</title>
  <style>{_STYLE}</style>
</head>
<body>
  <h1>FreeInference Model Status</h1>
  <div class="sub">Each model is probed with a synthetic request on a schedule.</div>
  <div class="summary">
    <span class="pill ok">{healthy} up</span>
    <span class="pill {"bad" if unhealthy else "muted"}">{unhealthy} down</span>
    <span class="pill muted">{total} models</span>
  </div>
  <div class="grid">{cards}</div>
  <footer>Auto-refreshes every {refresh_seconds}s · <a href="api/status">JSON</a></footer>
</body>
</html>"""
