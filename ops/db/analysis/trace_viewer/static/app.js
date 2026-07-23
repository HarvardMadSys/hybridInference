"use strict";
/* Vanilla single-page UI for the api_logs trace viewer. No external deps. */

// ---------- tiny helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

const COLORS = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6", "--c7", "--c8"];
const cvar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const fmtInt = (n) => (n == null ? "—" : Math.round(n).toLocaleString());
function fmtNum(n) {
  if (n == null) return "—";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}
const fmtCost = (n) => (n == null ? "—" : "$" + Number(n).toFixed(4));
function fmtMs(n) {
  if (n == null) return "—";
  return n >= 1000 ? (n / 1000).toFixed(2) + " s" : Math.round(n) + " ms";
}
function fmtTs(epoch) {
  if (epoch == null) return "—";
  return new Date(epoch * 1000).toISOString().replace("T", " ").slice(0, 19);
}
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

// ---------- global state ----------
const state = {
  view: "overview",
  filters: {},
  req: { sort: "ts", order: "desc", page: 1, page_size: 50 },
  sess: { sort: "start_ts", order: "desc", page: 1, page_size: 50, min: 2 },
  detailRecord: null,
  detailRaw: false,
};

function readFilters() {
  const f = {};
  const q = $("#f-q").value.trim();
  if (q) f.q = q;
  for (const [id, key] of [["#f-model", "model_id"], ["#f-provider", "provider"], ["#f-status", "status_code"]]) {
    const v = $(id).value;
    if (v) f[key] = v;
  }
  const user = $("#f-user").value.trim();
  if (user) f.user_id = user;
  const start = $("#f-start").value.trim();
  if (start) f.start = start;
  const end = $("#f-end").value.trim();
  if (end) f.end = end;
  if ($("#f-errors").checked) f.errors_only = "true";
  return f;
}

function qs(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v != null && v !== "") p.set(k, v);
  return p.toString();
}

// ---------- charts (SVG built as strings) ----------
function columnChart(points, { x, y, title, color = "--c1", errKey = null }, W = 760) {
  const H = 200, PL = 44, PR = 10, PT = 12, PB = 26;
  const iw = W - PL - PR, ih = H - PT - PB;
  if (!points.length) return `<div class="empty dim">no data</div>`;
  const yMax = Math.max(1, ...points.map(y));
  const bw = iw / points.length;
  const yTicks = 4;
  const col = cvar(color), bad = cvar("--bad"), border = cvar("--border");
  let s = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">`;
  for (let t = 0; t <= yTicks; t++) {
    const yy = PT + ih - (ih * t) / yTicks;
    const val = (yMax * t) / yTicks;
    s += `<line class="axis" x1="${PL}" y1="${yy}" x2="${W - PR}" y2="${yy}" stroke-width="1" opacity="0.4"/>`;
    s += `<text x="${PL - 6}" y="${yy + 3}" text-anchor="end">${esc(fmtNum(val))}</text>`;
  }
  points.forEach((p, i) => {
    const v = y(p), e = errKey ? errKey(p) : 0;
    const bh = (v / yMax) * ih;
    const eh = (e / yMax) * ih;
    const px = PL + i * bw;
    const bwidth = Math.max(1, bw - 1);
    s += `<rect x="${px.toFixed(1)}" y="${(PT + ih - bh).toFixed(1)}" width="${bwidth.toFixed(1)}" height="${bh.toFixed(1)}" fill="${col}">`;
    s += `<title>${esc(title(p))}</title></rect>`;
    if (eh > 0) s += `<rect x="${px.toFixed(1)}" y="${(PT + ih - eh).toFixed(1)}" width="${bwidth.toFixed(1)}" height="${eh.toFixed(1)}" fill="${bad}"><title>${esc(title(p))}</title></rect>`;
  });
  // x labels: first / middle / last
  const idxs = points.length > 1 ? [0, Math.floor(points.length / 2), points.length - 1] : [0];
  idxs.forEach((i) => {
    const px = PL + i * bw + bw / 2;
    const anchor = i === 0 ? "start" : i === points.length - 1 ? "end" : "middle";
    s += `<text x="${px.toFixed(1)}" y="${H - 8}" text-anchor="${anchor}">${esc(x(points[i]))}</text>`;
  });
  s += `<line class="axis" x1="${PL}" y1="${PT + ih}" x2="${W - PR}" y2="${PT + ih}" stroke="${border}"/>`;
  s += `</svg>`;
  return s;
}

