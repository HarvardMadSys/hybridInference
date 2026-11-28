'use client';

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';

import { useTheme } from '@/components/providers/ThemeProvider';
import {
  computeLayout,
  drawChart,
  formatTipTime,
  FULL_WINDOW,
  hoverAt,
  panWindow,
  seriesCsv,
  windowSpan,
  zoomWindow,
  type ChartLayout,
  type ChartOptions,
  type ChartSeries,
  type ChartWindow,
  type Hover,
} from './chartCanvas';
import type { ChartSpec } from './charts';
import { fill } from '@/lib/utils/interpolate';
import { saveFile } from './download';
import type { InfraFormat } from './format';
import { InfraDialog, smallButton } from './InfraDialog';

interface Tip {
  hover: Hover;
  clientX: number;
  clientY: number;
  spanMs: number;
}

/** The theme's colors, as the canvas's own classes resolve them. */
function canvasColors(canvas: HTMLCanvasElement) {
  const style = getComputedStyle(canvas);
  const surface = canvas.closest<HTMLElement>('[data-chart-surface]');
  return {
    text: style.color || '#6b7280',
    grid: style.borderTopColor || '#e5e7eb',
    surface: (surface && getComputedStyle(surface).backgroundColor) || '#ffffff',
  };
}

/**
 * One chart on a canvas: hover for the values at a time, scroll to zoom, drag
 * to pan (a tap shows the values on a touch screen), double-click to reset.
 * The window shown is kept in `windowRef` when one is passed, so a parent can
 * open the same view larger.
 *
 * With `zoomNeedsModifier` the wheel zooms only with Ctrl or ⌘ held (a
 * trackpad pinch sends the same), so a page of charts still scrolls under a
 * plain wheel.
 */
