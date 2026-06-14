import type { ProbeRow, Snapshot } from "./db";

// Probe cadence — must match the cron schedule in wrangler.toml (`*/5` = 5 min).
// Charts bucket each probe into its cron cycle by this interval to detect gaps.
const PROBE_INTERVAL_MS = 5 * 60 * 1000;

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
.card { background: #161a21; border: 1px solid #232a33; border-radius: 12px; padding: 1rem 1.1rem; cursor: pointer; transition: border-color .12s ease, transform .12s ease; }
.card:hover { border-color: #3b82f6; transform: translateY(-1px); }
.card:focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
.card .top { display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
.model { font-weight: 600; font-size: 1rem; word-break: break-all; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
.dot.up { background: #4ade80; } .dot.down { background: #f87171; }
.metrics { margin-top: .75rem; display: grid; grid-template-columns: 1fr 1fr; gap: .4rem .75rem; font-size: .82rem; }
.metrics .k { color: #9aa0a6; } .metrics .v { text-align: right; }
.spark { margin-top: .8rem; display: flex; gap: 2px; align-items: flex-end; height: 22px; }
.spark span { flex: 1; border-radius: 1px; min-width: 2px; }
.spark span.up { background: #2f6b46; } .spark span.down { background: #7a2b2b; }
.ttft { margin-top: .8rem; }
.ttft-head { display: flex; justify-content: space-between; font-size: .72rem; color: #9aa0a6; margin-bottom: .3rem; }
.ttft-svg { width: 100%; height: 32px; display: block; overflow: visible; }
.ttft-svg polyline { fill: none; stroke: #60a5fa; stroke-width: 1.4; vector-effect: non-scaling-stroke; }
.ttft-empty { margin-top: .8rem; font-size: .72rem; color: #6b7280; }
.zoom-cue { margin-top: .6rem; font-size: .68rem; color: #6b7280; text-align: right; }
.err { margin-top: .5rem; color: #f87171; font-size: .8rem; word-break: break-word; }
.banner { background: #3a1414; color: #fca5a5; border: 1px solid #7a2b2b; border-radius: 10px; padding: .75rem 1rem; margin-bottom: 1.5rem; font-size: .9rem; }
footer { margin-top: 2rem; color: #6b7280; font-size: .8rem; }
a { color: #60a5fa; }

/* Zoom-in detail overlay */
#zoom { position: fixed; inset: 0; background: rgba(2, 4, 8, .68); display: none; align-items: flex-start; justify-content: center; padding: 2.5rem 1rem; overflow: auto; z-index: 50; }
#zoom.open { display: flex; }
.zoom-card { background: #161a21; border: 1px solid #2a323d; border-radius: 14px; padding: 1.3rem 1.5rem 1.5rem; width: min(760px, 100%); box-shadow: 0 18px 60px rgba(0, 0, 0, .55); }
.zoom-head { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: .5rem; }
.zoom-title { font-size: 1.15rem; font-weight: 600; word-break: break-all; }
.zoom-close { background: #232a33; color: #e6e6e6; border: none; border-radius: 8px; width: 34px; height: 34px; flex: 0 0 auto; cursor: pointer; font-size: 1rem; line-height: 1; }
.zoom-close:hover { background: #2f3744; }
.zoom-stats { display: flex; flex-wrap: wrap; gap: .35rem 1.1rem; font-size: .82rem; color: #9aa0a6; margin-bottom: 1.1rem; }
.zoom-stats b { color: #e6e6e6; font-weight: 600; }
.chart-block { margin-bottom: 1.2rem; }
.chart-block h3 { margin: 0 0 .2rem; font-size: .85rem; font-weight: 600; }
.chart-block h3 .accent { font-weight: 400; color: #9aa0a6; font-size: .76rem; margin-left: .4rem; }
.chart-meta { display: flex; justify-content: space-between; font-size: .72rem; color: #9aa0a6; margin-bottom: .35rem; }
.chart { width: 100%; height: auto; display: block; background: #0f1115; border: 1px solid #1c2128; border-radius: 8px; }
.chart polyline { fill: none; stroke-width: 1.8; vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round; }
.chart .axg { stroke: #232a33; stroke-width: 1; vector-effect: non-scaling-stroke; }
.chart .axl { fill: #6b7280; font-size: 9px; }
.chart .axt { fill: #6b7280; font-size: 8px; }
.chart-empty { font-size: .78rem; color: #6b7280; padding: .5rem 0 .8rem; }
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

/** Inline SVG line chart of TTFT (ms) over the recent probe history. */
export function ttftSparkline(rows: ProbeRow[]): string {
  const n = rows.length;
  const pts: { i: number; ttft: number }[] = [];
  for (let i = 0; i < n; i++) {
    const t = rows[i].ttftMs;
    if (t != null) pts.push({ i, ttft: t });
  }
  if (pts.length < 2) {
    return '<div class="ttft-empty">TTFT trend — not enough data yet</div>';
  }
  const vals = pts.map((p) => p.ttft);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const W = 100;
  const H = 32;
  const PAD = 3;
  const xy = (p: { i: number; ttft: number }): [number, number] => [
    n > 1 ? (p.i / (n - 1)) * W : 0,
    H - PAD - ((p.ttft - min) / span) * (H - 2 * PAD),
  ];
  const coords = pts.map((p) => xy(p).map((v) => v.toFixed(1)).join(",")).join(" ");
  const latest = vals[vals.length - 1];
  return `
      <div class="ttft">
        <div class="ttft-head"><span>TTFT trend (${pts.length})</span><span>${latest} ms · ${min}–${max} ms</span></div>
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="ttft-svg" role="img" aria-label="TTFT over time, latest ${latest} ms (min ${min}, max ${max})">
          <polyline points="${coords}" />
        </svg>
      </div>`;
}

function card(model: Snapshot["models"][number]): string {
  const latest = model.latest;
  const ok = latest.ok;
  const err = !ok && latest.error ? `<div class="err">${esc(latest.error)}</div>` : "";
  const uptime = `${Math.round(model.uptimeRatio * 1000) / 10}%`;
  return `
    <div class="card" data-model="${esc(model.modelId)}" role="button" tabindex="0" aria-label="Show ${esc(model.modelId)} latency and throughput detail">
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
      ${ttftSparkline(model.history)}
      <div class="zoom-cue">click to zoom ↗</div>
      ${err}
    </div>`;
}

/** Compact per-model history embedded in the page for the client zoom view. */
export interface SeriesPoint {
  ok: number;
  t: string;
  latencyMs: number | null;
  ttftMs: number | null;
  throughputTps: number | null;
}

/** Maps each model to its retained history as a compact, JSON-friendly series. */
export function seriesPayload(models: Snapshot["models"]): Record<string, SeriesPoint[]> {
  // Prototype-safe map: a model literally named "__proto__" would otherwise hit
  // the prototype setter and be dropped from the serialized payload.
  const out: Record<string, SeriesPoint[]> = Object.create(null);
  for (const m of models) {
    out[m.modelId] = m.history.map((r) => ({
      ok: r.ok ? 1 : 0,
      t: r.checkedAt,
      latencyMs: r.latencyMs,
      ttftMs: r.ttftMs,
      throughputTps: r.throughputTps,
    }));
  }
  return out;
}

/**
 * Client script powering the zoom-in detail view: clicking a card opens an
 * overlay with full-size latency, throughput, and TTFT time-series charts built
 * from the embedded per-model history. Written with string concatenation (no
 * template literals) so it injects verbatim without `${}` collisions.
 */
function clientScript(refreshMs: number, cycleMs: number): string {
  return `
(function () {
  // Cron cadence in ms (probe interval). Charts bucket each probe into its cycle
  // by flooring its timestamp with this, to detect skipped cycles robustly.
  var CYCLE_MS = ${cycleMs};
  var data = {};
  var modelDataEl = document.getElementById("model-data");
  if (modelDataEl) {
    try { data = JSON.parse(modelDataEl.textContent); } catch (e) {}
  }
  var overlay = document.getElementById("zoom");
  var timer = null;
  var activeTrigger = null;
  function scheduleRefresh() { timer = setTimeout(function () { location.reload(); }, ${refreshMs}); }
  function cancelRefresh() { if (timer) { clearTimeout(timer); timer = null; } }
  scheduleRefresh();

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(s) { return s ? s.slice(0, 19).replace("T", " ") : ""; }
  function round(v, d) { var f = Math.pow(10, d || 0); return Math.round(v * f) / f; }

  function lineChart(rows, key, color, unit) {
    var W = 640, H = 170, PADL = 46, PADR = 14, PADT = 12, PADB = 24;
    var pts = [];
    for (var i = 0; i < rows.length; i++) {
      var v = rows[i][key];
      if (v != null && isFinite(v)) pts.push({ i: i, v: v, t: rows[i].t, ok: rows[i].ok });
    }
    if (pts.length < 1) return '<div class="chart-empty">No ' + unit + ' data captured for this model yet.</div>';
    var vals = pts.map(function (p) { return p.v; });
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    if (min === max) { min = min * 0.95; max = max * 1.05 + 1; }
    var n = rows.length;
    function x(i) { return PADL + (n > 1 ? i / (n - 1) : 0) * (W - PADL - PADR); }
    function y(v) { return PADT + (1 - (v - min) / (max - min)) * (H - PADT - PADB); }

    // Bucket each probe into its cron cycle: floor(checkedAt / CYCLE_MS). Because
    // checkedAt is the per-model probe-start (which drifts within a cycle as pool
    // latency varies), raw time deltas are unreliable; the cycle index is not —
    // any within-cycle offset (< one interval) floors to the same cron tick. A
    // skipped cron cycle shows up as a cycle-index jump of 2+.
    var cyc = rows.map(function (r) {
      var t = Date.parse(r.t);
      return isFinite(t) ? Math.floor(t / CYCLE_MS) : null;
    });

    // Break the line where the series skips plotted points (gap in row index) or
    // skips a whole cron cycle (cycle index jumps by more than one).
    var segs = [], cur = [], prev = null, prevC = null;
    for (var k = 0; k < pts.length; k++) {
      var p = pts[k];
      var c = cyc[p.i];
      var jumped = prev != null && (p.i !== prev + 1 || (prevC != null && c != null && c - prevC > 1));
      if (jumped) { if (cur.length) segs.push(cur); cur = []; }
      cur.push(p); prev = p.i; prevC = c != null ? c : prevC;
    }
    if (cur.length) segs.push(cur);

    var polys = segs.map(function (s) {
      return '<polyline points="' + s.map(function (p) { return x(p.i).toFixed(1) + "," + y(p.v).toFixed(1); }).join(" ") + '" style="stroke:' + color + '"/>';
    }).join("");
    var dots = pts.map(function (p) {
      return '<circle cx="' + x(p.i).toFixed(1) + '" cy="' + y(p.v).toFixed(1) + '" r="2.4" style="fill:' + (p.ok ? color : "#f87171") + '"><title>' + fmtTime(p.t) + " \\u2014 " + round(p.v, 2) + " " + unit + "</title></circle>";
    }).join("");
    var grid = '<line x1="' + PADL + '" y1="' + y(max).toFixed(1) + '" x2="' + (W - PADR) + '" y2="' + y(max).toFixed(1) + '" class="axg"/>' +
               '<line x1="' + PADL + '" y1="' + y(min).toFixed(1) + '" x2="' + (W - PADR) + '" y2="' + y(min).toFixed(1) + '" class="axg"/>';
    var ylab = '<text x="6" y="' + (y(max) + 3).toFixed(1) + '" class="axl">' + round(max, 0) + '</text>' +
               '<text x="6" y="' + (y(min) + 3).toFixed(1) + '" class="axl">' + round(min, 0) + '</text>';
    var xlab = '<text x="' + PADL + '" y="' + (H - 6) + '" class="axt">' + fmtTime(pts[0].t) + '</text>' +
               '<text x="' + (W - PADR) + '" y="' + (H - 6) + '" class="axt" text-anchor="end">' + fmtTime(pts[pts.length - 1].t) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" class="chart" role="img" aria-label="' + unit + ' over time">' + grid + polys + dots + ylab + xlab + '</svg>';
  }

  function chartBlock(label, rows, key, color, unit) {
    var present = rows.filter(function (r) { return r[key] != null && isFinite(r[key]); }).map(function (r) { return r[key]; });
    var meta = "";
    if (present.length) {
      var mn = Math.min.apply(null, present), mx = Math.max.apply(null, present);
      var avg = present.reduce(function (a, b) { return a + b; }, 0) / present.length;
      // "latest" reflects the most recent sample, not the last non-null value —
      // a failed probe shows "—" rather than a stale earlier reading.
      var lastRaw = rows.length ? rows[rows.length - 1][key] : null;
      var lastStr = lastRaw != null && isFinite(lastRaw) ? round(lastRaw, 2) + " " + unit : "\\u2014";
      meta = '<div class="chart-meta"><span>latest ' + lastStr +
        '</span><span>min ' + round(mn, 1) + " \\u00B7 avg " + round(avg, 1) + " \\u00B7 max " + round(mx, 1) + " " + unit + "</span></div>";
    }
    return '<div class="chart-block"><h3>' + escHtml(label) + '<span class="accent">' + unit + '</span></h3>' + meta + lineChart(rows, key, color, unit) + "</div>";
  }

  function openZoom(modelId, trigger) {
    var rows = data[modelId];
    if (!rows) return;
    activeTrigger = trigger;
    var latest = rows.length ? rows[rows.length - 1] : null;
    var okCount = 0;
    for (var i = 0; i < rows.length; i++) if (rows[i].ok) okCount++;
    var uptime = rows.length ? round((okCount / rows.length) * 100, 1) : 0;
    var stats = '<div class="zoom-stats">' +
      "<span>Status <b>" + (latest && latest.ok ? "UP" : "DOWN") + "</b></span>" +
      "<span>Latency <b>" + (latest && latest.latencyMs != null ? round(latest.latencyMs, 0) + " ms" : "\\u2014") + "</b></span>" +
      "<span>TTFT <b>" + (latest && latest.ttftMs != null ? round(latest.ttftMs, 0) + " ms" : "\\u2014") + "</b></span>" +
      "<span>Throughput <b>" + (latest && latest.throughputTps != null ? round(latest.throughputTps, 1) + " tok/s" : "\\u2014") + "</b></span>" +
      "<span>Uptime <b>" + uptime + "%</b></span>" +
      "<span>Samples <b>" + rows.length + "</b></span></div>";
    // Single source of truth for which metrics get a detail chart.
    var charts = [
      ["Latency", "latencyMs", "#60a5fa", "ms"],
      ["Throughput", "throughputTps", "#4ade80", "tok/s"],
      ["Time to first token", "ttftMs", "#fbbf24", "ms"]
    ].map(function (m) { return chartBlock(m[0], rows, m[1], m[2], m[3]); }).join("");
    overlay.innerHTML = '<div class="zoom-card" role="dialog" aria-modal="true" aria-label="' + escHtml(modelId) + ' detail">' +
      '<div class="zoom-head"><span class="zoom-title">' + escHtml(modelId) + '</span>' +
      '<button class="zoom-close" aria-label="Close detail view">\\u2715</button></div>' +
      stats +
      charts +
      "</div>";
    overlay.classList.add("open");
    cancelRefresh();
    var btn = overlay.querySelector(".zoom-close");
    if (btn) { btn.addEventListener("click", closeZoom); btn.focus(); }
  }

  function closeZoom() {
    overlay.classList.remove("open");
    overlay.innerHTML = "";
    cancelRefresh();
    scheduleRefresh();
    // Restore focus to the card that opened the overlay (WCAG keyboard nav).
    if (activeTrigger) {
      activeTrigger.focus();
      activeTrigger = null;
    }
  }

  var grid = document.querySelector(".grid");
  if (grid) {
    grid.addEventListener("click", function (e) {
      var card = e.target.closest(".card");
      if (card && card.dataset.model != null) openZoom(card.dataset.model, card);
    });
    grid.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      var card = e.target.closest(".card");
      if (card && card.dataset.model != null) { e.preventDefault(); openZoom(card.dataset.model, card); }
    });
  }
  overlay.addEventListener("click", function (e) { if (e.target === overlay) closeZoom(); });
  document.addEventListener("keydown", function (e) {
    if (!overlay.classList.contains("open")) return;
    if (e.key === "Escape") { closeZoom(); return; }
    if (e.key !== "Tab") return;
    // Trap focus inside the modal dialog (aria-modal) until it closes.
    var f = overlay.querySelectorAll('button, a[href], [tabindex]:not([tabindex="-1"])');
    if (!f.length) { e.preventDefault(); return; }
    var first = f[0], last = f[f.length - 1], active = document.activeElement;
    if (e.shiftKey && (active === first || !overlay.contains(active))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !overlay.contains(active))) { e.preventDefault(); first.focus(); }
  });
})();
`;
}

/** Renders the full auto-refreshing status dashboard HTML page. */
export function renderDashboard(
  snapshot: Snapshot,
  gatewayHost?: string,
  refreshSeconds = 30,
): string {
  const target = gatewayHost ? ` · monitoring <strong>${esc(gatewayHost)}</strong>` : "";
  const cards = snapshot.models.length
    ? snapshot.models.map(card).join("")
    : '<p class="sub">No probe results yet — the first cron cycle is pending.</p>';
  const banner = snapshot.cycle.ok
    ? ""
    : `<div class="banner">⚠ Last probe cycle failed${
        snapshot.cycle.error ? `: ${esc(snapshot.cycle.error)}` : ""
      }${
        snapshot.cycle.checkedAt ? ` (${esc(snapshot.cycle.checkedAt).slice(0, 19)})` : ""
      }. Results below may be stale.</div>`;
  // Embed each model's history so the client can render zoom charts without an
  // extra round-trip. Neutralize "</" so the JSON can't break out of <script>.
  const dataJson = JSON.stringify(seriesPayload(snapshot.models)).replace(/</g, "\\u003c");
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <noscript><meta http-equiv="refresh" content="${refreshSeconds}"></noscript>
  <title>FreeInference Model Status</title>
  <style>${STYLE}</style>
</head>
<body>
  <h1>FreeInference Model Status</h1>
  <div class="sub">Each model is probed with a synthetic request every 5 minutes (Cloudflare cron)${target}. Click a model to zoom in on its latency and throughput history.</div>
  ${banner}
  <div class="summary">
    <span class="pill ok">${snapshot.healthy} up</span>
    <span class="pill ${snapshot.unhealthy ? "bad" : "muted"}">${snapshot.unhealthy} down</span>
    <span class="pill muted">${snapshot.total} models</span>
  </div>
  <div class="grid">${cards}</div>
  <div id="zoom" role="presentation"></div>
  <footer>Auto-refreshes every ${refreshSeconds}s · <a href="api/status">JSON</a></footer>
  <script id="model-data" type="application/json">${dataJson}</script>
  <script>${clientScript(refreshSeconds * 1000, PROBE_INTERVAL_MS)}</script>
</body>
</html>`;
}