function histogramChart(hist, color = "--c1", W = 760) {
  if (!hist || !hist.count) return `<div class="empty dim">no data</div>`;
  const pts = hist.bins.map((b) => ({ ...b }));
  return columnChart(pts, {
    color,
    y: (b) => b.count,
    x: (b) => fmtNum(b.lo),
    title: (b) => `${fmtNum(b.lo)} – ${fmtNum(b.hi)}${b.overflow ? "+" : ""}: ${fmtInt(b.count)}`,
  }, W);
}

const cw = (el) => Math.max(320, Math.floor((el && el.getBoundingClientRect().width) || 700));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function breakdown(container, items, { value = "count", color = "--c1", fmt = fmtInt } = {}) {
  if (!items || !items.length) { container.innerHTML = `<div class="empty dim">no data</div>`; return; }
  const max = Math.max(1, ...items.map((d) => d[value]));
  container.innerHTML = items
    .map((d, i) => {
      const w = ((d[value] / max) * 100).toFixed(1);
      const c = cvar(COLORS[i % COLORS.length]);
      const errNote = d.errors ? ` · <span style="color:var(--bad)">${fmtInt(d.errors)} err</span>` : "";
      return `<div class="brk">
        <span class="brk-label" title="${esc(d.key)}">${esc(d.key)}</span>
        <span class="brk-bar"><i style="width:${w}%;background:${c}"></i></span>
        <span class="brk-val">${esc(fmt(d[value]))}${errNote}</span></div>`;
    })
    .join("");
}

// ---------- overview ----------
async function loadOverview() {
  const summary = await getJSON("/api/summary?" + qs(state.filters));
  state.lastSummary = summary;
  renderOverview(summary);
}

function renderOverview(summary) {
  const k = summary.kpis;
  const tiles = [
    { label: "Requests", value: fmtInt(k.requests), sub: fmtTs(k.start_ts) + " → " + fmtTs(k.end_ts) },
    { label: "Users", value: fmtInt(k.users) },
    { label: "Errors", value: fmtInt(k.errors), sub: (k.error_rate * 100).toFixed(1) + "%", bad: k.errors > 0 },
    { label: "Cost", value: fmtCost(k.cost_usd), sub: "upstream " + fmtCost(k.upstream_cost_usd) },
    { label: "Tokens", value: fmtNum(k.total_tokens), sub: `${fmtNum(k.prompt_tokens)} in / ${fmtNum(k.completion_tokens)} out` },
    { label: "Latency p50 / p95", value: fmtMs(k.latency_p50), sub: "p95 " + fmtMs(k.latency_p95) + " · p99 " + fmtMs(k.latency_p99) },
    { label: "TTFT p50 / p95", value: fmtMs(k.ttft_p50), sub: "p95 " + fmtMs(k.ttft_p95) },
  ];
  $("#kpis").innerHTML = tiles
    .map((t) => `<div class="kpi ${t.bad ? "bad" : ""}"><div class="label">${esc(t.label)}</div>
      <div class="value">${esc(t.value)}</div><div class="sub">${esc(t.sub || "")}</div></div>`)
    .join("");

  const ts = summary.timeseries;
  $("#ts-legend").innerHTML = ts.points.length
    ? `${ts.points.length} buckets · ${fmtDur(ts.bucket_seconds)} each · <span style="color:var(--bad)">■</span> errors`
    : "";
  $("#chart-timeseries").innerHTML = columnChart(ts.points, {
    y: (p) => p.count,
    errKey: (p) => p.errors,
    x: (p) => fmtTs(p.ts),
    title: (p) => `${fmtTs(p.ts)}\n${fmtInt(p.count)} req · ${fmtInt(p.errors)} err\n${fmtNum(p.prompt_tokens + p.completion_tokens)} tok · ${fmtCost(p.cost)}`,
  }, cw($("#chart-timeseries")));

  breakdown($("#chart-model"), summary.by_model);
  breakdown($("#chart-provider"), summary.by_provider);
  breakdown($("#chart-status"), summary.by_status);
  breakdown($("#chart-users"), summary.top_users, { fmt: fmtInt });

  const h = summary;
  $("#lat-stats").textContent = h.latency_hist.count ? `p50 ${fmtMs(h.latency_hist.p50)} · p95 ${fmtMs(h.latency_hist.p95)} · p99 ${fmtMs(h.latency_hist.p99)}` : "";
  $("#ttft-stats").textContent = h.ttft_hist.count ? `p50 ${fmtMs(h.ttft_hist.p50)} · p95 ${fmtMs(h.ttft_hist.p95)}` : "";
  $("#chart-latency").innerHTML = histogramChart(h.latency_hist, "--c1", cw($("#chart-latency")));
  $("#chart-ttft").innerHTML = histogramChart(h.ttft_hist, "--c6", cw($("#chart-ttft")));
  $("#chart-ptok").innerHTML = histogramChart(h.prompt_tokens_hist, "--c3", cw($("#chart-ptok")));
  $("#chart-ctok").innerHTML = histogramChart(h.completion_tokens_hist, "--c2", cw($("#chart-ctok")));
}

