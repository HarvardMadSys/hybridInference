// A small time-series chart drawn on a canvas: the geometry, the drawing, the
// point under the pointer, zooming and panning, and the CSV of what is drawn.
// Free of React so it can be tested without a browser.

export interface ChartPoint {
  /** Milliseconds since the epoch. */
  t: number;
  v: number | null;
}

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
  points: ChartPoint[];
}

export interface ChartOptions {
  /** Bottom of the value axis; 0 unless given. */
  min?: number;
  /** Top of the value axis at least; the data may raise it. */
  max?: number;
  /** How an axis label and a tooltip value are written. */
  format?: (value: number) => string;
}

/** The span shown; null on either side follows the data. */
export interface ChartWindow {
  tMin: number | null;
  tMax: number | null;
}

export const FULL_WINDOW: ChartWindow = { tMin: null, tMax: null };

export interface ChartColors {
  /** Axis labels and the empty-chart note. */
  text: string;
  /** The horizontal grid lines. */
  grid: string;
  /** The ring around the points under the pointer: the chart's background. */
  surface: string;
}

interface Padding {
  l: number;
  r: number;
  t: number;
  b: number;
}

/** Where everything falls for one width and height, or null with nothing to draw. */
export interface ChartLayout {
  pad: Padding;
  width: number;
  height: number;
  innerW: number;
  innerH: number;
  /** The data's first and last times. */
  dataT0: number;
  dataT1: number;
  /** The window's first and last times. */
  t0: number;
  t1: number;
  min: number;
  max: number;
}

export interface HoverRow {
  label: string;
  color: string;
  v: number;
}

