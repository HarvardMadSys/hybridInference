'use client';

import { area, curveMonotoneX, scaleLinear } from 'd3';
import type { KeyboardEvent, PointerEvent } from 'react';
import { useMemo, useRef } from 'react';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import {
  CONTINENT_COLORS,
  CONTINENT_NAMES,
  positiveNearestRankPercentile,
  type GeoMetricModel,
} from './geoMath';

const WIDTH = 1_000;
const HEIGHT = 96;
const MARGIN = { top: 6, right: 8, bottom: 20, left: 8 } as const;

interface StackedContinent {
  continent: string;
  lower: number[];
  upper: number[];
}

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

/** One timeline for the whole loaded window: stacked demand by continent plus the scrubber. */
export function WindowTimeline({
  data,
  selectedHourIndex,
  metricModel,
  playing,
  onTogglePlay,
  onHourChange,
  totalLabel,
}: {
  data: GeoAnalyticsResponse;
  selectedHourIndex: number | null;
  metricModel: GeoMetricModel;
  playing: boolean;
  onTogglePlay: () => void;
  onHourChange: (hourIndex: number) => void;
  totalLabel: string;
}) {
  const hourCount = data.hours_index.length;
  const lastIndex = Math.max(0, hourCount - 1);
  const isHourDetail = selectedHourIndex !== null;
  const hourIndex = Math.max(0, Math.min(lastIndex, selectedHourIndex ?? lastIndex));
  const scrubbing = useRef(false);

  const { stacked, continents, yMax } = useMemo(() => {
    const series = metricModel.continentSeries;
    const ordered = [...series.keys()].sort((a, b) => {
      const totalA = series.get(a)?.reduce((sum, value) => sum + value, 0) ?? 0;
      const totalB = series.get(b)?.reduce((sum, value) => sum + value, 0) ?? 0;
      return totalB - totalA || a.localeCompare(b);
    });
    const base = Array(hourCount).fill(0) as number[];
    const layers: StackedContinent[] = ordered.map((continent) => {
      const values = series.get(continent) ?? [];
      const lower = [...base];
      for (let index = 0; index < hourCount; index += 1) base[index] += values[index] ?? 0;
      return { continent, lower, upper: [...base] };
    });
    return {
      stacked: layers,
      continents: ordered,
      yMax: Math.max(1, positiveNearestRankPercentile(base, 0.99)),
    };
  }, [hourCount, metricModel]);

  const x = scaleLinear()
    .domain([0, Math.max(1, lastIndex)])
    .range([MARGIN.left, WIDTH - MARGIN.right]);
  const y = scaleLinear()
    .domain([0, yMax])
    .range([HEIGHT - MARGIN.bottom, MARGIN.top])
    .clamp(true);
  const layerArea = area<number>()
    .x((_, index) => x(index))
    .curve(curveMonotoneX);
  const hourIndices = Array.from({ length: hourCount }, (_, index) => index);
  const layerPaths = stacked.map((layer) => {
    layerArea.y0((_, index) => y(layer.lower[index] ?? 0));
    layerArea.y1((_, index) => y(layer.upper[index] ?? 0));
    return { continent: layer.continent, d: layerArea(hourIndices) ?? undefined };
  });

  const midnights = useMemo(() => {
    const days = Math.max(1, Math.round(hourCount / 24));
    const labelEvery = days > 14 ? 5 : days > 7 ? 2 : 1;
    const result: { index: number; label: string | null }[] = [];
    let midnightCount = 0;
    data.hours_index.forEach((timestamp, index) => {
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime()) || date.getUTCHours() !== 0) return;
      result.push({
        index,
        label: midnightCount % labelEvery === 0 ? dayLabel(timestamp) : null,
      });
      midnightCount += 1;
    });
    return result;
  }, [data.hours_index, hourCount]);

  const seekFromClientX = (element: SVGSVGElement, clientX: number) => {
    const rect = element.getBoundingClientRect();
    if (!rect.width || hourCount === 0) return;
    const svgX = ((clientX - rect.left) / rect.width) * WIDTH;
    const bounded = Math.max(MARGIN.left, Math.min(WIDTH - MARGIN.right, svgX));
    onHourChange(Math.round(x.invert(bounded)));
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
    <section className="rounded-xl border border-gray-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 disabled:opacity-40"
          disabled={!isHourDetail || hourIndex <= 0}
          onClick={() => onHourChange(hourIndex - 1)}
        >
          ← 1h
        </button>
        <button
          type="button"
          aria-pressed={playing}
          className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
            playing ? 'bg-gray-900 text-white' : 'border border-gray-200 bg-white text-gray-700'
          }`}
          onClick={onTogglePlay}
        >
          {playing ? '⏸ Pause' : '▶ Play'}
        </button>
        <button
          type="button"
          className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 disabled:opacity-40"
          disabled={!isHourDetail || hourIndex >= lastIndex}
          onClick={() => onHourChange(hourIndex + 1)}
        >
          1h →
        </button>
        <p className="ml-auto text-sm font-medium tabular-nums text-gray-700">{readout}</p>
      </div>
      <svg
        aria-label="Demand timeline for the loaded window"
        aria-orientation="horizontal"
        aria-valuemax={lastIndex}
        aria-valuemin={0}
        aria-valuenow={hourIndex}
        aria-valuetext={isHourDetail ? readout : `${readout}; select an hour for details`}
        className="mt-2 block h-24 w-full cursor-crosshair touch-none overflow-visible"
        onKeyDown={handleKeyDown}
        onPointerCancel={handlePointerEnd}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerEnd}
        preserveAspectRatio="none"
        role="slider"
        tabIndex={0}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      >
        {midnights.map(({ index, label }) => (
          <g key={index}>
            <line
              stroke="rgba(107,114,128,0.18)"
              x1={x(index)}
              x2={x(index)}
              y1={MARGIN.top}
              y2={HEIGHT - MARGIN.bottom}
            />
            {label ? (
              <text fill="#9ca3af" fontSize="10" x={x(index) + 3} y={HEIGHT - 6}>
                {label}
              </text>
            ) : null}
          </g>
        ))}
        {layerPaths.map((layer) => (
          <path
            key={layer.continent}
            data-continent={layer.continent}
            d={layer.d}
            fill={CONTINENT_COLORS[layer.continent] ?? CONTINENT_COLORS['?']}
            opacity={0.75}
          />
        ))}
        {isHourDetail && (
          <line
            data-testid="selected-hour-marker"
            stroke="#111827"
            strokeWidth="1.2"
            x1={x(hourIndex)}
            x2={x(hourIndex)}
            y1={MARGIN.top - 2}
            y2={HEIGHT - MARGIN.bottom + 3}
          />
        )}
      </svg>
      <div className="mt-1.5 flex flex-wrap gap-3 text-[11px] text-gray-500">
        {continents.map((continent) => (
          <span key={continent} className="inline-flex items-center gap-1">
            <span
              aria-hidden="true"
              className="h-2 w-2 rounded-full"
              style={{ backgroundColor: CONTINENT_COLORS[continent] ?? '#898781' }}
            />
            {CONTINENT_NAMES[continent] ?? continent}
          </span>
        ))}
      </div>
    </section>
  );
}