// ---------- requests ----------
async function loadRequests() {
  const p = { ...state.filters, sort: state.req.sort, order: state.req.order, page: state.req.page, page_size: state.req.page_size };
  const data = await getJSON("/api/requests?" + qs(p));
  const body = $("#req-body");
  body.innerHTML = data.rows
    .map((r) => {
      const st = r.has_error
        ? `<span class="pill err">${esc(r.status_code ?? "err")}</span>`
        : `<span class="pill ok">${esc(r.status_code ?? "—")}</span>`;
      return `<tr data-row="${r.row}">
        <td class="mono">${esc(fmtTs(r.ts))}</td>
        <td>${esc(r.model_id ?? "—")}</td>
        <td class="dim">${esc(r.provider ?? "—")}</td>
        <td>${st}</td>
        <td class="num">${esc(fmtMs(r.latency_ms))}</td>
        <td class="num">${esc(fmtMs(r.ttft_ms))}</td>
        <td class="num">${fmtInt(r.prompt_tokens)}</td>
        <td class="num">${fmtInt(r.completion_tokens)}</td>
        <td class="num">${esc(fmtCost(r.cost_usd))}</td>
        <td class="dim">${esc(r.user_id ?? "—")}</td>
        <td class="num">${r.num_tool_calls ? fmtInt(r.num_tool_calls) : ""}</td>
      </tr>`;
    })
    .join("") || `<tr><td colspan="11" class="empty dim">no matching requests</td></tr>`;
  $$("#req-body tr[data-row]").forEach((tr) =>
    tr.addEventListener("click", () => openRequestDetail(Number(tr.dataset.row))));
  markSort("#req-table", state.req);
  renderPager("#req-pager", data, (pg) => { state.req.page = pg; loadRequests(); });
}

// ---------- sessions ----------
async function loadSessions() {
  const p = {
    sort: state.sess.sort, order: state.sess.order, page: state.sess.page,
    page_size: state.sess.page_size, min_requests: state.sess.min,
    user_id: state.filters.user_id, model_id: state.filters.model_id,
  };
  const data = await getJSON("/api/sessions?" + qs(p));
  $("#sess-count").textContent = `${fmtInt(data.total)} sessions`;
  const body = $("#sess-body");
  body.innerHTML = data.rows
    .map((s) => {
      const src = `<span class="pill ${s.source === "session_id" ? "tag" : "tag"}">${s.source === "session_id" ? "session_id" : "inferred"}</span>`;
      const models = s.models.slice(0, 3).map((m) => `<span class="pill tag">${esc(m)}</span>`).join(" ") + (s.models.length > 3 ? ` +${s.models.length - 3}` : "");
      return `<tr data-sid="${esc(s.sid)}">
        <td class="mono">${esc(fmtTs(s.start_ts))}</td>
        <td class="dim">${esc(s.user_id ?? "—")}</td>
        <td class="num">${fmtInt(s.n_requests)}${s.errors ? ` <span style="color:var(--bad)">(${s.errors})</span>` : ""}</td>
        <td class="num">${esc(fmtDur(s.duration_s))}</td>
        <td>${models}</td>
        <td class="num">${fmtNum(s.total_tokens)}</td>
        <td class="num">${esc(fmtCost(s.total_cost))}</td>
        <td class="num">${s.n_tool_calls ? fmtInt(s.n_tool_calls) : ""}</td>
        <td>${src}</td>
      </tr>`;
    })
    .join("") || `<tr><td colspan="9" class="empty dim">no sessions</td></tr>`;
  $$("#sess-body tr[data-sid]").forEach((tr) =>
    tr.addEventListener("click", () => openSessionDetail(tr.dataset.sid)));
  markSort("#sess-table", state.sess);
  renderPager("#sess-pager", data, (pg) => { state.sess.page = pg; loadSessions(); });
}