export interface Hover {
  t: number;
  x: number;
  rows: HoverRow[];
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function padding(width: number): Padding {
  return width < 420 ? { l: 34, r: 8, t: 10, b: 22 } : { l: 48, r: 12, t: 12, b: 28 };
}

export function computeLayout(
  series: readonly ChartSeries[],
  opts: ChartOptions,
  win: ChartWindow,
  width: number,
  height: number,
): ChartLayout | null {
  let dataT0 = Infinity;
  let dataT1 = -Infinity;
  for (const s of series) {
    for (const p of s.points) {
      if (p.v === null) continue;
      if (p.t < dataT0) dataT0 = p.t;
      if (p.t > dataT1) dataT1 = p.t;
    }
  }
  if (dataT0 === Infinity) return null;
  const pad = padding(width);
  const t0 = win.tMin ?? dataT0;
  const t1 = win.tMax ?? (dataT1 || t0 + 1);
  const min = opts.min ?? 0;
  let max = Math.max(opts.max ?? 1, min + 1);
  for (const s of series) {
    for (const p of s.points) {
      if (p.v !== null && p.t >= t0 && p.t <= t1 && p.v > max) max = p.v;
    }
  }
  return {
    pad,
    width,
    height,
    innerW: width - pad.l - pad.r,
    innerH: height - pad.t - pad.b,
    dataT0,
    dataT1,
    t0,
    t1,
    min,
    max,
  };
}

function span(layout: ChartLayout): number {
  return Math.max(1, layout.t1 - layout.t0);
}

export function xOf(layout: ChartLayout, t: number): number {
  return layout.pad.l + ((t - layout.t0) / span(layout)) * layout.innerW;
}

export function yOf(layout: ChartLayout, v: number): number {
  const { pad, innerH, min, max } = layout;
  return pad.t + innerH - ((v - min) / (max - min)) * innerH;
}

const pad2 = (n: number) => String(n).padStart(2, '0');

/**
 * An axis time: the clock to the second within half an hour, to the minute
 * within a day and a half, then the date, then the year.
 */
export function formatTick(t: number, spanMs: number): string {
  const d = new Date(t);
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (spanMs <= 30 * MINUTE) return `${hm}:${pad2(d.getSeconds())}`;
  if (spanMs <= 36 * HOUR) return hm;
  if (spanMs <= 40 * DAY) return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** A tooltip time: to the second, with the date once the span passes a day and a half. */
export function formatTipTime(t: number, spanMs: number): string {
  const d = new Date(t);
  const hms = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  if (spanMs <= 36 * HOUR) return hms;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hms}`;
}

export function windowSpan(layout: ChartLayout): number {
  return span(layout);
}

/**
 * Draw the chart: grid and value labels, three time labels, one line per series
 * with a dot on its last point, and the pointer's column when there is one.
 */
export function drawChart(
  ctx: CanvasRenderingContext2D,
  layout: ChartLayout | null,
  series: readonly ChartSeries[],
  opts: ChartOptions,
  colors: ChartColors,
  hover: Hover | null,
  size: { width: number; height: number },
  emptyLabel: string,
): void {
  ctx.clearRect(0, 0, size.width, size.height);
  if (!layout) {
    const pad = padding(size.width);
    ctx.fillStyle = colors.text;
    ctx.font = '12px ui-sans-serif, system-ui, sans-serif';
    ctx.fillText(emptyLabel, pad.l, pad.t + 16);
    return;
  }
  const { pad, width, height, innerW, innerH, t0, t1, min, max } = layout;
  const spanMs = span(layout);

  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
  ctx.fillStyle = colors.text;
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.t + (innerH * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(width - pad.r, y);
    ctx.stroke();
    const value = max - ((max - min) * i) / 4;
    ctx.fillText(opts.format ? opts.format(value) : String(Math.round(value)), 4, y + 3);
  }
  [t0, t0 + spanMs / 2, t1].forEach((t, i) => {
    const x = pad.l + (i / 2) * innerW;
    const label = formatTick(t, spanMs);
    const tw = ctx.measureText(label).width;
    const tx = i === 0 ? x : i === 2 ? x - tw : x - tw / 2;
    ctx.fillText(label, tx, height - 8);
  });

  for (const line of series) {
    const points = line.points.filter(
      (p): p is { t: number; v: number } => p.v !== null && p.t >= t0 && p.t <= t1,
    );
    if (!points.length) continue;
    if (points.length >= 2) {
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = xOf(layout, p.t);
        const y = yOf(layout, p.v);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = line.color;
      ctx.lineWidth = 1.8;
      ctx.stroke();
    }
    const last = points[points.length - 1];
    ctx.fillStyle = line.color;
    ctx.beginPath();
    ctx.arc(xOf(layout, last.t), yOf(layout, last.v), 2.4, 0, Math.PI * 2);
    ctx.fill();
  }

  if (hover && hover.rows.length) {
    ctx.save();
    ctx.globalAlpha = 0.45;
    ctx.strokeStyle = colors.text;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(hover.x, pad.t);
    ctx.lineTo(hover.x, pad.t + innerH);
    ctx.stroke();
    ctx.restore();
    for (const row of hover.rows) {
      ctx.fillStyle = row.color;
      ctx.beginPath();
      ctx.arc(hover.x, yOf(layout, row.v), 3.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = colors.surface;
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }
  }
}

/**
 * The time nearest the pointer that some series has a point at, and each
 * series' value there (a series whose nearest point is too far away is left
 * out). Null outside the plot or with nothing near.
 */
export function hoverAt(
  series: readonly ChartSeries[],
  layout: ChartLayout | null,
  offsetX: number,
): Hover | null {
  if (!layout || layout.innerW <= 0) return null;
  const { pad, innerW, t0, t1 } = layout;
  if (offsetX < pad.l || offsetX > pad.l + innerW) return null;
  const spanMs = span(layout);
  const t = t0 + ((offsetX - pad.l) / innerW) * spanMs;
  let bestT: number | null = null;
  let bestD = Infinity;
  for (const s of series) {
    for (const p of s.points) {
      if (p.v === null || p.t < t0 || p.t > t1) continue;
      const d = Math.abs(p.t - t);
      if (d < bestD) {
        bestD = d;
        bestT = p.t;
      }
    }
  }
  if (bestT === null) return null;
  const tolerance = Math.max(spanMs * 0.02, 30_000);
  const rows: HoverRow[] = [];
  for (const s of series) {
    let hit: ChartPoint | null = null;
    let hitD = Infinity;
    for (const p of s.points) {
      if (p.v === null) continue;
      const d = Math.abs(p.t - bestT);
      if (d < hitD) {
        hitD = d;
        hit = p;
      }
    }
    if (hit && hit.v !== null && hitD <= tolerance) {
      rows.push({ label: s.label || s.key, color: s.color, v: hit.v });
    }
  }
  if (!rows.length) return null;
  return { t: bestT, x: xOf(layout, bestT), rows };
}

/**
 * The window after one wheel step at `offsetX`: 25% wider out, 20% narrower in,
 * around the time under the pointer, never under a minute or past the data.
 */
export function zoomWindow(layout: ChartLayout, offsetX: number, deltaY: number): ChartWindow {
  const { pad, innerW, dataT0, dataT1, t0 } = layout;
  const spanMs = span(layout);
  const frac = Math.min(1, Math.max(0, (offsetX - pad.l) / innerW));
  const factor = deltaY > 0 ? 1.25 : 0.8;
  const nextSpan = Math.max(MINUTE, Math.min(dataT1 - dataT0, spanMs * factor));
  let nextMin = t0 + frac * spanMs - frac * nextSpan;
  nextMin = Math.max(dataT0, Math.min(nextMin, dataT1 - nextSpan));
  return { tMin: nextMin, tMax: nextMin + nextSpan };
}

/** The window moved by a drag of `dx` pixels, kept within the data. */
export function panWindow(layout: ChartLayout, dx: number): ChartWindow {
  const { innerW, dataT0, dataT1, t0 } = layout;
  const spanMs = span(layout);
  let nextMin = t0 - (dx / innerW) * spanMs;
  nextMin = Math.max(dataT0, Math.min(nextMin, dataT1 - spanMs));
  return { tMin: nextMin, tMax: nextMin + spanMs };
}

function csvCell(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** Every point drawn, one row per time, one column per series. */
export function seriesCsv(series: readonly ChartSeries[]): string {
  const headers = ['time_iso', 'unix_ms', ...series.map((s) => s.label || s.key)];
  const maps = series.map((s) => {
    const m = new Map<number, number>();
    for (const p of s.points) if (p.v !== null) m.set(p.t, p.v);
    return m;
  });
  const times = new Set<number>();
  for (const m of maps) for (const t of m.keys()) times.add(t);
  const rows = [...times]
    .sort((a, b) => a - b)
    .map((t) =>
      [
        new Date(t).toISOString(),
        String(t),
        ...maps.map((m) => (m.has(t) ? String(m.get(t)) : '')),
      ].join(','),
    );
  return [headers.map(csvCell).join(','), ...rows].join('\n');
}