export function TimeSeriesChart({
  series,
  opts,
  label,
  describedBy,
  height,
  emptyLabel,
  initialWindow = FULL_WINDOW,
  windowRef,
  resetKey = 0,
  touchPan = false,
  zoomNeedsModifier = false,
}: {
  series: ChartSeries[];
  opts: ChartOptions;
  /** What a screen reader announces for the chart. */
  label: string;
  describedBy?: string;
  height: number;
  emptyLabel: string;
  initialWindow?: ChartWindow;
  windowRef?: { current: ChartWindow };
  /** Changing it returns the chart to the whole range. */
  resetKey?: number;
  /** Let a finger drag the window (in the large view) rather than only tap. */
  touchPan?: boolean;
  /** Zoom on the wheel only with Ctrl or ⌘ held. */
  zoomNeedsModifier?: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const win = useRef<ChartWindow>(initialWindow);
  const layout = useRef<ChartLayout | null>(null);
  const hover = useRef<Hover | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const { resolvedTheme } = useTheme();
  // The handlers are installed once; this keeps them reading the latest props.
  const latest = useRef({ series, opts, emptyLabel, touchPan, zoomNeedsModifier, windowRef });
  useLayoutEffect(() => {
    latest.current = { series, opts, emptyLabel, touchPan, zoomNeedsModifier, windowRef };
  });

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const width = canvas.clientWidth || 720;
    const cssHeight = canvas.clientHeight || height;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(cssHeight * dpr);
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = canvas.getContext('2d');
    } catch {
      ctx = null;
    }
    const { series: s, opts: o, emptyLabel: empty, windowRef: shared } = latest.current;
    layout.current = computeLayout(s, o, win.current, width, cssHeight);
    if (shared) shared.current = win.current;
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawChart(
      ctx,
      layout.current,
      s,
      o,
      canvasColors(canvas),
      hover.current,
      {
        width,
        height: cssHeight,
      },
      empty,
    );
  }, [height]);

  // New data keeps the window the reader chose; a new reset key returns to all of it.
  const lastReset = useRef(resetKey);
  useEffect(() => {
    if (lastReset.current === resetKey) return;
    lastReset.current = resetKey;
    win.current = FULL_WINDOW;
    hover.current = null;
    setTip(null);
    draw();
  }, [resetKey, draw]);

  useEffect(() => {
    draw();
  }, [series, opts, emptyLabel, resolvedTheme, draw]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => draw());
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [draw]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let dragging = false;
    let moved = false;
    let lastX = 0;
    let frame = 0;
    let pending: { x: number; cx: number; cy: number } | null = null;

    const showHover = (offsetX: number, clientX: number, clientY: number) => {
      const found = hoverAt(latest.current.series, layout.current, offsetX);
      hover.current = found;
      draw();
      setTip(
        found && layout.current
          ? { hover: found, clientX, clientY, spanMs: windowSpan(layout.current) }
          : null,
      );
    };
    const clearHover = () => {
      hover.current = null;
      draw();
      setTip(null);
    };

    const onWheel = (event: WheelEvent) => {
      if (!layout.current) return;
      if (latest.current.zoomNeedsModifier && !event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      win.current = zoomWindow(layout.current, event.offsetX, event.deltaY);
      showHover(event.offsetX, event.clientX, event.clientY);
    };
    const onDown = (event: PointerEvent) => {
      if (event.pointerType === 'touch' && !latest.current.touchPan) {
        showHover(event.offsetX, event.clientX, event.clientY);
        return;
      }
      dragging = true;
      moved = false;
      lastX = event.offsetX;
      setTip(null);
      canvas.setPointerCapture?.(event.pointerId);
    };
    const onMove = (event: PointerEvent) => {
      if (!layout.current) return;
      if (dragging) {
        const dx = event.offsetX - lastX;
        if (Math.abs(dx) > 3) moved = true;
        lastX = event.offsetX;
        win.current = panWindow(layout.current, dx);
        hover.current = null;
        draw();
        return;
      }
      pending = { x: event.offsetX, cx: event.clientX, cy: event.clientY };
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (pending) showHover(pending.x, pending.cx, pending.cy);
      });
    };
    const onUp = (event: PointerEvent) => {
      const wasDrag = dragging && moved;
      dragging = false;
      if (wasDrag) {
        setTip(null);
        return;
      }
      showHover(event.offsetX, event.clientX, event.clientY);
    };
    const onLeave = () => {
      if (dragging) return;
      pending = null;
      clearHover();
    };
    const onDoubleClick = () => {
      win.current = FULL_WINDOW;
      clearHover();
    };

    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('pointerdown', onDown);
    canvas.addEventListener('pointermove', onMove);
    canvas.addEventListener('pointerup', onUp);
    canvas.addEventListener('pointerleave', onLeave);
    canvas.addEventListener('dblclick', onDoubleClick);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('pointerdown', onDown);
      canvas.removeEventListener('pointermove', onMove);
      canvas.removeEventListener('pointerup', onUp);
      canvas.removeEventListener('pointerleave', onLeave);
      canvas.removeEventListener('dblclick', onDoubleClick);
    };
  }, [draw]);

  // Keep the tooltip beside the pointer and inside the window.
  useLayoutEffect(() => {
    const el = tipRef.current;
    if (!el || !tip) return;
    const margin = 12;
    let x = tip.clientX + 14;
    let y = tip.clientY + 16;
    if (x + el.offsetWidth > window.innerWidth - margin) x = tip.clientX - el.offsetWidth - 12;
    if (y + el.offsetHeight > window.innerHeight - margin) y = tip.clientY - el.offsetHeight - 12;
    el.style.left = `${Math.max(margin, x)}px`;
    el.style.top = `${Math.max(margin, y)}px`;
  }, [tip]);

  const format = opts.format ?? ((v: number) => String(Math.round(v * 10) / 10));
  return (
    <>
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={label}
        aria-describedby={describedBy}
        className="block w-full touch-pan-y border-gray-200 text-gray-500"
        style={{ height }}
      />
      {tip ? (
        <div
          ref={tipRef}
          role="tooltip"
          className="pointer-events-none fixed z-50 max-w-xs rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-700 shadow-lg"
          style={{ left: -9999, top: -9999 }}
        >
          <div className="mb-1 font-mono text-gray-500">
            {formatTipTime(tip.hover.t, tip.spanMs)}
          </div>
          {tip.hover.rows.map((row, i) => (
            <div key={`${row.label}:${i}`} className="flex items-center gap-2 tabular-nums">
              <span
                aria-hidden="true"
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: row.color }}
              />
              <span className="min-w-0 flex-1 truncate">{row.label}</span>
              <b className="font-semibold text-gray-900">{format(row.v)}</b>
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

function slug(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'series'
  );
}

/** Save a chart's points as CSV, named for the chart, the deployment and the time. */
export function downloadChartCsv(spec: ChartSpec, deploy: string): void {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  saveFile(
    `${slug(spec.id)}-${slug(deploy || 'fleet')}-${stamp}.csv`,
    seriesCsv(spec.series),
    'text/csv',
  );
}

/**
 * A chart in its card: title, legend and unit, Zoom and CSV, the canvas, and
 * the zoom dialog that opens the same view larger with its description.
 */
export function ChartPanel({
  spec,
  deploy,
  f,
}: {
  spec: ChartSpec;
  /** The deployment shown, for the CSV's name. */
  deploy: string;
  f: InfraFormat;
}) {
  const { t } = f;
  const descId = useId();
  const windowRef = useRef<ChartWindow>(FULL_WINDOW);
  const [zoom, setZoom] = useState<ChartWindow | null>(null);
  const [zoomReset, setZoomReset] = useState(0);
  const empty = t('openinfra.chart.empty', 'No samples');
  const label = spec.unit ? `${spec.title} (${spec.unit})` : spec.title;

  return (
    <article
      data-chart-surface
      className="min-w-0 rounded-2xl border border-gray-200 bg-white p-4 shadow-sm"
    >
      <header className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-900">{spec.title}</h3>
        <div className="flex gap-1.5">
          <button
            type="button"
            className={smallButton}
            onClick={() => setZoom({ ...windowRef.current })}
            aria-label={fill(t('openinfra.chart.zoom_label', 'Zoom: {chart}'), {
              chart: spec.title,
            })}
          >
            {t('openinfra.chart.zoom', 'Zoom')}
          </button>
          <button
            type="button"
            className={smallButton}
            onClick={() => downloadChartCsv(spec, deploy)}
            aria-label={fill(t('openinfra.chart.csv_label', 'CSV: {chart}'), { chart: spec.title })}
          >
            {t('openinfra.chart.csv', 'CSV')}
          </button>
        </div>
      </header>
      <Legend series={spec.series} unit={spec.unit} />
      <p id={descId} className="sr-only">
        {spec.desc}
      </p>
      <TimeSeriesChart
        series={spec.series}
        opts={spec.opts}
        label={label}
        describedBy={descId}
        height={180}
        emptyLabel={empty}
        windowRef={windowRef}
        zoomNeedsModifier
      />
      {zoom ? (
        <InfraDialog
          wide
          title={spec.title}
          onClose={() => setZoom(null)}
          actions={
            <>
              <button
                type="button"
                className={smallButton}
                onClick={() => setZoomReset((n) => n + 1)}
              >
                {t('openinfra.chart.reset', 'Reset')}
              </button>
              <button
                type="button"
                className={smallButton}
                onClick={() => downloadChartCsv(spec, deploy)}
              >
                {t('openinfra.chart.csv', 'CSV')}
              </button>
              <button type="button" className={smallButton} onClick={() => setZoom(null)}>
                {t('openinfra.chart.close', 'Close')}
              </button>
            </>
          }
          description={
            <>
              {spec.unit ? (
                <p className="font-medium text-gray-700">
                  {t('openinfra.chart.unit', 'Unit')}: {spec.unit}
                </p>
              ) : null}
              <p>{spec.desc}</p>
              <p className="text-xs text-gray-500">
                {t(
                  'openinfra.chart.help',
                  'Hover for values · scroll to zoom · drag to pan · double-click to reset',
                )}
              </p>
            </>
          }
        >
          <div data-chart-surface className="bg-white">
            <Legend series={spec.series} unit="" />
            <TimeSeriesChart
              series={spec.series}
              opts={spec.opts}
              label={label}
              height={420}
              emptyLabel={empty}
              initialWindow={zoom}
              resetKey={zoomReset}
              touchPan
            />
          </div>
        </InfraDialog>
      ) : null}
    </article>
  );
}

function Legend({ series, unit }: { series: ChartSeries[]; unit: string }) {
  if (!series.length && !unit) return null;
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-600">
      {series.map((s) => (
        <span key={s.key} className="inline-flex min-w-0 items-center gap-1.5">
          <span
            aria-hidden="true"
            className="h-0.5 w-3.5 shrink-0 rounded-full"
            style={{ backgroundColor: s.color }}
          />
          <span className="truncate">{s.label}</span>
        </span>
      ))}
      {unit ? <span className="ml-auto text-gray-500">{unit}</span> : null}
    </div>
  );
}
