'use client';

import { curveMonotoneX, line, scaleLinear } from 'd3';
import type { KeyboardEvent, MouseEvent } from 'react';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { CONTINENT_COLORS, CONTINENT_NAMES, type GeoMetric, type GeoMetricModel } from './geoMath';

const WIDTH = 1_000;
const HEIGHT = 104;
const MARGIN = { top: 10, right: 48, bottom: 22, left: 8 } as const;

interface DayBucket {
  index: number;
  utcHour: number;
}

function metricLabel(metric: GeoMetric): string {
  if (metric === 'tout') return 'output tokens';
  return 'requests';
}

function formatScaleCap(cap: number, metric: GeoMetric): string {
  if (cap <= 0) return 'Absolute scale · no positive volume in range';
  return `Absolute scale · capped at range p99: ${Math.round(cap).toLocaleString()} ${metricLabel(metric)}/continent-hour`;
}

function formatUtcValue(timestamp: string | undefined): string {
  const date = new Date(timestamp ?? '');
  if (Number.isNaN(date.getTime())) return 'Unknown UTC hour';
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

export function TrafficRibbon({
  data,
  hourIndex,
  metricModel,
  onHourChange,
}: {
  data: GeoAnalyticsResponse;
  hourIndex: number;
  metricModel: GeoMetricModel;
  onHourChange: (hourIndex: number) => void;
}) {
  const selected = new Date(data.hours_index[hourIndex] ?? '');
  const validSelected = !Number.isNaN(selected.getTime());
  const dayKey = validSelected ? selected.toISOString().slice(0, 10) : '';
  const dayBuckets = data.hours_index
    .map((timestamp, index): DayBucket | null => {
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== dayKey) return null;
      return {
        index,
        utcHour: date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3_600,
      };
    })
    .filter((bucket): bucket is DayBucket => bucket !== null)
    .sort((a, b) => a.utcHour - b.utcHour || a.index - b.index);
  const latestBucket = dayBuckets.at(-1);
  const selectedBucket = dayBuckets.find((bucket) => bucket.index === hourIndex);
  const series = metricModel.continentSeries;
  const continents = [...series.keys()].sort((a, b) => {
    const aTotal = series.get(a)?.reduce((sum, value) => sum + value, 0) ?? 0;
    const bTotal = series.get(b)?.reduce((sum, value) => sum + value, 0) ?? 0;
    return bTotal - aTotal || a.localeCompare(b);
  });

  const x = scaleLinear()
    .domain([0, 24])
    .range([MARGIN.left, WIDTH - MARGIN.right]);
  const y = scaleLinear()
    .domain([0, Math.max(1, metricModel.continentHourP99)])
    .range([HEIGHT - MARGIN.bottom, MARGIN.top])
    .clamp(true);
  const pathLine = line<[number, number | null]>()
    .defined(([, value]) => value !== null)
    .x(([utcHour]) => x(utcHour))
    .y(([, value]) => y(value ?? 0))
    .curve(curveMonotoneX);

  const labels = continents
    .slice(0, 3)
    .map((continent) => ({
      continent,
      y: Math.min(
        HEIGHT - MARGIN.bottom - 2,
        Math.max(
          MARGIN.top + 6,
          y(latestBucket ? (series.get(continent)?.[latestBucket.index] ?? 0) : 0) + 3,
        ),
      ),
    }))
    .sort((a, b) => a.y - b.y);
  for (let index = labels.length - 2; index >= 0; index -= 1) {
    if (labels[index + 1].y - labels[index].y < 11) labels[index].y = labels[index + 1].y - 11;
  }

  const handleChartClick = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || dayBuckets.length === 0) return;
    const svgX = ((event.clientX - rect.left) / rect.width) * WIDTH;
    const clickedHour = x.invert(Math.max(MARGIN.left, Math.min(WIDTH - MARGIN.right, svgX)));
    const nearestBucket = dayBuckets.reduce((nearest, bucket) =>
      Math.abs(bucket.utcHour - clickedHour) < Math.abs(nearest.utcHour - clickedHour)
        ? bucket
        : nearest,
    );
    onHourChange(nearestBucket.index);
  };

  const handleChartKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    if (!selectedBucket || dayBuckets.length === 0) return;
    const position = dayBuckets.findIndex((bucket) => bucket.index === selectedBucket.index);
    let nextPosition = position;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') nextPosition -= 1;
    else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') nextPosition += 1;
    else if (event.key === 'Home') nextPosition = 0;
    else if (event.key === 'End') nextPosition = dayBuckets.length - 1;
    else return;

    event.preventDefault();
    const nextBucket = dayBuckets[Math.max(0, Math.min(dayBuckets.length - 1, nextPosition))];
    onHourChange(nextBucket.index);
  };

  const selectedValueText = formatUtcValue(data.hours_index[hourIndex]);

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-gray-700">
          Demand by continent · {dayKey || 'unknown day'} UTC · {metricLabel(metricModel.metric)}
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
      <p className="mt-1 text-[11px] text-gray-500" data-testid="ribbon-scale-note">
        {formatScaleCap(metricModel.continentHourP99, metricModel.metric)}
      </p>
      <svg
        aria-label="Demand timeline for the selected UTC day"
        aria-orientation="horizontal"
        aria-valuemax={latestBucket?.utcHour ?? 0}
        aria-valuemin={dayBuckets[0]?.utcHour ?? 0}
        aria-valuenow={selectedBucket?.utcHour ?? 0}
        aria-valuetext={selectedValueText}
        className="mt-2 block h-24 w-full cursor-pointer overflow-visible"
        onClick={handleChartClick}
        onKeyDown={handleChartKeyDown}
        preserveAspectRatio="none"
        role="slider"
        tabIndex={0}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      >
        {[0, 6, 12, 18, 24].map((utcHour) => (
          <g key={utcHour}>
            <line
              stroke="rgba(107,114,128,0.2)"
              x1={x(utcHour)}
              x2={x(utcHour)}
              y1={MARGIN.top}
              y2={HEIGHT - MARGIN.bottom}
            />
            <text
              fill="#9ca3af"
              fontSize="10"
              textAnchor={utcHour === 0 ? 'start' : utcHour === 24 ? 'end' : 'middle'}
              x={x(utcHour)}
              y={HEIGHT - 5}
            >
              {String(utcHour).padStart(2, '0')}:00
            </text>
          </g>
        ))}
        {continents.map((continent) => {
          const values = series.get(continent) ?? [];
          const points: [number, number | null][] = [];
          dayBuckets.forEach((bucket, bucketIndex) => {
            const previous = dayBuckets[bucketIndex - 1];
            if (previous && bucket.utcHour - previous.utcHour > 1.5) {
              points.push([previous.utcHour, null]);
            }
            points.push([bucket.utcHour, values[bucket.index] ?? 0]);
          });
          return (
            <path
              data-continent={continent}
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
            x={x(latestBucket?.utcHour ?? 0) + 6}
            y={labelY}
          >
            {continent}
          </text>
        ))}
        {selectedBucket ? (
          <line
            data-testid="selected-hour-marker"
            stroke="#111827"
            strokeWidth="1.2"
            x1={x(selectedBucket.utcHour)}
            x2={x(selectedBucket.utcHour)}
            y1={MARGIN.top - 2}
            y2={HEIGHT - MARGIN.bottom + 3}
          />
        ) : null}
      </svg>
      <input
        aria-label="Selected hour across loaded range"
        aria-valuetext={selectedValueText}
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
