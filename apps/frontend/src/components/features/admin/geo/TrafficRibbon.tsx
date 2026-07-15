'use client';

import { curveMonotoneX, line, scaleLinear } from 'd3';
import type { MouseEvent } from 'react';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { buildContinentSeries, CONTINENT_COLORS, CONTINENT_NAMES, type GeoMetric } from './geoMath';

const WIDTH = 1_000;
const HEIGHT = 104;
const MARGIN = { top: 10, right: 48, bottom: 22, left: 8 } as const;

function metricLabel(metric: GeoMetric): string {
  if (metric === 'tout') return 'output tokens';
  if (metric === 'gs') return 'compute seconds (est.)';
  return 'requests';
}

export function TrafficRibbon({
  data,
  hourIndex,
  metric,
  onHourChange,
}: {
  data: GeoAnalyticsResponse;
  hourIndex: number;
  metric: GeoMetric;
  onHourChange: (hourIndex: number) => void;
}) {
  const selected = new Date(data.hours_index[hourIndex]);
  const validSelected = !Number.isNaN(selected.getTime());
  const dayKey = validSelected ? selected.toISOString().slice(0, 10) : '';
  const indices = data.hours_index
    .map((timestamp, index) => ({ timestamp, index }))
    .filter(({ timestamp }) => timestamp.slice(0, 10) === dayKey)
    .map(({ index }) => index);
  const start = indices[0] ?? Math.max(0, hourIndex);
  const end = indices.at(-1) ?? start;
  const dayLength = Math.max(1, end - start + 1);
  const series = buildContinentSeries(data, metric);
  const continents = [...series.keys()].sort((a, b) => {
    const aTotal = series.get(a)?.reduce((sum, value) => sum + value, 0) ?? 0;
    const bTotal = series.get(b)?.reduce((sum, value) => sum + value, 0) ?? 0;
    return bTotal - aTotal || a.localeCompare(b);
  });

  let maximum = 1;
  for (const values of series.values()) {
    for (let index = start; index <= end; index += 1) {
      maximum = Math.max(maximum, values[index] ?? 0);
    }
  }

  const x = scaleLinear()
    .domain([start, start + Math.max(1, dayLength - 1)])
    .range([MARGIN.left, WIDTH - MARGIN.right]);
  const y = scaleLinear()
    .domain([0, maximum])
    .range([HEIGHT - MARGIN.bottom, MARGIN.top]);
  const pathLine = line<[number, number]>()
    .x(([index]) => x(index))
    .y(([, value]) => y(value))
    .curve(curveMonotoneX);

  const labels = continents
    .slice(0, 3)
    .map((continent) => ({
      continent,
      y: Math.min(
        HEIGHT - MARGIN.bottom - 2,
        Math.max(MARGIN.top + 6, y(series.get(continent)?.[end] ?? 0) + 3),
      ),
    }))
    .sort((a, b) => a.y - b.y);
  for (let index = labels.length - 2; index >= 0; index -= 1) {
    if (labels[index + 1].y - labels[index].y < 11) labels[index].y = labels[index + 1].y - 11;
  }

  const handleChartClick = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const svgX = ((event.clientX - rect.left) / rect.width) * WIDTH;
    onHourChange(Math.max(start, Math.min(end, Math.round(x.invert(svgX)))));
  };

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-gray-700">
          Demand by continent · {dayKey || 'unknown day'} UTC · {metricLabel(metric)}
        </p>
        <div className="flex flex-wrap gap-3 text-[11px] text-gray-500">
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
      </div>
      <svg
        aria-label="Demand timeline for the selected UTC day"
        className="mt-2 block h-24 w-full cursor-pointer overflow-visible"
        onClick={handleChartClick}
        preserveAspectRatio="none"
        role="img"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      >
        {[0, 6, 12, 18, 24].map((utcHour) => {
          const position = start + Math.min(dayLength - 1, (utcHour / 24) * dayLength);
          return (
            <g key={utcHour}>
              <line
                stroke="rgba(107,114,128,0.2)"
                x1={x(position)}
                x2={x(position)}
                y1={MARGIN.top}
                y2={HEIGHT - MARGIN.bottom}
              />
              <text
                fill="#9ca3af"
                fontSize="10"
                textAnchor={utcHour === 0 ? 'start' : utcHour === 24 ? 'end' : 'middle'}
                x={x(position)}
                y={HEIGHT - 5}
              >
                {String(utcHour).padStart(2, '0')}:00
              </text>
            </g>
          );
        })}
        {continents.map((continent) => {
          const values = series.get(continent) ?? [];
          const points = Array.from({ length: dayLength }, (_, offset) => [
            start + offset,
            values[start + offset] ?? 0,
          ]) as [number, number][];
          return (
            <path
              key={continent}
              d={pathLine(points) ?? undefined}
              fill="none"
              stroke={CONTINENT_COLORS[continent] ?? '#898781'}
              strokeWidth="2"
            />
          );
        })}
        {labels.map(({ continent, y: labelY }) => (
          <text
            key={continent}
            fill="#4b5563"
            fontSize="10"
            fontWeight="600"
            x={x(end) + 6}
            y={labelY}
          >
            {continent}
          </text>
        ))}
        <line
          stroke="#111827"
          strokeWidth="1.2"
          x1={x(hourIndex)}
          x2={x(hourIndex)}
          y1={MARGIN.top - 2}
          y2={HEIGHT - MARGIN.bottom + 3}
        />
      </svg>
      <input
        aria-label="Selected hour"
        className="mt-1 w-full accent-blue-600"
        max={Math.max(0, data.hours_index.length - 1)}
        min="0"
        onChange={(event) => onHourChange(Number(event.target.value))}
        step="1"
        type="range"
        value={hourIndex}
      />
    </section>
  );
}