function markSort(tableSel, cfg) {
  $$(`${tableSel} thead th`).forEach((th) => {
    th.classList.remove("sorted", "asc");
    if (th.dataset.sort === cfg.sort) {
      th.classList.add("sorted");
      if (cfg.order === "asc") th.classList.add("asc");
    }
  });
}

function renderPager(sel, data, onPage) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const box = $(sel);
  box.innerHTML = "";
  const mk = (label, pg, disabled) => {
    const b = document.createElement("button");
    b.className = "btn"; b.textContent = label; b.disabled = disabled;
    if (!disabled) b.addEventListener("click", () => onPage(pg));
    return b;
  };
  box.appendChild(mk("‹ prev", data.page - 1, data.page <= 1));
  const info = document.createElement("span");
  info.textContent = `page ${data.page} / ${pages} · ${fmtInt(data.total)} rows`;
  box.appendChild(info);
  box.appendChild(mk("next ›", data.page + 1, data.page >= pages));
}

// ---------- request detail drawer ----------
function renderContent(content) {
  if (content == null) return "";
  if (typeof content === "string") return esc(content);
  if (Array.isArray(content)) {
    return content
      .map((part) => (part && part.type === "text" ? esc(part.text || "") : esc(JSON.stringify(part, null, 2))))
      .join("\n");
  }
  return esc(JSON.stringify(content, null, 2));
}

function renderToolCalls(tcs) {
  return (tcs || [])
    .map((tc) => {
      const fn = tc.function || {};
      let args = fn.arguments;
      try { args = JSON.stringify(typeof args === "string" ? JSON.parse(args) : args, null, 2); }
      catch (e) { /* leave raw */ }
      return `<div class="toolcall"><div class="tc-name">→ ${esc(fn.name || "?")}</div><div class="msg-body">${esc(args)}</div></div>`;
    })
    .join("");
}

function renderMessage(m) {
  const role = m.role || "?";
  const cls = ["system", "user", "assistant", "tool"].includes(role) ? `role-${role}` : "";
  const head = m.name ? `${role} · ${m.name}` : role;
  let inner = renderContent(m.content);
  if (m.tool_calls) inner += renderToolCalls(m.tool_calls);
  return `<div class="msg ${cls}"><div class="msg-head">${esc(head)}</div><div class="msg-body">${inner}</div></div>`;
}

function renderConversation(msgs) {
  if (!Array.isArray(msgs)) return `<pre class="raw">${esc(JSON.stringify(msgs, null, 2))}</pre>`;
  return msgs.map(renderMessage).join("");
}

function renderResponse(resp) {
  if (!resp || typeof resp !== "object") return `<div class="dim">—</div>`;
  const choices = resp.choices || [];
  let html = choices
    .map((c) => {
      const m = c.message || {};
      let inner = renderContent(m.content);
      if (m.tool_calls) inner += renderToolCalls(m.tool_calls);
      return `<div class="msg role-assistant"><div class="msg-head">assistant · finish: ${esc(c.finish_reason || "?")}</div><div class="msg-body">${inner || "<span class='dim'>(empty)</span>"}</div></div>`;
    })
    .join("");
  if (!choices.length) html = `<pre class="raw">${esc(JSON.stringify(resp, null, 2))}</pre>`;
  return html;
}

