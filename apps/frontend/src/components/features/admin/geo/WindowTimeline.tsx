'use client';

import { area, curveMonotoneX, line, scaleLinear } from 'd3';
import type { KeyboardEvent, PointerEvent } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { buildOriginSeries, ORIGIN_COLORS, type GeoMetricModel } from './geoMath';

const HEIGHT = 112;
// The right margin holds the value ticks; the plot itself starts flush left.
const MARGIN = { top: 10, right: 40, bottom: 22, left: 0 } as const;
const FALLBACK_WIDTH = 1_000;
const MIN_DAY_LABEL_GAP_PX = 58;

function formatUtcReadout(timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return 'Unknown UTC hour';
  const day = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return `${day} · ${String(date.getUTCHours()).padStart(2, '0')}:00 UTC`;
}

function dayLabel(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function formatTick(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 100) / 10}k`;
  return String(Math.round(value));
}

/** The chart's rendered width; drawn in real pixels so its text is never stretched. */
function useMeasuredWidth<T extends Element>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const update = () => {
      const measured = element.getBoundingClientRect().width;
      if (measured > 0) setWidth(measured);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

const TOOL_BUTTON =
  'rounded-md px-2.5 py-1 text-xs text-slate-300 ring-1 ring-inset ring-white/10 transition hover:bg-white/[0.06] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/80 disabled:pointer-events-none disabled:opacity-35';

/** One timeline for the whole loaded window: total demand, the selected country over it, and the scrubber. */
export function WindowTimeline({
  data,
  selectedHourIndex,
  metricModel,
  playing,
  onTogglePlay,
  onHourChange,
  onReturnToTotal,
  totalLabel,
  selectedCountry,
  selectedCountryName,
}: {
  data: GeoAnalyticsResponse;
  selectedHourIndex: number | null;
  metricModel: GeoMetricModel;
  playing: boolean;
  onTogglePlay: () => void;
  onHourChange: (hourIndex: number) => void;
  onReturnToTotal: () => void;
  totalLabel: string;
  selectedCountry: string | null;
  selectedCountryName: string | null;
}) {
  const hourCount = data.hours_index.length;
  const lastIndex = Math.max(0, hourCount - 1);
  const isHourDetail = selectedHourIndex !== null;
  const hourIndex = Math.max(0, Math.min(lastIndex, selectedHourIndex ?? lastIndex));
  const scrubbing = useRef(false);
  const [chartRef, width] = useMeasuredWidth<SVGSVGElement>();

  const metric = metricModel.metric;
  const { totalSeries, yMax } = useMemo(() => {
    const series = buildOriginSeries(data, metric);
    // Preserve spikes: the labeled axis must represent every plotted value.
    const cap = Math.max(1, ...series);
    return { totalSeries: series, yMax: scaleLinear().domain([0, cap]).nice(3).domain()[1] };
  }, [data, metric]);
  const countrySeries = useMemo(
    () => (selectedCountry ? buildOriginSeries(data, metric, selectedCountry) : null),
    [data, metric, selectedCountry],
  );

  const plotRight = Math.max(MARGIN.left + 1, width - MARGIN.right);
  const x = scaleLinear()
    .domain([0, Math.max(1, lastIndex)])
    .range([MARGIN.left, plotRight]);
  const y = scaleLinear()
    .domain([0, yMax])
    .range([HEIGHT - MARGIN.bottom, MARGIN.top])
    .clamp(true);
  const seriesArea = area<number>()
    .x((_, index) => x(index))
    .y0(y(0))
    .y1((value) => y(value))
    .curve(curveMonotoneX);
  const seriesLine = line<number>()
    .x((_, index) => x(index))
    .y((value) => y(value))
    .curve(curveMonotoneX);
  const ticks = y.ticks(2).filter((tick) => tick > 0 && Number.isInteger(tick));

  const midnights = useMemo(() => {
    const result: { index: number; timestamp: string }[] = [];
    data.hours_index.forEach((timestamp, index) => {
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime()) || date.getUTCHours() !== 0) return;
      result.push({ index, timestamp });
    });
    return result;
  }, [data.hours_index]);
  // Space day labels by real pixels so they never run together on a phone.
  const pixelsPerDay = ((plotRight - MARGIN.left) * 24) / Math.max(1, lastIndex);
  const labelEvery = Math.max(1, Math.ceil(MIN_DAY_LABEL_GAP_PX / Math.max(1, pixelsPerDay)));

  const seekFromClientX = (element: SVGSVGElement, clientX: number) => {
    const rect = element.getBoundingClientRect();
    if (!rect.width || hourCount === 0) return;
    const plotWidth = Math.max(1, rect.width - MARGIN.left - MARGIN.right);
    const fraction = Math.max(0, Math.min(1, (clientX - rect.left - MARGIN.left) / plotWidth));
    onHourChange(Math.round(fraction * lastIndex));
  };

  const handlePointerDown = (event: PointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) return;
    scrubbing.current = true;
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Scrubbing still works while the pointer stays over the chart.
    }
    seekFromClientX(event.currentTarget, event.clientX);
  };
  const handlePointerMove = (event: PointerEvent<SVGSVGElement>) => {
    if (!scrubbing.current) return;
    seekFromClientX(event.currentTarget, event.clientX);
  };
  const handlePointerEnd = (event: PointerEvent<SVGSVGElement>) => {
    scrubbing.current = false;
    try {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
    } catch {
      // Pointer capture may already be released.
    }
  };

  const handleKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    let next = hourIndex;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') next -= 1;
    else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') next += 1;
    else if (event.key === 'PageDown') next -= 24;
    else if (event.key === 'PageUp') next += 24;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = lastIndex;
    else return;
    event.preventDefault();
    onHourChange(Math.max(0, Math.min(lastIndex, next)));
  };

  const readout = isHourDetail ? formatUtcReadout(data.hours_index[hourIndex]) : totalLabel;

  return (
    <section className="border-t border-white/[0.07] px-5 pb-4 pt-4 sm:px-8">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-slate-500">
          {metric === 'tout' ? 'Output tokens' : 'Requests'} per hour (UTC)
          {selectedCountryName && (
            <span className="ml-3 text-xs normal-case tracking-normal text-white">
              <span aria-hidden="true">— </span>
              {selectedCountryName}
            </span>
          )}
        </p>
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            className={TOOL_BUTTON}
            disabled={!isHourDetail || hourIndex <= 0}
            onClick={() => onHourChange(hourIndex - 1)}
          >
            ← 1h
          </button>
          <button
            type="button"
            aria-pressed={playing}
            className={`${TOOL_BUTTON} min-w-[4.75rem] font-medium ${
              playing ? 'bg-sky-300 text-slate-950 ring-sky-300 hover:bg-sky-200' : ''
            }`}
            onClick={onTogglePlay}
          >
            {playing ? '⏸ Pause' : '▶ Play'}
          </button>
          <button
            type="button"
            className={TOOL_BUTTON}
            disabled={!isHourDetail || hourIndex >= lastIndex}
            onClick={() => onHourChange(hourIndex + 1)}
          >
            1h →
          </button>
          {/* Always laid out so entering or leaving an hour never reflows the
              toolbar; hidden from sight and assistive tech on the total. */}
          <button
            type="button"
            aria-hidden={!isHourDetail}
            className={`ml-1 rounded-md px-2 py-1 text-xs font-medium text-sky-300 transition hover:text-sky-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/80 ${
              isHourDetail ? '' : 'invisible'
            }`}
            disabled={!isHourDetail}
            onClick={onReturnToTotal}
            tabIndex={isHourDetail ? undefined : -1}
          >
            <span aria-hidden="true">↩ </span>Back to {totalLabel}
          </button>
          {/* First on a phone, so the reserved button above trails the row
              instead of indenting the readout. */}
          <p className="order-first w-full text-[13px] tabular-nums text-slate-300 sm:order-none sm:w-auto sm:min-w-[9.5rem] sm:text-right">
            <span className="text-slate-500">Showing </span>
            <span className="font-medium text-white">{readout}</span>
          </p>
        </div>
      </div>
      <svg
        ref={chartRef}
        aria-label="Demand timeline for the loaded window"
        aria-orientation="horizontal"
        aria-valuemax={lastIndex}
        aria-valuemin={0}
        aria-valuenow={hourIndex}
        aria-valuetext={isHourDetail ? readout : `${readout}; select an hour for details`}
        // Same UA `:focus` outline as the globe marks: keyboard focus only.
        className="mt-3 block w-full cursor-crosshair touch-none select-none overflow-visible rounded-sm focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-sky-300"
        height={HEIGHT}
        onKeyDown={handleKeyDown}
        onPointerCancel={handlePointerEnd}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerEnd}
        role="slider"
        tabIndex={0}
        viewBox={`0 0 ${width} ${HEIGHT}`}
      >
        {[0, ...ticks].map((tick) => (
          <g key={tick}>
            <line
              stroke="rgba(255,255,255,0.06)"
              x1={MARGIN.left}
              x2={plotRight + 6}
              y1={y(tick)}
              y2={y(tick)}
            />
            <text fill="#64748b" fontSize={11} textAnchor="end" x={width} y={y(tick) + 4}>
              {formatTick(tick)}
            </text>
          </g>
        ))}
        {midnights.map(({ index, timestamp }, position) =>
          position % labelEvery === 0 ? (
            <text key={index} fill="#64748b" fontSize={11} x={x(index)} y={HEIGHT - 5}>
              {dayLabel(timestamp)}
            </text>
          ) : null,
        )}
        <path
          d={seriesArea(totalSeries) ?? undefined}
          data-series="total"
          fill={ORIGIN_COLORS.mark}
          fillOpacity={countrySeries ? 0.07 : 0.12}
        />
        <path
          d={seriesLine(totalSeries) ?? undefined}
          fill="none"
          stroke={ORIGIN_COLORS.mark}
          strokeOpacity={countrySeries ? 0.55 : 0.95}
          strokeWidth={1.25}
        />
        {countrySeries && (
          <path
            d={seriesLine(countrySeries) ?? undefined}
            data-series="selected"
            fill="none"
            stroke={ORIGIN_COLORS.selected}
            strokeWidth={1.75}
          />
        )}
        {isHourDetail && (
          <g pointerEvents="none">
            <line
              data-testid="selected-hour-marker"
              stroke={ORIGIN_COLORS.selected}
              strokeWidth={1.5}
              x1={x(hourIndex)}
              x2={x(hourIndex)}
              y1={MARGIN.top - 6}
              y2={y(0)}
            />
            <circle
              cx={x(hourIndex)}
              cy={y(totalSeries[hourIndex] ?? 0)}
              fill={ORIGIN_COLORS.selected}
              r={3.5}
            />
          </g>
        )}
      </svg>
    </section>
  );
}
