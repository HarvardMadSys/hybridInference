import type { ProbeRow, Snapshot } from "./db";

const STYLE = `
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f1115; color: #e6e6e6; }
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
`;

function esc(value: string): string {
  return value.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function fmt(value: number | null, suffix = ""): string {
  return value == null ? "—" : `${value}${suffix}`;
}

function spark(rows: ProbeRow[]): string {
  return rows
    .map((r) => {
      const cls = r.ok ? "up" : "down";
      const height = r.ok ? Math.max(15, Math.min(100, Math.round((r.latencyMs ?? 0) / 50))) : 100;
      return `<span class="${cls}" style="height:${height}%"></span>`;
    })
    .join("");
}

function card(model: Snapshot["models"][number]): string {
  const latest = model.latest;
  const ok = latest.ok;
  const err = !ok && latest.error ? `<div class="err">${esc(latest.error)}</div>` : "";
  const uptime = `${Math.round(model.uptimeRatio * 1000) / 10}%`;
  return `
    <div class="card">
      <div class="top">
        <span class="model">${esc(model.modelId)}</span>
        <span class="dot ${ok ? "up" : "down"}"></span>
      </div>
      <div class="metrics">
        <span class="k">Status</span><span class="v">${ok ? "UP" : "DOWN"}</span>
        <span class="k">Latency</span><span class="v">${fmt(latest.latencyMs && Math.round(latest.latencyMs), " ms")}</span>
        <span class="k">TTFT</span><span class="v">${fmt(latest.ttftMs && Math.round(latest.ttftMs), " ms")}</span>
        <span class="k">Throughput</span><span class="v">${fmt(latest.throughputTps, " tok/s")}</span>
        <span class="k">Uptime</span><span class="v">${uptime}</span>
        <span class="k">Checked</span><span class="v">${esc(latest.checkedAt).slice(0, 19)}</span>
      </div>
      <div class="spark">${spark(model.spark)}</div>
      ${err}
    </div>`;
}

/** Renders the full auto-refreshing status dashboard HTML page. */
export function renderDashboard(snapshot: Snapshot, refreshSeconds = 30): string {
  const cards = snapshot.models.length
    ? snapshot.models.map(card).join("")
    : '<p class="sub">No probe results yet — the first cron cycle is pending.</p>';
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="${refreshSeconds}">
  <title>FreeInference Model Status</title>
  <style>${STYLE}</style>
</head>
<body>
  <h1>FreeInference Model Status</h1>
  <div class="sub">Each model is probed with a synthetic request every 5 minutes (Cloudflare cron).</div>
  <div class="summary">
    <span class="pill ok">${snapshot.healthy} up</span>
    <span class="pill ${snapshot.unhealthy ? "bad" : "muted"}">${snapshot.unhealthy} down</span>
    <span class="pill muted">${snapshot.total} models</span>
  </div>
  <div class="grid">${cards}</div>
  <footer>Auto-refreshes every ${refreshSeconds}s · <a href="api/status">JSON</a></footer>
</body>
</html>`;
}