function detailGrid(rec) {
  const L = rec._light || {};
  const fields = [
    ["model", L.model_id], ["served", rec.served_model_id || L.served_model_id],
    ["provider", L.provider], ["endpoint", rec.served_endpoint_id],
    ["status", L.status_code], ["latency", fmtMs(L.latency_ms)], ["ttft", fmtMs(L.ttft_ms)],
    ["prompt tok", fmtInt(L.prompt_tokens)], ["compl tok", fmtInt(L.completion_tokens)],
    ["reasoning tok", fmtInt(L.reasoning_tokens)], ["cache r/w", `${fmtInt(L.cache_read_tokens)}/${fmtInt(L.cache_write_tokens)}`],
    ["cost", fmtCost(L.cost_usd)], ["upstream", fmtCost(L.upstream_cost_usd)],
    ["user", L.user_id], ["session", L.session_id], ["agent", L.agent],
    ["stream", String(rec.stream)], ["turns", L.num_turns], ["tool calls", L.num_tool_calls],
    ["time", fmtTs(L.ts)],
  ];
  return `<div class="detail-grid">${fields
    .map(([k, v]) => `<div><div class="k">${esc(k)}</div><div class="v">${esc(v ?? "—")}</div></div>`)
    .join("")}</div>`;
}

function renderDetailBody(rec) {
  if (state.detailRaw) {
    const clone = { ...rec };
    delete clone._light;
    return `<pre class="raw">${esc(JSON.stringify(clone, null, 2))}</pre>`;
  }
  let html = detailGrid(rec);
  if (rec._light && rec._light.error) html += `<div class="msg role-tool"><div class="msg-head">error</div><div class="msg-body">${esc(rec._light.error)}</div></div>`;
  html += `<div class="section-h">Prompt</div>` + renderConversation(rec.prompt_parsed);
  html += `<div class="section-h">Response</div>` + renderResponse(rec.response_parsed);
  const tools = rec.tools_parsed || [];
  if (Array.isArray(tools) && tools.length) {
    const names = tools.map((t) => (t.function && t.function.name) || t.name || "?");
    html += `<div class="section-h">Tools offered (${names.length})</div><div class="msg-body">${esc(names.join(", "))}</div>`;
  }
  const meta = rec.metadata_parsed;
  if (meta && Object.keys(meta).length) html += `<div class="section-h">Metadata</div><pre class="raw">${esc(JSON.stringify(meta, null, 2))}</pre>`;
  return html;
}

async function openRequestDetail(row) {
  openDrawer();
  $("#drawer-body").innerHTML = `<div class="empty dim">loading…</div>`;
  try {
    const rec = await getJSON(`/api/requests/${row}`);
    state.detailRecord = rec; state.detailRaw = false;
    const idLabel = rec.request_id || rec.id || `row ${row}`;
    $("#drawer-title").textContent = `Request ${idLabel}`;
    $("#drawer-body").innerHTML = renderDetailBody(rec);
  } catch (e) {
    $("#drawer-body").innerHTML = `<div class="empty" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

async function openSessionDetail(sid) {
  openDrawer();
  $("#drawer-body").innerHTML = `<div class="empty dim">loading…</div>`;
  try {
    const data = await getJSON(`/api/sessions/${encodeURIComponent(sid)}`);
    state.detailRecord = data; state.detailRaw = false;
    const s = data.summary;
    $("#drawer-title").textContent = `Session · ${s.n_requests} requests`;
    const grid = [
      ["user", s.user_id], ["source", s.source], ["requests", s.n_requests],
      ["duration", fmtDur(s.duration_s)], ["tokens", fmtNum(s.total_tokens)],
      ["cost", fmtCost(s.total_cost)], ["tool calls", s.n_tool_calls], ["errors", s.errors],
      ["models", s.models.join(", ")], ["providers", s.providers.join(", ")],
      ["start", fmtTs(s.start_ts)], ["end", fmtTs(s.end_ts)],
    ];
    const reqs = data.requests;
    const maxLat = Math.max(1, ...reqs.map((r) => r.latency_ms || 0));
    const rows = reqs
      .map((r) => {
        const w = ((r.latency_ms || 0) / maxLat) * 100;
        const barc = r.has_error ? "var(--bad)" : "var(--accent)";
        return `<div class="tl-row" data-row="${r.row}">
          <span class="tl-meta">#${r.seq}</span>
          <span class="tl-meta mono">${r.offset_s == null ? "" : "+" + fmtDur(r.offset_s)}</span>
          <span><span class="tl-bar" style="width:${w.toFixed(1)}%;background:${barc}"></span>
            <span class="tl-meta"> ${esc(r.model_id ?? "—")} · ${fmtMs(r.latency_ms)} · ${fmtInt(r.prompt_tokens)}→${fmtInt(r.completion_tokens)} tok${r.num_tool_calls ? " · " + r.num_tool_calls + " tools" : ""}</span></span>
          <span class="tl-meta mono" style="text-align:right">Σ ${fmtNum(r.cumulative_tokens)}</span>
        </div>`;
      })
      .join("");
    $("#drawer-body").innerHTML =
      `<div class="detail-grid">${grid.map(([k, v]) => `<div><div class="k">${esc(k)}</div><div class="v">${esc(v ?? "—")}</div></div>`).join("")}</div>
       <div class="section-h">Timeline (click a turn to inspect)</div>${rows}`;
    $$("#drawer-body .tl-row").forEach((el) =>
      el.addEventListener("click", () => openRequestDetail(Number(el.dataset.row))));
  } catch (e) {
    $("#drawer-body").innerHTML = `<div class="empty" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

function openDrawer() { $("#drawer").classList.remove("hidden"); $("#scrim").classList.remove("hidden"); }
function closeDrawer() { $("#drawer").classList.add("hidden"); $("#scrim").classList.add("hidden"); state.detailRecord = null; }

// ---------- view switching ----------
function showView(view) {
  state.view = view;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  for (const v of ["overview", "requests", "sessions"]) $(`#view-${v}`).classList.toggle("hidden", v !== view);
  refresh();
}

function refresh() {
  if (state.view === "overview") loadOverview().catch(showErr);
  else if (state.view === "requests") loadRequests().catch(showErr);
  else loadSessions().catch(showErr);
}

function showErr(e) {
  console.error(e);
  const line = $("#meta-line");
  if (line) line.textContent = "error: " + e.message;
}

// ---------- init ----------
async function init() {
  // wire tabs
  $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
  // filter bar
  $("#f-apply").addEventListener("click", applyFilters);
  $("#f-reset").addEventListener("click", () => {
    for (const id of ["#f-q", "#f-user", "#f-start", "#f-end"]) $(id).value = "";
    for (const id of ["#f-model", "#f-provider", "#f-status"]) $(id).value = "";
    $("#f-errors").checked = false;
    applyFilters();
  });
  $("#f-q").addEventListener("keydown", (e) => { if (e.key === "Enter") applyFilters(); });
  $("#s-min").addEventListener("change", () => { state.sess.min = Math.max(1, Number($("#s-min").value) || 1); state.sess.page = 1; loadSessions().catch(showErr); });
  // sortable headers
  wireSort("#req-table", state.req, loadRequests);
  wireSort("#sess-table", state.sess, loadSessions);
  // drawer
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  $("#drawer-raw").addEventListener("click", () => {
    if (!state.detailRecord || !state.detailRecord.prompt_parsed) return; // only request detail has raw
    state.detailRaw = !state.detailRaw;
    $("#drawer-body").innerHTML = renderDetailBody(state.detailRecord);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  window.addEventListener("resize", debounce(() => {
    if (state.view === "overview" && state.lastSummary) renderOverview(state.lastSummary);
  }, 200));

  // meta + populate selects
  try {
    const meta = await getJSON("/api/meta");
    $("#meta-line").textContent =
      `${fmtInt(meta.total_rows)} requests · ${fmtInt(meta.n_sessions)} sessions · ${fmtTs(meta.start_ts)} → ${fmtTs(meta.end_ts)} · ${meta.source_path.split("/").pop()}`;
  } catch (e) { showErr(e); }

  try {
    const base = await getJSON("/api/summary");
    fillSelect("#f-model", base.by_model);
    fillSelect("#f-provider", base.by_provider);
    fillSelect("#f-status", base.by_status);
  } catch (e) { /* selects stay minimal */ }

  showView("overview");
}

function fillSelect(sel, items) {
  const el = $(sel);
  const label = el.options[0].text;
  el.innerHTML = `<option value="">${esc(label)}</option>` +
    items.filter((d) => d.raw != null).map((d) => `<option value="${esc(d.raw)}">${esc(d.key)} (${fmtInt(d.count)})</option>`).join("");
}

function applyFilters() {
  state.filters = readFilters();
  state.req.page = 1;
  state.sess.page = 1;
  refresh();
}

function wireSort(tableSel, cfg, reload) {
  $$(`${tableSel} thead th[data-sort]`).forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (cfg.sort === key) cfg.order = cfg.order === "asc" ? "desc" : "asc";
      else { cfg.sort = key; cfg.order = "desc"; }
      cfg.page = 1;
      reload().catch(showErr);
    });
  });
}

init();
